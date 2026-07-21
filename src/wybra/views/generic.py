"""Persistence-independent generic resource views."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from tortoise.models import Model

from wybra.api import ApiCapability
from wybra.content_types import ContentType, ContentTypesCapability
from wybra.core.routes import RouteType
from wybra.db import DatabaseCapability
from wybra.db.capabilities import tortoise_connection_for_route
from wybra.errors.diagnostics import type_name
from wybra.events import observe
from wybra.events.views import generic_view_event
from wybra.forms import ModelForm
from wybra.forms.csrf import request_form_data
from wybra.site import get_site
from wybra.views.base import HandlerResult, View
from wybra.views.bulk import BulkAction, BulkActionResult, BulkDeleteAction
from wybra.views.templates import TemplateResponse


class _RequestValidationError(ValueError):
    """Invalid client input that should become a representation-aware response."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class _ApiResponse:
    """Deferred API rendering without imposing a representation on views."""

    request: Request
    data: object
    status_code: int = 200

    async def render_response(self) -> Response:
        """Delegate representation selection to the configured API capability."""
        api = get_site(self.request.app).require_capability(ApiCapability)
        return api.response(self.data, status_code=self.status_code)


class GenericView(View):
    """Base for collection and item views using standard HTTP dispatch."""

    template: ClassVar[str] = "views/generic/view.html"
    editor_template: ClassVar[str] = "views/generic/editor.html"
    delete_template: ClassVar[str] = "views/generic/delete.html"

    async def dispatch(self, request: Request, **kwargs: Any) -> Response:
        """Retain the registration representation without changing handler hooks."""
        route_type = kwargs.pop("_route_type", RouteType.PAGE)
        self._route_type = RouteType(route_type)
        self._collection_path = kwargs.pop("_collection_path", request.url.path)
        return await super().dispatch(request, **kwargs)

    @classmethod
    def route_definitions(cls, base_path: str):
        """Return the collection, bulk, and item routes for this resource."""
        from wybra.views.routing import ViewRoute

        return (
            ViewRoute(base_path, ("GET", "POST"), name_suffix="collection"),
            ViewRoute(
                f"{base_path.rstrip('/')}/bulk",
                ("POST",),
                name_suffix="bulk",
                dispatch_kwargs={"bulk": True},
            ),
            ViewRoute(
                f"{base_path.rstrip('/')}/{{id}}",
                ("GET", "PATCH", "DELETE"),
                name_suffix="item",
                path_parameter="id",
            ),
        )

    async def get(self, request: Request, **kwargs: Any) -> HandlerResult:
        object_id = kwargs.get("id")
        if object_id is None:
            return await self.list_objects(request)
        return await self.retrieve_object(request, str(object_id))

    async def post(self, request: Request, **kwargs: Any) -> HandlerResult:
        if kwargs.get("bulk"):
            return await self.bulk_action(request)
        return await self.create_object(request)

    async def patch(self, request: Request, **kwargs: Any) -> HandlerResult:
        return await self.update_object(request, self._require_object_id(kwargs))

    async def delete(self, request: Request, **kwargs: Any) -> HandlerResult:
        return await self.delete_object(request, self._require_object_id(kwargs))

    async def list_objects(self, request: Request) -> HandlerResult:
        """Return the collection representation for this request."""
        raise NotImplementedError

    async def retrieve_object(self, request: Request, object_id: str) -> HandlerResult:
        """Return one object representation for this request."""
        raise NotImplementedError

    async def create_object(self, request: Request) -> HandlerResult:
        """Create and return one object representation for this request."""
        raise NotImplementedError

    async def update_object(self, request: Request, object_id: str) -> HandlerResult:
        """Update and return one object representation for this request."""
        raise NotImplementedError

    async def delete_object(self, request: Request, object_id: str) -> HandlerResult:
        """Delete and return one object representation for this request."""
        raise NotImplementedError

    async def bulk_action(self, request: Request) -> HandlerResult:
        """Perform a collection action for this request."""
        raise NotImplementedError

    def _require_object_id(self, kwargs: dict[str, Any]) -> str:
        object_id = kwargs.get("id")
        if object_id is None:
            raise ValueError("GenericView requires route parameter 'id'.")
        return str(object_id)


class ModelGenericView(GenericView):
    """Generic model resource adapter using Wybra capability boundaries."""

    model: ClassVar[type[Model] | None] = None
    form: ClassVar[type[ModelForm] | None] = None
    bulk_actions: ClassVar[dict[str, BulkAction]] = {}
    database_name: ClassVar[str] = "default"
    _generated_form: ClassVar[type[ModelForm] | None] = None
    _transient_collection_parameters = frozenset(
        {"edit", "delete", "confirm_delete", "highlight"}
    )
    _event_bulk_counts: tuple[int, int, int] | None = None

    @observe(generic_view_event)
    async def dispatch(self, request: Request, **kwargs: Any) -> Response:
        """Translate client input validation failures to the active representation."""
        self._event_bulk_counts = None
        try:
            response = await super().dispatch(request, **kwargs)
        except _RequestValidationError as exc:
            response = await self._request_validation_response(request, exc)
        return response

    async def list_objects(self, request: Request) -> HandlerResult:
        """Render or represent the configured model collection."""
        records = await self.get_collection(request)
        content_type = self.get_content_type(request)
        if self.is_api_request(request):
            return _ApiResponse(
                request,
                [self.serialise_object(record) for record in records],
            )
        context: dict[str, object] = {
            "content_type": content_type,
            "objects": records,
            "create_form": await self.get_form(request),
            "bulk_actions": self.bulk_actions,
            "collection_path": self._collection_path,
            "collection_url": self._collection_url(request),
            "bulk_action_url": self._bulk_action_url(request),
            "page_title": content_type.verbose_name_plural,
        }
        edit_id = request.query_params.get("edit")
        delete_id = request.query_params.get("delete")
        if edit_id is not None:
            record = await self.get_object(request, edit_id)
            editor_context = await self._editor_context(request, record)
            if self._is_htmx_request(request):
                return TemplateResponse(
                    request,
                    self.editor_template,
                    await self.get_context(editor_context, request),
                )
            context.update(editor_context)
        elif delete_id is not None:
            record = await self.get_object(request, delete_id)
            delete_context = self._delete_context(request, record)
            if self._is_htmx_request(request):
                return TemplateResponse(
                    request,
                    self.delete_template,
                    await self.get_context(delete_context, request),
                )
            context.update(delete_context)
        return TemplateResponse(
            request,
            self.template,
            await self.get_context(context, request),
        )

    async def retrieve_object(
        self,
        request: Request,
        object_id: str,
    ) -> HandlerResult:
        """Render or represent one visible model record by primary key."""
        record = await self.get_object(request, object_id)
        if self.is_api_request(request):
            return _ApiResponse(request, self.serialise_object(record))
        if self._is_htmx_request(request):
            if request.query_params.get("confirm_delete") == "1":
                return TemplateResponse(
                    request,
                    self.delete_template,
                    await self.get_context(
                        self._delete_context(request, record),
                        request,
                    ),
                )
            return TemplateResponse(
                request,
                self.editor_template,
                await self.get_context(
                    await self._editor_context(request, record),
                    request,
                ),
            )
        return RedirectResponse(self._edit_url(request, record), status_code=303)

    async def create_object(self, request: Request) -> HandlerResult:
        """Bind, validate, and persist a new model instance."""
        form = await self.get_form(request)
        return await self._save_form(request, form, status_code=201)

    async def update_object(self, request: Request, object_id: str) -> HandlerResult:
        """Bind, validate, and persist an existing model instance."""
        instance = await self.get_object(request, object_id)
        form = await self.get_form(request, instance=instance)
        return await self._save_form(request, form)

    async def delete_object(self, request: Request, object_id: str) -> HandlerResult:
        """Delete a confirmed model record through its bound ModelForm."""
        values = await self.request_values(request)
        if values.get("confirm") not in (True, "true", "True", "1", 1):
            record = await self.get_object(request, object_id)
            if self.is_api_request(request):
                return (
                    get_site(request.app)
                    .require_capability(ApiCapability)
                    .validation_error_response(
                        [
                            {
                                "field": "confirm",
                                "messages": ["Confirmation is required."],
                            }
                        ]
                    )
                )
            return TemplateResponse(
                request,
                self.template,
                await self.get_context(
                    self._delete_context(request, record),
                    request,
                ),
                status_code=422,
            )
        record = await self.get_object(request, object_id)
        deleted = await self.delete_record(request, record)
        if self.is_api_request(request):
            return _ApiResponse(
                request,
                {"id": object_id, "deleted": deleted},
            )
        return self._mutation_redirect(request, record)

    async def bulk_action(self, request: Request) -> HandlerResult:
        """Run an explicitly registered action over visible selected records."""
        values = await self.request_values(request)
        action_name = values.get("action")
        selected = _selected_values(values)
        if not isinstance(action_name, str):
            raise _RequestValidationError(
                "action", "Bulk actions require an action name."
            )
        action = self.bulk_actions.get(action_name)
        if action is None:
            raise _RequestValidationError(
                "action", f"Bulk action is not registered: {action_name}."
            )
        if isinstance(action, BulkDeleteAction) and not _is_confirmed(values):
            return await self._bulk_confirmation_error(request)
        selected_ids = tuple(dict.fromkeys(str(value) for value in selected))
        visible_records = {
            str(getattr(record, record._meta.pk_attr)): record
            for record in await self.get_collection(request)
        }
        records = tuple(
            visible_records[record_id]
            for record_id in selected_ids
            if record_id in visible_records
        )
        skipped_ids = tuple(
            record_id for record_id in selected_ids if record_id not in visible_records
        )
        result = await action.execute(self, request, records)
        if skipped_ids:
            result = BulkActionResult(
                affected_ids=result.affected_ids,
                skipped_ids=(*result.skipped_ids, *skipped_ids),
                failed_ids=result.failed_ids,
            )
        self._event_bulk_counts = (
            len(result.affected_ids),
            len(result.skipped_ids),
            len(result.failed_ids),
        )
        return await self._bulk_result_response(request, result)

    def _content_type_identifier(self, request: Request) -> str | None:
        """Return content-type identity when it is already available to the view."""

        content_types = get_site(request.app).optional_capability(
            ContentTypesCapability
        )
        if content_types is None:
            return None
        try:
            return content_types.for_model(self.get_model()).identifier
        except Exception:
            return None

    def _model_type_identity(self) -> str | None:
        """Return model identity without allowing observation to change a view error."""

        try:
            return type_name(self.get_model())
        except Exception:
            return None

    async def delete_record(self, request: Request, record: Model) -> bool:
        """Delete one visible record for an explicitly registered bulk action."""
        form = await self.get_form(request, instance=record)
        result = await form.delete()
        return result.deleted

    async def get_collection(self, request: Request) -> tuple[Model, ...]:
        """Return the ordered collection through the selected reader route."""
        model = self.get_model()
        client = self._read_client(request)
        return tuple(
            await model.all().using_db(client).order_by(*self.get_list_ordering())
        )

    async def get_object(self, request: Request, object_id: str) -> Model:
        """Return one record by its configured model primary key."""
        model = self.get_model()
        primary_key = model._meta.pk_attr
        record = (
            await model.filter(**{primary_key: object_id})
            .using_db(self._read_client(request))
            .first()
        )
        if record is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Record was not found.")
        return record

    def get_model(self) -> type[Model]:
        """Return the explicitly configured Tortoise model class."""
        if self.model is None:
            raise ValueError("ModelGenericView requires model.")
        return self.model

    def get_content_type(self, request: Request) -> ContentType:
        """Return content metadata for the configured model."""
        content_types = get_site(request.app).require_capability(ContentTypesCapability)
        return content_types.for_model(self.get_model())

    def get_list_ordering(self) -> tuple[str, ...]:
        """Return model-specific ordering, falling back to its primary key."""
        ordering = getattr(self.get_form_class().Meta, "list_ordering", None)
        model = self.get_model()
        if ordering is None:
            ordering = getattr(model.Meta, "ordering", None)
        if ordering is None:
            return (model._meta.pk_attr,)
        if isinstance(ordering, str):
            return (ordering,)
        return tuple(ordering)

    def get_form_class(self) -> type[ModelForm]:
        """Return an explicit form or derive one from editable model fields."""
        if self.form is not None:
            return self.form
        view_type = type(self)
        generated_form = view_type.__dict__.get("_generated_form")
        if generated_form is not None:
            return generated_form
        model = self.get_model()
        fields = getattr(model.Meta, "form_fields", None)
        if fields is None:
            fields = _generated_model_form_fields(model)
        meta = type(
            "Meta",
            (),
            {
                "model": model,
                "fields": tuple(fields),
                "form_options": getattr(model.Meta, "form_options", {}),
            },
        )
        generated_form = type(
            f"{model.__name__}GenericForm",
            (ModelForm,),
            {"Meta": meta},
        )
        view_type._generated_form = generated_form
        return generated_form

    async def get_form(
        self,
        request: Request,
        *,
        instance: Model | None = None,
    ) -> ModelForm:
        """Instantiate the resolved form through the model writer connection."""
        database = get_site(request.app).require_capability(DatabaseCapability)
        return self.get_form_class()(
            instance=instance,
            connection=database.database(self.database_name),
        )

    async def _save_form(
        self,
        request: Request,
        form: ModelForm,
        *,
        status_code: int = 200,
    ) -> HandlerResult:
        await form.parse(await self.request_values(request))
        if not form.result.is_valid:
            return await self._invalid_form_response(request, form)
        result = await form.save()
        if not form.result.is_valid:
            return await self._invalid_form_response(request, form)
        record = result.primary
        if not isinstance(record, Model):
            raise ValueError("ModelGenericView form did not return a model record.")
        if self.is_api_request(request):
            return _ApiResponse(request, self.serialise_object(record), status_code)
        return self._mutation_redirect(request, record)

    async def request_values(self, request: Request) -> Mapping[str, object]:
        """Read mapping input for either the API or HTML representation."""
        if self.is_api_request(request):
            try:
                values = await request.json()
            except ValueError as exc:
                raise _RequestValidationError(
                    "body", "Generic API request body must be valid JSON."
                ) from exc
            if isinstance(values, Mapping):
                return values
            raise _RequestValidationError(
                "body", "Generic API request body must be a JSON object."
            )
        return await request_form_data(request)

    async def _request_validation_response(
        self,
        request: Request,
        error: _RequestValidationError,
    ) -> Response:
        if self.is_api_request(request):
            return (
                get_site(request.app)
                .require_capability(ApiCapability)
                .validation_error_response(
                    [{"field": error.field, "messages": [str(error)]}]
                )
            )
        context = await self._collection_context(request)
        context["bulk_error"] = str(error)
        return await TemplateResponse(
            request,
            self.template,
            await self.get_context(context, request),
            status_code=422,
        ).render_response()

    async def _invalid_form_response(
        self,
        request: Request,
        form: ModelForm,
    ) -> HandlerResult:
        if self.is_api_request(request):
            errors = [
                {"field": field, "messages": list(messages)}
                for field, messages in form.result.errors.items()
            ]
            return (
                get_site(request.app)
                .require_capability(ApiCapability)
                .validation_error_response(errors)
            )
        content_type = self.get_content_type(request)
        context: dict[str, object] = {
            "content_type": content_type,
            "objects": await self.get_collection(request),
            "bulk_actions": self.bulk_actions,
            "collection_path": self._collection_path,
            "collection_url": self._collection_url(request),
            "bulk_action_url": self._bulk_action_url(request),
            "page_title": content_type.verbose_name_plural,
        }
        if isinstance(form.instance, Model):
            context.update(await self._editor_context(request, form.instance))
            context["form"] = form
        else:
            context["create_form"] = form
        return TemplateResponse(
            request,
            self.template,
            await self.get_context(context, request),
            status_code=422,
        )

    def _mutation_redirect(self, request: Request, record: Model) -> Response:
        primary_key = str(getattr(record, record._meta.pk_attr))
        location = self._collection_url(request, highlight=primary_key)
        if request.headers.get("HX-Request", "").lower() == "true":
            return Response(status_code=204, headers={"HX-Redirect": location})
        return RedirectResponse(location, status_code=303)

    async def _bulk_result_response(
        self,
        request: Request,
        result: BulkActionResult,
    ) -> HandlerResult:
        data = {
            "affected_ids": result.affected_ids,
            "skipped_ids": result.skipped_ids,
            "failed_ids": result.failed_ids,
        }
        if self.is_api_request(request):
            return _ApiResponse(request, data)
        context = await self._collection_context(request)
        context["bulk_result"] = data
        return TemplateResponse(
            request,
            self.template,
            await self.get_context(context, request),
        )

    async def _bulk_confirmation_error(self, request: Request) -> HandlerResult:
        if self.is_api_request(request):
            return (
                get_site(request.app)
                .require_capability(ApiCapability)
                .validation_error_response(
                    [{"field": "confirm", "messages": ["Confirmation is required."]}]
                )
            )
        context = await self._collection_context(request)
        context["bulk_error"] = "Confirmation is required."
        return TemplateResponse(
            request,
            self.template,
            await self.get_context(context, request),
            status_code=422,
        )

    async def get_context(
        self,
        context: dict[str, object],
        request: Request,
    ) -> dict[str, object]:
        """Extend the model-driven template context for an HTML representation."""
        return context

    def serialise_object(self, record: Model) -> dict[str, object]:
        """Map database fields to a transport-neutral record representation."""
        return {
            name: _serialise_api_value(getattr(record, name))
            for name in record._meta.db_fields
        }

    def is_api_request(self, request: Request) -> bool:
        """Determine representation through the configured API capability."""
        if self._route_type is not RouteType.API:
            return False
        return (
            get_site(request.app)
            .require_capability(ApiCapability)
            .is_api_request(
                request,
                route_type=self._route_type,
            )
        )

    def _read_client(self, request: Request):
        database = get_site(request.app).require_capability(DatabaseCapability)
        connection = database.database(self.database_name)
        return tortoise_connection_for_route(connection, connection.for_read())

    def _object_path(self, record: Model) -> str:
        primary_key = getattr(record, record._meta.pk_attr)
        return f"{self._collection_path.rstrip('/')}/{primary_key}"

    def _collection_url(self, request: Request, **additional: str) -> str:
        """Return the collection URL with user-facing query state retained."""
        parameters = [
            (name, value)
            for name, value in request.query_params.multi_items()
            if name not in self._transient_collection_parameters
        ]
        parameters.extend(additional.items())
        if not parameters:
            return self._collection_path
        return f"{self._collection_path}?{urlencode(parameters)}"

    def _with_collection_state(self, request: Request, path: str) -> str:
        collection_url = self._collection_url(request)
        if "?" not in collection_url:
            return path
        return f"{path}?{collection_url.split('?', maxsplit=1)[1]}"

    def _bulk_action_url(self, request: Request) -> str:
        return self._with_collection_state(
            request,
            f"{self._collection_path.rstrip('/')}/bulk",
        )

    def _edit_url(self, request: Request, record: Model) -> str:
        primary_key = str(getattr(record, record._meta.pk_attr))
        return self._collection_url(request, edit=primary_key)

    def _delete_url(self, request: Request, record: Model) -> str:
        primary_key = str(getattr(record, record._meta.pk_attr))
        return self._collection_url(request, delete=primary_key)

    async def _editor_context(
        self,
        request: Request,
        record: Model,
    ) -> dict[str, object]:
        content_type = self.get_content_type(request)
        object_path = self._object_path(record)
        form_action = self._with_collection_state(request, object_path)
        return {
            "content_type": content_type,
            "object": record,
            "form": await self.get_form(request, instance=record),
            "form_action": form_action,
            "form_method": "patch",
            "form_attr": {"hx-patch": form_action},
            "delete_action": form_action,
            "delete_fragment_url": self._delete_url(request, record),
            "collection_path": self._collection_path,
            "collection_url": self._collection_url(request),
            "page_title": content_type.verbose_name_plural,
        }

    def _delete_context(self, request: Request, record: Model) -> dict[str, object]:
        content_type = self.get_content_type(request)
        object_path = self._object_path(record)
        return {
            "content_type": content_type,
            "object": record,
            "delete_action": self._with_collection_state(request, object_path),
            "collection_path": self._collection_path,
            "collection_url": self._collection_url(request),
            "page_title": content_type.verbose_name_plural,
        }

    async def _collection_context(self, request: Request) -> dict[str, object]:
        content_type = self.get_content_type(request)
        return {
            "content_type": content_type,
            "objects": await self.get_collection(request),
            "create_form": await self.get_form(request),
            "bulk_actions": self.bulk_actions,
            "collection_path": self._collection_path,
            "collection_url": self._collection_url(request),
            "bulk_action_url": self._bulk_action_url(request),
            "page_title": content_type.verbose_name_plural,
        }

    @staticmethod
    def _is_htmx_request(request: Request) -> bool:
        return request.headers.get("HX-Request", "").lower() == "true"


__all__ = ["GenericView", "ModelGenericView"]


def _selected_values(values: Mapping[str, object]) -> tuple[object, ...]:
    getlist = getattr(values, "getlist", None)
    if callable(getlist):
        return tuple(getlist("selected"))
    if "selected" not in values:
        return ()
    selected = values["selected"]
    if not isinstance(selected, (list, tuple)):
        raise _RequestValidationError(
            "selected", "Bulk action selections must be an array."
        )
    if not all(
        isinstance(value, (str, int)) and not isinstance(value, bool)
        for value in selected
    ):
        raise _RequestValidationError(
            "selected", "Bulk action selections must contain string or integer IDs."
        )
    return tuple(selected)


def _is_confirmed(values: Mapping[str, object]) -> bool:
    return values.get("confirm") in (True, "true", "True", "1", 1)


def _generated_model_form_fields(model: type[Model]) -> tuple[str, ...]:
    """Return editable declared fields for a model-generated form."""
    meta = model._meta
    reverse_fields = set(meta.backward_fk_fields) | set(meta.backward_o2o_fields)
    fields: list[str] = []
    for name, field in meta.fields_map.items():
        if name in reverse_fields:
            continue
        if bool(getattr(field, "generated", False)):
            continue
        if bool(getattr(field, "auto_now", False)) or bool(
            getattr(field, "auto_now_add", False)
        ):
            continue
        if name == meta.pk_attr and getattr(field, "default", None) is not None:
            continue
        fields.append(name)
    return tuple(fields)


def _serialise_api_value(value: object) -> object:
    """Convert common model values to representation-neutral primitives."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _serialise_api_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialise_api_value(item) for item in value]
    return value
