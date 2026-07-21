from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import copy
from dataclasses import dataclass
from typing import Literal, cast

from tortoise import fields as tortoise_fields
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.fields.relational import ManyToManyFieldInstance
from tortoise.models import Model

from wybra.db.capabilities import (
    tortoise_connection_for_route,
    tortoise_transaction_for_route,
)
from wybra.db.routing import DbConnection, DbRoute
from wybra.db.versioning import (
    OptimisticLockConflict,
    VersionField,
    VersionFieldError,
    delete_model_instance,
    ensure_model_version,
    save_model_update,
    version_field_name,
)
from wybra.events import observe
from wybra.events.forms import persistence_event
from wybra.forms.fields import (
    CheckboxField,
    ChoiceField,
    DateField,
    DateTimeField,
    Field,
    FieldResult,
    Form,
    FormError,
    FormResult,
    IntegerField,
    MultiSelectField,
    NonNegativeIntegerField,
    SaveResult,
    SelectField,
    TextAreaField,
    TextField,
    TimeField,
    UnknownFieldPolicy,
    form_raw_value,
    list_values,
)


class ModelFormError(FormError):
    """Base for model-backed form errors."""


class ModelFormDeclarationError(ModelFormError):
    """Raised when a model form declaration is invalid."""


class ModelBindingError(ModelFormError):
    """Raised when a model binding cannot read or write a record."""


type DeletionAction = Literal["physical", "soft"]


@dataclass(frozen=True, slots=True)
class FormFieldOptions:
    """Form-specific policy for one model field."""

    editable: bool = True
    relation_query: Callable[[RelationQueryContext], Awaitable[RelationPage]] | None = (
        None
    )
    relation_value: (
        Callable[[object, RelationQueryContext], Awaitable[object | None]] | None
    ) = None
    option_format: Callable[[object, RelationQueryContext], Awaitable[str]] | None = (
        None
    )


@dataclass(frozen=True, slots=True)
class RelationPage:
    records: tuple[object, ...]
    next_cursor: str | None = None


class RelationQueryService:
    """Writer-pinned relation querying available to relation callbacks."""

    async def fetch(
        self,
        model: type[Model],
        *,
        selected_values: tuple[object, ...] = (),
        search: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> RelationPage:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class RelationQueryContext:
    model: type[Model]
    selected_values: tuple[object, ...]
    search: str | None
    cursor: str | None
    limit: int | None
    query: RelationQueryService


class _TortoiseRelationQueryService(RelationQueryService):
    def __init__(self, connection: DbConnection, route: DbRoute) -> None:
        self._connection = connection
        self._route = route

    async def fetch(
        self,
        model: type[Model],
        *,
        selected_values: tuple[object, ...] = (),
        search: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> RelationPage:
        """Fetch an ordered page without exposing a Tortoise query object.

        Applications that need searchable or policy-scoped relations supply
        ``FormFieldOptions.relation_query``.  The default intentionally has no
        display-field heuristic, so it provides deterministic primary-key
        pagination only.
        """
        del search
        client = tortoise_connection_for_route(self._connection, self._route)
        primary_key = model._meta.pk_attr
        query = model.all().using_db(client).order_by(primary_key)
        if cursor is not None:
            query = query.filter(**{f"{primary_key}__gt": cursor})

        page_size = limit or 100
        records = tuple(await query.limit(page_size + 1))
        next_cursor = None
        if len(records) > page_size:
            records = records[:page_size]
            next_cursor = str(getattr(records[-1], primary_key))

        if not selected_values:
            return RelationPage(records=records, next_cursor=next_cursor)

        selected = tuple(
            await model.filter(**{f"{primary_key}__in": selected_values})
            .using_db(client)
            .order_by(primary_key)
        )
        selected_keys = {str(getattr(record, primary_key)) for record in selected}
        return RelationPage(
            records=selected
            + tuple(
                record
                for record in records
                if str(getattr(record, primary_key)) not in selected_keys
            ),
            next_cursor=next_cursor,
        )


class ModelForm(Form):
    @classmethod
    def declared_fields(cls) -> dict[str, Field]:
        fields = super().declared_fields()
        allowed = getattr(getattr(cls, "Meta", None), "fields", None)
        if allowed is None:
            return fields
        model = cls._declared_model()
        model_fields = (
            model._meta.fields_map
            if isinstance(model, type) and issubclass(model, Model)
            else {}
        )
        unknown = set(allowed) - (set(fields) | set(model_fields))
        if unknown:
            raise ModelFormDeclarationError(
                "Unknown model form field(s): " + ", ".join(sorted(unknown))
            )
        return {
            name: (
                fields[name]
                if name in fields
                else cls.form_field_from_model(model_fields[name])
            )
            for name in allowed
        }

    @staticmethod
    def form_field_from_model(model_field: object) -> Field:
        required = not bool(getattr(model_field, "null", False))
        related_model = getattr(model_field, "related_model", None)
        if isinstance(related_model, type) and issubclass(related_model, Model):
            if isinstance(model_field, ManyToManyFieldInstance):
                return MultiSelectField(required=required)
            return SelectField(required=required)
        choices = getattr(model_field, "choices", None)
        if choices:
            return ChoiceField(choices=dict(choices), required=required)
        if isinstance(model_field, tortoise_fields.BooleanField):
            return CheckboxField(required=False)
        if isinstance(model_field, VersionField):
            return NonNegativeIntegerField(required=True, widget="hidden")
        if isinstance(
            model_field,
            (
                tortoise_fields.IntField,
                tortoise_fields.SmallIntField,
                tortoise_fields.BigIntField,
            ),
        ):
            return IntegerField(required=required)
        if isinstance(model_field, tortoise_fields.TextField):
            return TextAreaField(required=required)
        if isinstance(model_field, tortoise_fields.CharField):
            return TextField(
                required=required,
                max_length=getattr(model_field, "max_length", None),
            )
        if isinstance(model_field, tortoise_fields.DatetimeField):
            return DateTimeField(required=required)
        if isinstance(model_field, tortoise_fields.DateField):
            return DateField(required=required)
        if isinstance(model_field, tortoise_fields.TimeField):
            return TimeField(required=required)
        return TextField(required=required)

    def __init__(
        self,
        *,
        instance: object | None = None,
        connection: DbConnection | None = None,
        defaults: Mapping[str, object] | None = None,
        values: Mapping[str, object] | None = None,
        options: Mapping[str, Mapping[str, str]] | None = None,
        unknown_fields: UnknownFieldPolicy = "ignore",
    ) -> None:
        self.instance = instance
        self.connection = connection
        self._writer_route = self._select_writer_route(connection)
        model = self._declared_model()
        if (
            isinstance(model, type)
            and issubclass(model, Model)
            and instance is not None
            and not isinstance(instance, Model)
        ):
            raise ModelBindingError("ModelForm instance must be a Tortoise model.")
        self._validate_declared_model_fields()
        bound_defaults = self._bound_defaults(defaults or {}, instance)
        super().__init__(
            defaults=bound_defaults,
            values=values,
            options=options,
            unknown_fields=unknown_fields,
        )
        self._apply_form_options()

    @staticmethod
    def _select_writer_route(connection: DbConnection | None) -> DbRoute | None:
        if connection is None:
            return None
        return connection.for_write()

    async def prepare_relations(
        self,
        *,
        selected_values: Mapping[str, object] | None = None,
        search: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> None:
        """Load relation options through this form's selected writer route."""
        if not self._relation_fields():
            return
        if self.connection is None or self._writer_route is None:
            raise ModelBindingError("Relation controls require DbConnection.")

        selected_values = selected_values or {}
        query = _TortoiseRelationQueryService(self.connection, self._writer_route)
        for name, related_model in self._relation_fields().items():
            raw_selected = (
                form_raw_value(
                    selected_values,
                    name,
                    multiple=isinstance(self.fields[name], MultiSelectField),
                )
                if name in selected_values
                else self.fields[name].value
            )
            values = tuple(str(value) for value in list_values(raw_selected))
            context = RelationQueryContext(
                model=related_model,
                selected_values=values,
                search=search,
                cursor=cursor,
                limit=limit,
                query=query,
            )
            policy = self._declared_form_options(self.fields)[name]
            page = await self._relation_page(policy, context)
            choices = {
                str(self._primary_key(record)): await self._format_relation_option(
                    policy, record, context
                )
                for record in page.records
            }
            field = self.fields[name]
            if isinstance(field, SelectField):
                field.choices = choices

    async def parse(self, data: Mapping[str, object]) -> FormResult:
        await self.prepare_relations(selected_values=data)
        await super().parse(data)
        await self._resolve_relation_values()
        return self.result

    def apply(self, instance: object | None = None) -> object:
        target = instance if instance is not None else self.instance
        if target is None:
            raise ModelBindingError("ModelForm.apply() requires an instance.")
        if not self.is_valid():
            return target

        version_name = (
            version_field_name(type(target)) if isinstance(target, Model) else None
        )

        for name, result in self.result.fields.items():
            form_field = self.fields[name]
            if (
                form_field.disabled
                or not result.is_valid
                or name in self._many_to_many_fields()
                or name == version_name
            ):
                continue
            setattr(target, name, form_field.to_model_value(result.value))
        return target

    @observe(persistence_event)
    async def save(self) -> SaveResult:
        model = self._declared_model()
        if not isinstance(model, type) or not issubclass(model, Model):
            return await self._save()
        self._stale_conflict = False
        if self.connection is None or self._writer_route is None:
            raise ModelBindingError("ModelForm persistence requires DbConnection.")
        async with self._writer_transaction() as client:
            return await self._save_with_client(client)

    def _writer_transaction(self):
        if self.connection is None or self._writer_route is None:
            raise ModelBindingError("ModelForm persistence requires DbConnection.")
        return tortoise_transaction_for_route(self.connection, self._writer_route)

    async def _save_with_client(self, client: BaseDBAsyncClient) -> SaveResult:
        """Persist through an already-selected internal writer transaction."""
        model = self._declared_model()
        if not isinstance(model, type) or not issubclass(model, Model):
            raise ModelBindingError("ModelForm persistence requires a Tortoise model.")
        created = self.instance is None
        target = self.create_instance(model) if created else cast(Model, self.instance)
        original = None if created else copy(target)
        self.apply(target)
        if not self.result.is_valid:
            raise FormError("Cannot save an invalid form.")
        changed_fields = self._changed_model_fields(original, target, created)
        try:
            many_to_many_changes = (
                tuple(
                    name
                    for name in self._many_to_many_fields()
                    if self.result.fields[name].value is not None
                )
                if created
                else await self._changed_many_to_many_fields(target, client)
            )
            changed_fields += many_to_many_changes
            if not created and not changed_fields:
                await ensure_model_version(
                    target, client=client, expected_version=self._submitted_version()
                )
                return SaveResult(primary=target, original=original)
            if created:
                await target.save(using_db=client)
            else:
                await save_model_update(
                    target, client=client, expected_version=self._submitted_version()
                )
            await self._save_many_to_many_relations(
                target, client, many_to_many_changes
            )
        except OptimisticLockConflict:
            self._stale_conflict = True
            self.add_error(None, "This record was changed by another user.")
            return SaveResult(primary=target, original=original)
        self.instance = target
        return SaveResult(
            primary=target,
            original=original,
            changed_fields=changed_fields,
            created=created,
            updated=not created,
            affected_count=1,
        )

    @observe(persistence_event, "delete")
    async def delete(self) -> SaveResult:
        """Delete the bound model instance through this form's writer route."""
        model = self._declared_model()
        if not isinstance(model, type) or not issubclass(model, Model):
            raise ModelBindingError("ModelForm.delete() requires a Tortoise model.")
        self._stale_conflict = False
        return await self._delete_model(model)

    async def _delete_model(self, model: type[Model]) -> SaveResult:
        if self.instance is None:
            raise ModelBindingError("ModelForm.delete() requires an existing instance.")
        if self.connection is None or self._writer_route is None:
            raise ModelBindingError("ModelForm deletion requires DbConnection.")
        if not self.result.is_valid:
            raise FormError("Cannot delete with an invalid form.")

        target = self.instance
        if not isinstance(target, Model):
            raise ModelBindingError("ModelForm instance must be a Tortoise model.")
        original = copy(target)
        action = await self.deletion_action(target)
        try:
            async with tortoise_transaction_for_route(
                self.connection, self._writer_route
            ) as client:
                if action == "physical":
                    await delete_model_instance(
                        target,
                        client=client,
                        expected_version=self._submitted_version(),
                    )
                    changed_fields: tuple[str, ...] = ()
                elif action == "soft":
                    await save_model_update(
                        target,
                        client=client,
                        expected_version=self._submitted_version(),
                    )
                    changed_fields = self._changed_model_fields(original, target, False)
                else:
                    raise ModelBindingError(f"Unknown deletion action: {action}.")
        except OptimisticLockConflict:
            self._stale_conflict = True
            self.add_error(None, "This record was changed by another user.")
            return SaveResult(primary=target, original=original)
        return SaveResult(
            primary=target,
            original=original,
            changed_fields=changed_fields,
            updated=action == "soft",
            deleted=True,
            affected_count=1,
        )

    async def deletion_action(self, instance: Model) -> DeletionAction:
        """Select physical deletion or save a soft-deleted instance."""
        del instance
        return "physical"

    def create_instance(self, model: type[Model]) -> Model:
        """Create a new model instance for this form.

        Model forms for models with required values outside their editable
        fields can override this hook to supply those values.
        """
        return model()

    def _changed_model_fields(
        self,
        original: Model | None,
        target: Model,
        created: bool,
    ) -> tuple[str, ...]:
        changed: list[str] = []
        for name, result in self.result.fields.items():
            form_field = self.fields[name]
            if form_field.disabled or not result.is_valid:
                continue
            if name in self._many_to_many_fields():
                continue
            if (
                created
                or original is None
                or getattr(original, name) != getattr(target, name)
            ):
                changed.append(name)
        return tuple(changed)

    def _submitted_version(self) -> int | None:
        model = self._declared_model()
        if not isinstance(model, type) or not issubclass(model, Model):
            return None
        name = version_field_name(model)
        if name is None:
            return None
        result = self.result.fields.get(name)
        if result is None or not result.is_valid or not isinstance(result.value, int):
            raise ModelBindingError(
                "Versioned ModelForm updates require a valid submitted version field."
            )
        return result.value

    async def _resolve_relation_values(self) -> None:
        relation_fields = self._relation_fields()
        if not relation_fields:
            return
        if self.connection is None or self._writer_route is None:
            raise ModelBindingError("Relation controls require DbConnection.")

        query = _TortoiseRelationQueryService(self.connection, self._writer_route)
        updated = dict(self.field_results)
        policies = self._declared_form_options(self.fields)
        for name, related_model in relation_fields.items():
            result = updated[name]
            if not result.is_valid or result.value is None:
                continue
            values = tuple(str(value) for value in list_values(result.value))
            context = RelationQueryContext(
                model=related_model,
                selected_values=values,
                search=None,
                cursor=None,
                limit=None,
                query=query,
            )
            resolved = await self._resolve_relation_value(
                name, policies[name], result.value, context
            )
            if resolved is None:
                self.add_error(name, "Select a valid option.")
                continue
            if isinstance(self.fields[name], MultiSelectField):
                if not isinstance(resolved, tuple):
                    self.add_error(name, "Select valid options.")
                    continue
            updated[name] = FieldResult(
                name=name,
                raw_value=result.raw_value,
                value=resolved,
            )

        self.field_results = updated
        fields = self._results_with_errors(updated)
        self._result = FormResult(
            fields=fields,
            unknown_fields=self.result.unknown_fields,
            form_errors=tuple(self.errors.get(None, ())),
        )

    async def _relation_page(
        self,
        policy: FormFieldOptions,
        context: RelationQueryContext,
    ) -> RelationPage:
        if policy.relation_query is not None:
            return await policy.relation_query(context)
        return await context.query.fetch(
            context.model,
            selected_values=context.selected_values,
            search=context.search,
            cursor=context.cursor,
            limit=context.limit,
        )

    async def _resolve_relation_value(
        self,
        field_name: str,
        policy: FormFieldOptions,
        raw_value: object,
        context: RelationQueryContext,
    ) -> object | None:
        if policy.relation_value is not None:
            return await policy.relation_value(raw_value, context)
        page = await context.query.fetch(
            context.model,
            selected_values=context.selected_values,
        )
        records = {str(self._primary_key(record)): record for record in page.records}
        if isinstance(self.fields[field_name], MultiSelectField):
            resolved = tuple(records.get(value) for value in context.selected_values)
            return resolved if all(record is not None for record in resolved) else None
        return records.get(str(raw_value))

    @staticmethod
    async def _format_relation_option(
        policy: FormFieldOptions,
        record: object,
        context: RelationQueryContext,
    ) -> str:
        if policy.option_format is not None:
            return await policy.option_format(record, context)
        return str(record)

    def _relation_fields(self) -> dict[str, type[Model]]:
        model = self._declared_model()
        if not isinstance(model, type) or not issubclass(model, Model):
            return {}
        relations: dict[str, type[Model]] = {}
        for name in self.fields:
            field = model._meta.fields_map.get(name)
            related_model = getattr(field, "related_model", None)
            if isinstance(related_model, type) and issubclass(related_model, Model):
                relations[name] = related_model
        return relations

    async def _save_many_to_many_relations(
        self,
        target: Model,
        client: BaseDBAsyncClient,
        names: tuple[str, ...],
    ) -> None:
        for name in names:
            value = self.result.fields[name].value
            if value is None:
                continue
            if not isinstance(value, tuple):
                raise ModelBindingError(
                    f"Multi-select relation field must resolve to records: {name}."
                )
            relation = getattr(target, name)
            await relation.clear(using_db=client)
            await relation.add(*value, using_db=client)

    async def _changed_many_to_many_fields(
        self,
        target: Model,
        client: BaseDBAsyncClient,
    ) -> tuple[str, ...]:
        changed: list[str] = []
        for name in self._many_to_many_fields():
            value = self.result.fields[name].value
            if value is None:
                continue
            if not isinstance(value, tuple):
                raise ModelBindingError(
                    f"Multi-select relation field must resolve to records: {name}."
                )
            current = await getattr(target, name).all().using_db(client)
            if {self._primary_key(record) for record in current} != {
                self._primary_key(record) for record in value
            }:
                changed.append(name)
        return tuple(changed)

    def _many_to_many_fields(self) -> tuple[str, ...]:
        model = self._declared_model()
        if not isinstance(model, type) or not issubclass(model, Model):
            return ()
        return tuple(
            name
            for name in self._relation_fields()
            if isinstance(model._meta.fields_map[name], ManyToManyFieldInstance)
        )

    @staticmethod
    def _primary_key(record: object) -> object:
        meta = getattr(record, "_meta", None)
        primary_key = getattr(meta, "pk_attr", None)
        if not isinstance(primary_key, str):
            raise ModelBindingError("Relation record has no primary key metadata.")
        return getattr(record, primary_key)

    def _bound_defaults(
        self,
        defaults: Mapping[str, object],
        instance: object | None,
    ) -> dict[str, object]:
        bound_defaults = dict(defaults)
        if instance is not None:
            for name, field in self.declared_fields().items():
                try:
                    value = getattr(instance, name)
                except AttributeError as exc:
                    raise ModelBindingError(
                        f"Model instance has no attribute for field: {name}."
                    ) from exc
                bound_defaults[name] = field.from_model_value(value)
        return bound_defaults

    @classmethod
    def _declared_model(cls) -> object:
        model = getattr(getattr(cls, "Meta", None), "model", None)
        if model is None:
            raise ModelFormDeclarationError(
                "ModelForm declarations require Meta.model."
            )
        return model

    @classmethod
    def _validate_declared_model_fields(cls) -> None:
        model = cls._declared_model()
        if hasattr(getattr(cls, "Meta", None), "bindings"):
            raise ModelFormDeclarationError(
                "Meta.bindings is no longer supported; use field value adapters."
            )
        if not isinstance(model, type) or not issubclass(model, Model):
            return
        try:
            version_field_name(model)
        except VersionFieldError as exc:
            raise ModelFormDeclarationError(str(exc)) from exc
        fields = cls.declared_fields()
        unknown = set(fields) - set(model._meta.fields_map)
        if unknown:
            raise ModelFormDeclarationError(
                "Unknown Tortoise model field(s): " + ", ".join(sorted(unknown))
            )

        relation_fields = cls._relation_model_field_names(model)
        for name, options in cls._declared_form_options(fields).items():
            has_relation_policy = any(
                (
                    options.relation_query,
                    options.relation_value,
                    options.option_format,
                )
            )
            if has_relation_policy and name not in relation_fields:
                raise ModelFormDeclarationError(
                    f"Relation form options require a relation field: {name}."
                )

    @staticmethod
    def _relation_model_field_names(model: type[Model]) -> set[str]:
        relation_fields: set[str] = set()
        for name, field in model._meta.fields_map.items():
            related_model = getattr(field, "related_model", None)
            if isinstance(related_model, type) and issubclass(related_model, Model):
                relation_fields.add(name)
        return relation_fields

    @classmethod
    def _declared_form_options(
        cls, fields: Mapping[str, object]
    ) -> dict[str, FormFieldOptions]:
        raw_options = getattr(getattr(cls, "Meta", None), "form_options", {}) or {}
        if not isinstance(raw_options, Mapping):
            raise ModelFormDeclarationError("Meta.form_options must be a mapping.")
        unknown = set(raw_options) - set(fields)
        if unknown:
            raise ModelFormDeclarationError(
                "Unknown form option field(s): " + ", ".join(sorted(unknown))
            )
        if not all(
            isinstance(option, FormFieldOptions) for option in raw_options.values()
        ):
            raise ModelFormDeclarationError(
                "Meta.form_options values must be FormFieldOptions."
            )
        return {name: raw_options.get(name, FormFieldOptions()) for name in fields}

    def _apply_form_options(self) -> None:
        for name, options in self._declared_form_options(self.fields).items():
            if not options.editable:
                self.fields[name].disabled = True


__all__ = (
    "FormFieldOptions",
    "ModelBindingError",
    "ModelForm",
    "ModelFormDeclarationError",
    "ModelFormError",
    "RelationPage",
    "RelationQueryContext",
    "RelationQueryService",
)
