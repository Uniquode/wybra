from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from inspect import signature
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request

from wybra.content_types import ContentType, ContentTypesCapability
from wybra.core.exceptions import Http403
from wybra.db.models import Model
from wybra.scopes import (
    STANDARD_SCOPE_ACTIONS,
    DiscoveredScope,
    ScopeAction,
    ScopeCatalogueCapability,
    ScopeDeclaration,
    ScopeDeclarationError,
    ScopeDeclarationOrigin,
    ScopeGrantsCapability,
    ScopeSubject,
    access_decision,
    declared_scope_identifiers,
    discover_declared_scopes,
    get_scope_declaration,
    missing_scope_catalogue_entries,
    missing_scope_requirements,
    resolve_operation_requirements,
    resolve_scope_requirements,
    scope_dependency,
    scopes,
    validate_site_scope_catalogue,
)
from wybra.views import BulkDeleteAction, ModelGenericView, View, ViewRouter


class _ContentTypes:
    def __init__(self, content_type: ContentType) -> None:
        self._content_type = content_type

    def all(self) -> tuple[ContentType, ...]:
        return (self._content_type,)

    def for_identifier(self, identifier: str) -> ContentType:
        if identifier != self._content_type.identifier:
            raise LookupError(identifier)
        return self._content_type

    def for_model(self, model: type[Model]) -> ContentType:
        if model is not self._content_type.model:
            raise LookupError(model)
        return self._content_type


def _content_types(
    model: type[Model],
    *,
    identifier: str = "articles.article",
    actions: frozenset[str] = frozenset({"list", "view", "create", "update", "delete"}),
) -> ContentTypesCapability:
    return cast(
        ContentTypesCapability,
        _ContentTypes(
            ContentType(
                identifier=identifier,
                model=model,
                verbose_name="Article",
                verbose_name_plural="Articles",
                actions=actions,
            )
        ),
    )


class _ScopeGrants:
    def __init__(self, subject: ScopeSubject) -> None:
        self.subject = subject
        self.calls = 0

    async def resolve_scope_subject(self, _request: Request) -> ScopeSubject:
        self.calls += 1
        return self.subject


class _ScopeCatalogue:
    def __init__(self, identifiers: tuple[str, ...]) -> None:
        self.identifiers = identifiers
        self.calls = 0

    async def list_scope_identifiers(self) -> tuple[str, ...]:
        self.calls += 1
        return self.identifiers


def _policy_request(
    *,
    grants: _ScopeGrants | None,
    content_types: ContentTypesCapability,
) -> Request:
    from wybra.testing import create_test_site

    app = FastAPI()
    site = create_test_site({"app": {"modules": ()}}, app=app)
    if grants is not None:
        site.provide_capability(
            ScopeGrantsCapability,
            cast(ScopeGrantsCapability, grants),
        )
    site.provide_capability(ContentTypesCapability, content_types)
    return Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/articles/1",
            "query_string": b"",
            "headers": [],
            "app": app,
        }
    )


def test_endpoint_scope_declaration_is_immutable_and_preserves_callable() -> None:
    async def export_report(report_id: str) -> str:
        return report_id

    original_signature = signature(export_report)
    decorated = scopes("reports.export")(export_report)

    assert decorated is export_report
    assert signature(decorated) == original_signature
    assert get_scope_declaration(decorated) == ScopeDeclaration(
        requires=("reports.export",)
    )
    with pytest.raises(FrozenInstanceError):
        get_scope_declaration(decorated).requires = ()  # type: ignore[misc]


def test_target_sensitive_positionals_and_explicit_channels() -> None:
    @scopes("update", "delete")
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    @scopes(requires=("reports.export",))
    async def export_report() -> None:
        return None

    @scopes("backoffice.access", actions=("update",))
    class ProtectedModelView(ModelGenericView):
        model = ProtectedModel

    assert STANDARD_SCOPE_ACTIONS == (
        ScopeAction.LIST,
        ScopeAction.VIEW,
        ScopeAction.CREATE,
        ScopeAction.UPDATE,
        ScopeAction.DELETE,
        ScopeAction.MANAGE,
    )
    assert get_scope_declaration(ProtectedModel) == ScopeDeclaration(
        actions=(ScopeAction.UPDATE, ScopeAction.DELETE)
    )
    assert get_scope_declaration(export_report) == ScopeDeclaration(
        requires=("reports.export",)
    )
    assert get_scope_declaration(ProtectedModelView) == ScopeDeclaration(
        requires=("backoffice.access",),
        actions=(ScopeAction.UPDATE,),
    )


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (None, ()),
        (True, STANDARD_SCOPE_ACTIONS),
        (("update",), (ScopeAction.UPDATE,)),
        (
            ("-delete",),
            tuple(
                action
                for action in STANDARD_SCOPE_ACTIONS
                if action is not ScopeAction.DELETE
            ),
        ),
    ],
)
def test_model_meta_scopes_normalise_action_selection(
    metadata: object,
    expected: tuple[ScopeAction, ...],
) -> None:
    class ProtectedModel(Model):
        class Meta:
            abstract = True
            scopes = metadata

    assert get_scope_declaration(ProtectedModel) == ScopeDeclaration(actions=expected)


def test_empty_decorator_is_a_no_op_and_allows_meta_scopes() -> None:
    @scopes()
    class ProtectedModel(Model):
        class Meta:
            abstract = True
            scopes = ("view",)

    assert get_scope_declaration(ProtectedModel) == ScopeDeclaration(
        actions=(ScopeAction.VIEW,)
    )


@pytest.mark.parametrize("metadata", [("view",), (), None])
def test_non_empty_decorator_and_explicit_meta_scopes_are_ambiguous(
    metadata: object,
) -> None:
    @scopes("update")
    class ProtectedModel(Model):
        class Meta:
            abstract = True
            scopes = metadata

    with pytest.raises(ScopeDeclarationError, match="both.*decorator.*Meta.scopes"):
        get_scope_declaration(ProtectedModel)


@pytest.mark.parametrize(
    ("decorator", "message"),
    [
        (lambda: scopes(actions="update"), "actions=.*iterable"),
        (lambda: scopes(actions=cast(Any, True)), "actions=.*iterable"),
        (lambda: scopes(actions=("archive",)), "Unknown scope action"),
        (lambda: scopes(actions=("-",)), "Unknown scope action"),
        (
            lambda: scopes(actions=("update", "-delete")),
            "Inclusive and exclusion-prefixed",
        ),
        (lambda: scopes(requires=cast(Any, 1)), "requires=.*iterable"),
        (lambda: scopes(requires=("",)), "non-blank"),
        (lambda: scopes(requires=("catalog.access", "catalog.access")), "duplicates"),
    ],
)
def test_invalid_declaration_values_are_rejected(
    decorator: Callable[[], Callable[[type[Model]], type[Model]]],
    message: str,
) -> None:
    class TargetModel(Model):
        class Meta:
            abstract = True

    with pytest.raises(ScopeDeclarationError, match=message):
        decorator()(TargetModel)


def test_duplicate_target_default_forms_and_unbound_actions_are_rejected() -> None:
    with pytest.raises(ScopeDeclarationError, match="positional.*actions"):

        @scopes("update", actions=("delete",))
        class ConflictingModel(Model):
            class Meta:
                abstract = True

    with pytest.raises(ScopeDeclarationError, match="positional.*requires"):

        @scopes("reports.export", requires=("reports.read",))
        async def conflicting_endpoint() -> None:
            return None

    with pytest.raises(ScopeDeclarationError, match="model-backed"):
        scopes(actions=("update",))(lambda: None)


def test_view_declarations_are_inherited_without_mutation() -> None:
    @scopes("backoffice.access")
    class ProtectedView(View):
        pass

    class InheritedView(ProtectedView):
        pass

    assert get_scope_declaration(InheritedView) is get_scope_declaration(ProtectedView)


def test_resolved_model_and_view_requirements_are_additive_and_stable() -> None:
    @scopes("update", requires=("catalog.access",))
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    @scopes("backoffice.access", actions=("update",))
    class ProtectedModelView(ModelGenericView):
        model = ProtectedModel

    content_types = _content_types(ProtectedModel)

    assert resolve_scope_requirements(
        ProtectedModel,
        ScopeAction.VIEW,
        content_types,
    ) == ("catalog.access",)
    assert resolve_scope_requirements(
        ProtectedModel,
        ScopeAction.UPDATE,
        content_types,
    ) == ("catalog.access", "articles.article.update")
    assert resolve_scope_requirements(
        ProtectedModelView,
        ScopeAction.UPDATE,
        content_types,
    ) == ("backoffice.access", "articles.article.update")
    assert resolve_operation_requirements(
        ProtectedModel,
        ProtectedModelView,
        ScopeAction.UPDATE,
        content_types,
    ) == (
        "catalog.access",
        "articles.article.update",
        "backoffice.access",
    )
    assert get_scope_declaration(ProtectedModel) == ScopeDeclaration(
        requires=("catalog.access",),
        actions=(ScopeAction.UPDATE,),
    )


def test_exclusion_omits_only_its_layer_requirement() -> None:
    @scopes("-delete")
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    @scopes(actions=("delete",))
    class ProtectedModelView(ModelGenericView):
        model = ProtectedModel

    content_types = _content_types(ProtectedModel)

    assert (
        resolve_scope_requirements(
            ProtectedModel,
            ScopeAction.DELETE,
            content_types,
        )
        == ()
    )
    assert resolve_operation_requirements(
        ProtectedModel,
        ProtectedModelView,
        ScopeAction.DELETE,
        content_types,
    ) == ("articles.article.delete",)


def test_registered_content_type_grant_satisfies_descendants_at_dot_boundary() -> None:
    class Article(Model):
        class Meta:
            abstract = True

    content_types = _content_types(Article)

    assert (
        missing_scope_requirements(
            ("articles.article.update", "articles.article.export"),
            ("articles.article",),
            content_types,
        )
        == ()
    )
    assert missing_scope_requirements(
        ("articles.articleish.export", "reports.export"),
        ("articles.article", "reports.export"),
        content_types,
    ) == ("articles.articleish.export",)
    assert missing_scope_requirements(
        ("reports.export.detail",),
        ("reports.export",),
        content_types,
    ) == ("reports.export.detail",)


@pytest.mark.anyio
async def test_access_decision_uses_group_effective_scopes_and_request_cache() -> None:
    @scopes("update", requires=("catalog.access",))
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    @scopes("backoffice.access", actions=("update",))
    class ProtectedView(ModelGenericView):
        model = ProtectedModel

    actor = SimpleNamespace(is_admin=True, is_superuser=True)
    grants = _ScopeGrants(
        ScopeSubject(
            actor=actor,
            granted_scopes=(
                "articles.article",
                "backoffice.access",
                "catalog.access",
            ),
            groups=("editors",),
        )
    )
    content_types = _content_types(ProtectedModel)
    request = _policy_request(
        grants=grants,
        content_types=content_types,
    )

    first = await access_decision(
        request,
        target=ProtectedView,
        model=ProtectedModel,
        action=ScopeAction.UPDATE,
    )
    second = await access_decision(
        request,
        target=ProtectedView,
        model=ProtectedModel,
        action=ScopeAction.UPDATE,
    )

    assert first.allowed is True
    assert first.model_requirements == (
        "catalog.access",
        "articles.article.update",
    )
    assert first.view_requirements == (
        "backoffice.access",
        "articles.article.update",
    )
    assert first.required_scopes == (
        "catalog.access",
        "articles.article.update",
        "backoffice.access",
    )
    assert first.missing_scopes == ()
    assert second == first
    assert grants.calls == 1

    grants.subject = ScopeSubject(actor=actor, granted_scopes=(), groups=())
    later_request = _policy_request(
        grants=grants,
        content_types=content_types,
    )
    denied = await access_decision(
        later_request,
        target=ProtectedView,
        model=ProtectedModel,
        action=ScopeAction.UPDATE,
    )

    assert denied.allowed is False
    assert denied.missing_scopes == denied.required_scopes
    assert grants.calls == 2


@pytest.mark.anyio
async def test_anonymous_access_has_no_grants_without_persistence_lookup() -> None:
    @scopes("reports.export")
    class ProtectedView(View):
        pass

    request = _policy_request(
        grants=None,
        content_types=_content_types(cast(type[Model], Model)),
    )

    decision = await access_decision(
        request,
        target=ProtectedView,
        action=ScopeAction.VIEW,
    )

    assert decision.allowed is False
    assert decision.granted_scopes == ()
    assert decision.missing_scopes == ("reports.export",)


@pytest.mark.anyio
async def test_protected_view_denies_before_handler_and_allows_granted_user() -> None:
    invoked = False

    @scopes("reports.export")
    class ProtectedView(View):
        async def get(self, _request: Request) -> dict[str, bool]:
            nonlocal invoked
            invoked = True
            return {"exported": True}

    actor = SimpleNamespace()
    grants = _ScopeGrants(ScopeSubject(actor=actor, granted_scopes=(), groups=()))
    denied_request = _policy_request(
        grants=grants,
        content_types=_content_types(cast(type[Model], Model)),
    )
    denied_request.scope["method"] = "GET"

    with pytest.raises(Http403):
        await ProtectedView().dispatch(denied_request)
    assert invoked is False

    grants.subject = ScopeSubject(
        actor=actor,
        granted_scopes=("reports.export",),
        groups=("reporters",),
    )
    allowed_request = _policy_request(
        grants=grants,
        content_types=_content_types(cast(type[Model], Model)),
    )
    allowed_request.scope["method"] = "GET"

    response = await ProtectedView().dispatch(allowed_request)

    assert response.status_code == 200
    assert invoked is True


@pytest.mark.anyio
async def test_endpoint_dependency_enforces_attached_literal_requirements() -> None:
    @scopes("reports.export")
    async def export_report() -> None:
        return None

    actor = SimpleNamespace()
    grants = _ScopeGrants(ScopeSubject(actor=actor, granted_scopes=(), groups=()))
    denied_request = _policy_request(
        grants=grants,
        content_types=_content_types(cast(type[Model], Model)),
    )
    dependency = scope_dependency(export_report)

    with pytest.raises(Http403):
        await dependency(denied_request)

    grants.subject = ScopeSubject(
        actor=actor,
        granted_scopes=("reports.export",),
        groups=("reporters",),
    )
    allowed_request = _policy_request(
        grants=grants,
        content_types=_content_types(cast(type[Model], Model)),
    )

    decision = await dependency(allowed_request)

    assert decision.allowed is True
    assert decision.required_scopes == ("reports.export",)


@pytest.mark.anyio
async def test_single_and_bulk_delete_deny_before_body_access() -> None:
    @scopes("delete")
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    class ProtectedView(ModelGenericView):
        model = ProtectedModel
        bulk_actions = {"delete": BulkDeleteAction()}

    grants = _ScopeGrants(
        ScopeSubject(actor=SimpleNamespace(), granted_scopes=(), groups=())
    )
    base_request = _policy_request(
        grants=grants,
        content_types=_content_types(ProtectedModel),
    )
    body_read = False

    async def receive() -> dict[str, object]:
        nonlocal body_read
        body_read = True
        raise AssertionError("Denied scope policy must run before body access.")

    item_request = Request(
        {**base_request.scope, "method": "DELETE", "path": "/articles/1"},
        receive,
    )
    bulk_request = Request(
        {
            **base_request.scope,
            "method": "POST",
            "path": "/articles/bulk/delete",
        },
        receive,
    )

    with pytest.raises(Http403):
        await ProtectedView().dispatch(item_request, id="1")
    with pytest.raises(Http403):
        await ProtectedView().dispatch(bulk_request, action="delete")

    assert body_read is False


@pytest.mark.anyio
async def test_unknown_bulk_action_is_rejected_before_scope_or_body_access() -> None:
    @scopes("manage")
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    class ProtectedView(ModelGenericView):
        model = ProtectedModel

        async def _request_validation_response(
            self,
            _request: Request,
            _error: ValueError,
        ):
            from fastapi.responses import Response

            return Response(status_code=422)

    grants = _ScopeGrants(
        ScopeSubject(actor=SimpleNamespace(), granted_scopes=(), groups=())
    )
    base_request = _policy_request(
        grants=grants,
        content_types=_content_types(ProtectedModel),
    )
    body_read = False

    async def receive() -> dict[str, object]:
        nonlocal body_read
        body_read = True
        raise AssertionError("Unknown actions must be rejected before body access.")

    request = Request(
        {
            **base_request.scope,
            "method": "POST",
            "path": "/articles/bulk/archive",
        },
        receive,
    )

    response = await ProtectedView().dispatch(request, action="archive")

    assert response.status_code == 422
    assert body_read is False
    assert grants.calls == 0


@pytest.mark.anyio
async def test_object_check_runs_only_after_declarative_scope_success() -> None:
    @scopes("update")
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    actor = SimpleNamespace()
    grants = _ScopeGrants(ScopeSubject(actor=actor, granted_scopes=(), groups=()))
    content_types = _content_types(ProtectedModel)
    request = _policy_request(
        grants=grants,
        content_types=content_types,
    )
    checked: list[tuple[object, ScopeAction, object]] = []
    record = object()

    async def deny_object(
        current_user: object,
        action: ScopeAction | None,
        current_record: object,
    ) -> bool:
        assert action is not None
        checked.append((current_user, action, current_record))
        return False

    denied_by_scope = await access_decision(
        request,
        target=ProtectedModel,
        action=ScopeAction.UPDATE,
        record=record,
        object_check=deny_object,
    )

    assert denied_by_scope.allowed is False
    assert denied_by_scope.object_allowed is None
    assert checked == []

    grants.subject = ScopeSubject(
        actor=actor,
        granted_scopes=("articles.article.update",),
        groups=("editors",),
    )
    allowed_scope_request = _policy_request(
        grants=grants,
        content_types=content_types,
    )
    denied_by_object = await access_decision(
        allowed_scope_request,
        target=ProtectedModel,
        action=ScopeAction.UPDATE,
        record=record,
        object_check=deny_object,
    )

    assert denied_by_object.allowed is False
    assert denied_by_object.missing_scopes == ()
    assert denied_by_object.object_allowed is False
    assert checked == [(actor, ScopeAction.UPDATE, record)]


def test_declared_scope_discovery_preserves_origins_and_view_targets() -> None:
    @scopes("update", requires=("catalog.access",))
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    router = ViewRouter()

    @router.view("/articles")
    @scopes("backoffice.access", actions=("delete",))
    class ProtectedView(ModelGenericView):
        model = ProtectedModel

    @router.get("/reports/export")
    @scopes("reports.export")
    async def export_report() -> None:
        return None

    app = FastAPI()
    app.include_router(router)
    content_types = _content_types(ProtectedModel)

    discovered = discover_declared_scopes(app, content_types)

    assert discovered == (
        DiscoveredScope(
            identifier="articles.article",
            origin=ScopeDeclarationOrigin.AGGREGATE,
            target=f"{ProtectedModel.__module__}.{ProtectedModel.__qualname__}",
            content_type="articles.article",
            aggregate=True,
        ),
        DiscoveredScope(
            identifier="articles.article.delete",
            origin=ScopeDeclarationOrigin.DERIVED,
            target=f"{ProtectedView.__module__}.{ProtectedView.__qualname__}",
            content_type="articles.article",
            action=ScopeAction.DELETE,
        ),
        DiscoveredScope(
            identifier="articles.article.update",
            origin=ScopeDeclarationOrigin.DERIVED,
            target=f"{ProtectedModel.__module__}.{ProtectedModel.__qualname__}",
            content_type="articles.article",
            action=ScopeAction.UPDATE,
        ),
        DiscoveredScope(
            identifier="backoffice.access",
            origin=ScopeDeclarationOrigin.LITERAL,
            target=f"{ProtectedView.__module__}.{ProtectedView.__qualname__}",
            content_type="articles.article",
        ),
        DiscoveredScope(
            identifier="catalog.access",
            origin=ScopeDeclarationOrigin.LITERAL,
            target=f"{ProtectedModel.__module__}.{ProtectedModel.__qualname__}",
            content_type="articles.article",
        ),
        DiscoveredScope(
            identifier="reports.export",
            origin=ScopeDeclarationOrigin.LITERAL,
            target=f"{export_report.__module__}.{export_report.__qualname__}",
        ),
    )
    assert declared_scope_identifiers(discovered) == (
        "articles.article.delete",
        "articles.article.update",
        "backoffice.access",
        "catalog.access",
        "reports.export",
    )


def test_missing_catalogue_entries_are_distinct_and_do_not_require_aggregates() -> None:
    discovered = (
        DiscoveredScope(
            identifier="articles.article",
            origin=ScopeDeclarationOrigin.AGGREGATE,
            target="tests.Article",
            content_type="articles.article",
            aggregate=True,
        ),
        DiscoveredScope(
            identifier="articles.article.update",
            origin=ScopeDeclarationOrigin.DERIVED,
            target="tests.Article",
            content_type="articles.article",
            action=ScopeAction.UPDATE,
        ),
        DiscoveredScope(
            identifier="reports.export",
            origin=ScopeDeclarationOrigin.LITERAL,
            target="tests.export_report",
        ),
        DiscoveredScope(
            identifier="reports.export",
            origin=ScopeDeclarationOrigin.LITERAL,
            target="tests.ExportView",
        ),
    )

    assert missing_scope_catalogue_entries(
        discovered,
        persisted_identifiers=("articles.article.update",),
    ) == ("reports.export",)


@pytest.mark.anyio
async def test_site_catalogue_validation_uses_provider_without_mutation() -> None:
    @scopes("update")
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    @scopes("reports.export")
    async def export_report() -> None:
        return None

    from wybra.testing import create_test_site

    app = FastAPI()
    app.get("/reports/export")(export_report)
    site = create_test_site({"app": {"modules": ()}}, app=app)
    site.provide_capability(
        ContentTypesCapability,
        _content_types(ProtectedModel),
    )
    catalogue = _ScopeCatalogue(("articles.article.update",))
    site.provide_capability(
        ScopeCatalogueCapability,
        cast(ScopeCatalogueCapability, catalogue),
    )

    missing = await validate_site_scope_catalogue(site)

    assert missing == ("reports.export",)
    assert catalogue.identifiers == ("articles.article.update",)
    assert catalogue.calls == 1


@pytest.mark.anyio
async def test_generic_context_hides_controls_from_one_cached_visibility_map() -> None:
    @scopes("create", "update", "delete", "manage")
    class ProtectedModel(Model):
        class Meta:
            abstract = True

    class UpdateBulkAction:
        scope_action = ScopeAction.UPDATE

    class ProtectedView(ModelGenericView):
        model = ProtectedModel
        bulk_actions = {
            "delete": BulkDeleteAction(),
            "publish": object(),
            "update": UpdateBulkAction(),
        }

    grants = _ScopeGrants(
        ScopeSubject(
            actor=SimpleNamespace(),
            granted_scopes=(
                "articles.article.create",
                "articles.article.update",
            ),
            groups=("editors",),
        )
    )
    request = _policy_request(
        grants=grants,
        content_types=_content_types(ProtectedModel),
    )
    create_form = object()
    view = ProtectedView()
    view._collection_path = "/articles"

    context = await view.get_context(
        {"create_form": create_form},
        request,
    )

    assert context["create_form"] is create_form
    assert context["bulk_actions"] == {"update": ProtectedView.bulk_actions["update"]}
    assert context["scope_visibility"] == {
        "list": True,
        "view": True,
        "create": True,
        "update": True,
        "delete": False,
        "manage": False,
    }
    assert grants.calls == 1


@pytest.mark.anyio
async def test_generic_context_intersects_scope_visibility_with_content_actions() -> (
    None
):
    class ReadOnlyModel(Model):
        class Meta:
            abstract = True

    class ReadOnlyView(ModelGenericView):
        model = ReadOnlyModel
        bulk_actions = {"delete": BulkDeleteAction()}

    request = _policy_request(
        grants=None,
        content_types=_content_types(
            ReadOnlyModel,
            actions=frozenset({"list", "view"}),
        ),
    )

    context = await ReadOnlyView().get_context(
        {"create_form": object()},
        request,
    )

    assert "create_form" not in context
    assert context["bulk_actions"] == {}
    assert context["scope_visibility"] == {
        "list": True,
        "view": True,
        "create": False,
        "update": False,
        "delete": False,
        "manage": True,
    }


@pytest.mark.anyio
async def test_generic_dispatch_rejects_unavailable_content_action_before_handler() -> (
    None
):
    class ReadOnlyModel(Model):
        class Meta:
            abstract = True

    class ReadOnlyView(ModelGenericView):
        model = ReadOnlyModel

    request = _policy_request(
        grants=None,
        content_types=_content_types(
            ReadOnlyModel,
            actions=frozenset({"list", "view"}),
        ),
    )

    response = await ReadOnlyView().dispatch(request, id="1")

    assert response.status_code == 405


def test_invalid_bulk_scope_action_is_rejected_during_registration() -> None:
    class Article(Model):
        class Meta:
            abstract = True

    class InvalidBulkAction:
        scope_action = "archive"

    router = ViewRouter()

    with pytest.raises(
        ScopeDeclarationError,
        match="Bulk action 'archive'.*unknown scope action",
    ):

        @router.view("/articles")
        class InvalidView(ModelGenericView):
            model = Article
            bulk_actions = {"archive": InvalidBulkAction()}


@pytest.mark.parametrize(
    ("method", "kwargs", "expected"),
    [
        ("GET", {}, ScopeAction.LIST),
        ("GET", {"id": "1"}, ScopeAction.VIEW),
        ("POST", {}, ScopeAction.CREATE),
        ("PATCH", {"id": "1"}, ScopeAction.UPDATE),
        ("DELETE", {"id": "1"}, ScopeAction.DELETE),
        ("POST", {"action": "delete"}, ScopeAction.DELETE),
        ("POST", {"action": "publish"}, ScopeAction.MANAGE),
        ("POST", {"action": "update"}, ScopeAction.UPDATE),
    ],
)
def test_generic_route_operations_map_to_canonical_actions(
    method: str,
    kwargs: dict[str, str],
    expected: ScopeAction,
) -> None:
    class UpdateBulkAction:
        scope_action = ScopeAction.UPDATE

    class ProtectedView(ModelGenericView):
        bulk_actions = {
            "delete": BulkDeleteAction(),
            "publish": object(),
            "update": UpdateBulkAction(),
        }

    assert (
        ProtectedView().scope_action(_policy_method_request(method), **kwargs)
        is expected
    )


def _policy_method_request(method: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/articles",
            "headers": [],
            "app": FastAPI(),
        }
    )
