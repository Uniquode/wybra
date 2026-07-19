from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import uuid7

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRouter
from tests_support.content_types.models import Article

from wybra.api import ApiCapability, ApiPaging, ApiSettings, DefaultApiCapability
from wybra.content_types import ContentType, ContentTypeRegistry, ContentTypesCapability
from wybra.core.resources import PackageResourceSource
from wybra.core.routes import RouteType, route
from wybra.db import DatabaseCapability, fields
from wybra.db.models import Model
from wybra.forms import (
    Form,
    FormFieldOptions,
    ModelForm,
    TextField,
    forms_rendering_context,
)
from wybra.site import SiteCapabilityError
from wybra.template import DefaultTemplateCapability
from wybra.testing import (
    WybraTestClient,
    create_test_site,
    migrated_test_database,
)
from wybra.views import (
    APIResult,
    APIView,
    BulkDeleteAction,
    GenericView,
    HTMLView,
    ModelGenericView,
    TemplateResponse,
    TemplateView,
    View,
    ViewRouter,
    register_view,
)


def _request(method: str = "GET", *, app: FastAPI | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [],
            "app": app or FastAPI(),
        }
    )


def _json_request(
    values: dict[str, object],
    *,
    method: str,
    app: FastAPI,
) -> Request:
    body = json.dumps(values).encode("utf-8")
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/articles",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "app": app,
        },
        receive,
    )


@pytest.mark.anyio
async def test_view_dispatches_to_matching_http_method() -> None:
    class ExampleView(View):
        async def get(self, request: Request, *, item_id: str) -> dict[str, str]:
            return {"method": request.method, "item_id": item_id}

    response = await ExampleView().dispatch(_request(), item_id="42")

    assert isinstance(response, JSONResponse)
    assert response.body == b'{"method":"GET","item_id":"42"}'


@pytest.mark.anyio
async def test_view_returns_method_not_allowed_for_missing_handler() -> None:
    class ExampleView(View):
        def get(self, _request: Request) -> dict[str, str]:
            return {"ok": "yes"}

        def post(self, _request: Request) -> dict[str, str]:
            return {"ok": "yes"}

    response = await ExampleView().dispatch(_request("DELETE"))

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, POST"


def test_view_model_is_available_to_subclass() -> None:
    @dataclass(frozen=True, slots=True)
    class SomeDataModel:
        name: str

    class ModelView(View):
        model = SomeDataModel

    assert ModelView.model is SomeDataModel


def test_view_router_registers_decorated_view_relative_to_router_mount() -> None:
    router = ViewRouter()

    @router.view("/articles")
    class ArticleView(HTMLView):
        page_name = "<main>articles</main>"

    app = FastAPI()
    app.include_router(router, prefix="/admin")

    response = WybraTestClient(app).get("/admin/articles")

    assert response.status_code == 200
    assert response.text == "<main>articles</main>"


def test_register_view_supports_an_ordinary_api_router() -> None:
    router = APIRouter()

    @route("/status", RouteType.PAGE)
    class StatusView(HTMLView):
        page_name = "<main>ready</main>"

    register_view(router, StatusView)
    app = FastAPI()
    app.include_router(router, prefix="/health")

    response = WybraTestClient(app).get("/health/status")

    assert response.status_code == 200
    assert response.text == "<main>ready</main>"


def test_view_router_expands_a_generic_view_to_resource_routes() -> None:
    router = ViewRouter()

    @router.view("/articles")
    class ArticleView(GenericView):
        async def list_objects(self, request: Request) -> dict[str, str]:
            return {"action": "list"}

        async def retrieve_object(
            self, request: Request, object_id: str
        ) -> dict[str, str]:
            return {"action": "retrieve", "id": object_id}

        async def create_object(self, request: Request) -> dict[str, str]:
            return {"action": "create"}

        async def update_object(
            self, request: Request, object_id: str
        ) -> dict[str, str]:
            return {"action": "update", "id": object_id}

        async def delete_object(
            self, request: Request, object_id: str
        ) -> dict[str, str]:
            return {"action": "delete", "id": object_id}

        async def bulk_action(self, request: Request) -> dict[str, str]:
            return {"action": "bulk"}

    app = FastAPI()
    app.include_router(router, prefix="/admin")
    client = WybraTestClient(app)

    assert client.get("/admin/articles").json() == {"action": "list"}
    assert client.post("/admin/articles").json() == {"action": "create"}
    assert client.get("/admin/articles/42").json() == {
        "action": "retrieve",
        "id": "42",
    }
    assert client.patch("/admin/articles/42").json() == {
        "action": "update",
        "id": "42",
    }
    assert client.delete("/admin/articles/42").json() == {
        "action": "delete",
        "id": "42",
    }
    assert client.post("/admin/articles/bulk").json() == {"action": "bulk"}


def test_generic_view_respects_owning_router_dependencies() -> None:
    invoked = False

    async def deny() -> None:
        raise HTTPException(status_code=403, detail="Denied")

    router = ViewRouter(dependencies=[Depends(deny)])

    @router.view("/articles")
    class ArticleView(GenericView):
        async def list_objects(self, request: Request) -> dict[str, str]:
            nonlocal invoked
            invoked = True
            return {"action": "list"}

        async def retrieve_object(
            self, request: Request, object_id: str
        ) -> dict[str, str]:
            return {"action": "retrieve", "id": object_id}

        async def create_object(self, request: Request) -> dict[str, str]:
            return {"action": "create"}

        async def update_object(
            self, request: Request, object_id: str
        ) -> dict[str, str]:
            return {"action": "update", "id": object_id}

        async def delete_object(
            self, request: Request, object_id: str
        ) -> dict[str, str]:
            return {"action": "delete", "id": object_id}

        async def bulk_action(self, request: Request) -> dict[str, str]:
            return {"action": "bulk"}

    app = FastAPI()
    app.include_router(router)

    response = WybraTestClient(app).get("/articles")

    assert response.status_code == 403
    assert invoked is False


def test_generic_view_uses_the_shipped_workspace_template_by_default() -> None:
    assert GenericView.template == "views/generic/view.html"


@pytest.mark.anyio
async def test_shipped_generic_templates_render_through_jinja() -> None:
    class ArticleForm(Form):
        title = TextField()

    @dataclass(frozen=True, slots=True)
    class RenderedArticle:
        pk: str
        title: str

        def __str__(self) -> str:
            return self.title

    templates = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.template", directory="templates"),
            PackageResourceSource(package="wybra.forms", directory="templates"),
        )
    )
    form_context = forms_rendering_context(
        templates,
        {"csrf_field_name": "csrf_token", "csrf_token": "test-token"},
    )
    content_type = ContentType(
        identifier="articles.article",
        model=Article,
        verbose_name="Article",
        verbose_name_plural="Articles",
        actions=frozenset({"list", "create", "update", "delete"}),
    )
    article = RenderedArticle(pk="article-1", title="First article")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/articles",
            "query_string": b"highlight=article-1",
            "headers": [],
            "app": FastAPI(),
        }
    )
    page_context = {
        **form_context,
        "asset_url": lambda path: f"/assets/{path}",
        "bulk_action_url": "/articles/bulk",
        "bulk_actions": {"delete": object()},
        "collection_url": "/articles",
        "content_type": content_type,
        "create_form": ArticleForm(),
        "objects": (article,),
        "page_title": "Articles",
        "request": request,
        "route_name": "articles-collection",
    }

    workspace = await templates.render_template("views/generic/view.html", page_context)
    editor = await templates.render_template(
        "views/generic/editor.html",
        {
            **page_context,
            "delete_fragment_url": "/articles/article-1?delete=article-1",
            "form": ArticleForm(),
            "form_action": "/articles/article-1",
            "form_attr": {},
            "form_method": "patch",
        },
    )
    delete = await templates.render_template(
        "views/generic/delete.html",
        {
            **page_context,
            "delete_action": "/articles/article-1",
            "object": article,
        },
    )

    assert '<h1 id="generic-view-title">Articles</h1>' in workspace
    assert 'value="article-1"' in workspace
    assert 'name="csrf_token"' in workspace
    assert "data-highlighted" in workspace
    assert "data-wybra-generic-editor" in editor
    assert 'name="_method" type="hidden" value="PATCH"' in editor
    assert "data-wybra-generic-delete" in delete
    assert 'hx-delete="/articles/article-1"' in delete


def test_model_generic_view_uses_explicit_form_and_stable_generated_form() -> None:
    class GeneratedArticleView(ModelGenericView):
        model = Article

    class ArticleForm(ModelForm):
        class Meta:
            model = Article
            fields = ("title",)
            list_ordering = ("-title",)

    class OrderedArticleView(ModelGenericView):
        model = Article
        form = ArticleForm

    generated = GeneratedArticleView().get_form_class()

    assert GeneratedArticleView().get_form_class() is generated
    assert tuple(generated.Meta.fields) == ("title",)
    assert OrderedArticleView().get_form_class() is ArticleForm
    assert OrderedArticleView().get_list_ordering() == ("-title",)


def test_generated_model_form_uses_model_form_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MetadataArticleView(ModelGenericView):
        model = Article

    monkeypatch.setattr(Article.Meta, "form_fields", ("title",), raising=False)
    monkeypatch.setattr(
        Article.Meta,
        "form_options",
        {"title": FormFieldOptions(editable=False)},
        raising=False,
    )
    monkeypatch.setattr(Article.Meta, "ordering", ("-title",), raising=False)

    form = MetadataArticleView().get_form_class()()

    assert tuple(form.fields) == ("title",)
    assert form.fields["title"].disabled is True
    assert MetadataArticleView().get_list_ordering() == ("-title",)


def test_generated_form_fields_handle_managed_keys_and_relations() -> None:
    class Country(Model):
        code = fields.CharField(max_length=2, primary_key=True)
        name = fields.CharField(max_length=100)

    class Tag(Model):
        id = fields.UUIDField(primary_key=True)
        name = fields.CharField(max_length=100)

    class Place(Model):
        id = fields.UUIDField(primary_key=True)
        country = fields.ForeignKeyField("models.Country")
        tags = fields.ManyToManyField("models.Tag")
        name = fields.CharField(max_length=100)

    class CountryView(ModelGenericView):
        model = Country

    class PlaceView(ModelGenericView):
        model = Place

    assert CountryView().get_form_class().Meta.fields == ("code", "name")
    assert PlaceView().get_form_class().Meta.fields == ("country", "tags", "name")


def test_model_generic_view_serialises_portable_api_values() -> None:
    class Record(Model):
        id = fields.UUIDField(primary_key=True)
        created_at = fields.DatetimeField()
        due_date = fields.DateField()
        due_time = fields.TimeField()
        amount = fields.DecimalField(max_digits=6, decimal_places=2)

    class RecordView(ModelGenericView):
        model = Record

    record = Record(
        id=uuid7(),
        created_at=datetime(2026, 7, 19, 10, 30),
        due_date=date(2026, 7, 20),
        due_time=time(9, 45),
        amount=Decimal("12.30"),
    )

    assert RecordView().serialise_object(record) == {
        "id": str(record.id),
        "created_at": "2026-07-19T10:30:00+00:00",
        "due_date": "2026-07-20",
        "due_time": "09:45:00+00:00",
        "amount": "12.3",
    }


@pytest.mark.anyio
async def test_model_generic_view_lists_and_retrieves_api_records() -> None:
    class ArticleView(ModelGenericView):
        model = Article

    app = FastAPI()
    async with migrated_test_database(
        modules=("tests_support.content_types",)
    ) as database:
        site = create_test_site(
            {"app": {"modules": ("tests_support.content_types",)}},
            app=app,
        )
        site.provide_capability(DatabaseCapability, database.capability())
        site.provide_capability(
            ContentTypesCapability,
            ContentTypeRegistry.from_models(database.capability().models()),
        )
        site.provide_capability(ApiCapability, DefaultApiCapability(ApiSettings()))
        first = await Article.create(title="First")
        second = await Article.create(title="Second")

        collection = await ArticleView().dispatch(
            _request(app=app),
            _route_type=RouteType.API,
        )
        record = await ArticleView().dispatch(
            _request(app=app),
            _route_type=RouteType.API,
            id=str(second.id),
        )

    assert json.loads(collection.body)["data"] == [
        {"id": first.id, "title": "First"},
        {"id": second.id, "title": "Second"},
    ]
    assert json.loads(record.body)["data"] == {"id": second.id, "title": "Second"}


@pytest.mark.anyio
async def test_model_generic_view_creates_and_updates_records_from_api_values() -> None:
    class ArticleView(ModelGenericView):
        model = Article

    app = FastAPI()
    async with migrated_test_database(
        modules=("tests_support.content_types",)
    ) as database:
        site = create_test_site(
            {"app": {"modules": ("tests_support.content_types",)}},
            app=app,
        )
        site.provide_capability(DatabaseCapability, database.capability())
        site.provide_capability(
            ContentTypesCapability,
            ContentTypeRegistry.from_models(database.capability().models()),
        )
        site.provide_capability(ApiCapability, DefaultApiCapability(ApiSettings()))

        created = await ArticleView().dispatch(
            _json_request({"title": "First"}, method="POST", app=app),
            _route_type=RouteType.API,
            _collection_path="/articles",
        )
        created_id = json.loads(created.body)["data"]["id"]
        updated = await ArticleView().dispatch(
            _json_request({"title": "Updated"}, method="PATCH", app=app),
            _route_type=RouteType.API,
            _collection_path="/articles",
            id=str(created_id),
        )

    assert created.status_code == 201
    assert json.loads(updated.body)["data"] == {"id": created_id, "title": "Updated"}


@pytest.mark.anyio
async def test_model_generic_view_accepts_an_unchanged_api_update() -> None:
    class ArticleView(ModelGenericView):
        model = Article

    app = FastAPI()
    async with migrated_test_database(
        modules=("tests_support.content_types",)
    ) as database:
        site = create_test_site(
            {"app": {"modules": ("tests_support.content_types",)}},
            app=app,
        )
        site.provide_capability(DatabaseCapability, database.capability())
        site.provide_capability(
            ContentTypesCapability,
            ContentTypeRegistry.from_models(database.capability().models()),
        )
        site.provide_capability(ApiCapability, DefaultApiCapability(ApiSettings()))
        article = await Article.create(title="Unchanged")

        response = await ArticleView().dispatch(
            _json_request({"title": "Unchanged"}, method="PATCH", app=app),
            _route_type=RouteType.API,
            id=str(article.id),
        )

    assert response.status_code == 200
    assert json.loads(response.body)["data"] == {
        "id": article.id,
        "title": "Unchanged",
    }


@pytest.mark.anyio
async def test_model_generic_view_returns_api_validation_errors() -> None:
    class ArticleView(ModelGenericView):
        model = Article

    app = FastAPI()
    async with migrated_test_database(
        modules=("tests_support.content_types",)
    ) as database:
        site = create_test_site(
            {"app": {"modules": ("tests_support.content_types",)}},
            app=app,
        )
        site.provide_capability(DatabaseCapability, database.capability())
        site.provide_capability(
            ContentTypesCapability,
            ContentTypeRegistry.from_models(database.capability().models()),
        )
        site.provide_capability(ApiCapability, DefaultApiCapability(ApiSettings()))

        response = await ArticleView().dispatch(
            _json_request({}, method="POST", app=app),
            _route_type=RouteType.API,
            _collection_path="/articles",
        )

    assert response.status_code == 422
    assert json.loads(response.body)["error"]["code"] == "validation_error"


@pytest.mark.anyio
async def test_model_generic_view_deletes_only_confirmed_api_records() -> None:
    class ArticleView(ModelGenericView):
        model = Article

    app = FastAPI()
    async with migrated_test_database(
        modules=("tests_support.content_types",)
    ) as database:
        site = create_test_site(
            {"app": {"modules": ("tests_support.content_types",)}},
            app=app,
        )
        site.provide_capability(DatabaseCapability, database.capability())
        site.provide_capability(
            ContentTypesCapability,
            ContentTypeRegistry.from_models(database.capability().models()),
        )
        site.provide_capability(ApiCapability, DefaultApiCapability(ApiSettings()))
        article = await Article.create(title="First")

        denied = await ArticleView().dispatch(
            _json_request({}, method="DELETE", app=app),
            _route_type=RouteType.API,
            id=str(article.id),
        )
        deleted = await ArticleView().dispatch(
            _json_request({"confirm": True}, method="DELETE", app=app),
            _route_type=RouteType.API,
            id=str(article.id),
        )

        assert await Article.filter(id=article.id).exists() is False

    assert denied.status_code == 422
    assert json.loads(deleted.body)["data"] == {"id": str(article.id), "deleted": True}


@pytest.mark.anyio
async def test_model_generic_view_bulk_delete_uses_visible_selected_records() -> None:
    class ArticleView(ModelGenericView):
        model = Article
        bulk_actions = {"delete": BulkDeleteAction()}

    app = FastAPI()
    async with migrated_test_database(
        modules=("tests_support.content_types",)
    ) as database:
        site = create_test_site(
            {"app": {"modules": ("tests_support.content_types",)}},
            app=app,
        )
        site.provide_capability(DatabaseCapability, database.capability())
        site.provide_capability(
            ContentTypesCapability,
            ContentTypeRegistry.from_models(database.capability().models()),
        )
        site.provide_capability(ApiCapability, DefaultApiCapability(ApiSettings()))
        selected = await Article.create(title="Selected")
        retained = await Article.create(title="Retained")

        response = await ArticleView().dispatch(
            _json_request(
                {"action": "delete", "selected": [selected.id, 999], "confirm": True},
                method="POST",
                app=app,
            ),
            _route_type=RouteType.API,
            bulk=True,
        )

        assert await Article.filter(id=selected.id).exists() is False
        assert await Article.filter(id=retained.id).exists() is True

    assert json.loads(response.body)["data"] == {
        "affected_ids": [str(selected.id)],
        "skipped_ids": ["999"],
        "failed_ids": [],
    }


@pytest.mark.anyio
async def test_model_generic_view_html_workspace_preserves_collection_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ArticleView(ModelGenericView):
        model = Article

    captured: dict[str, object] = {}

    async def fake_render_page(
        _request: Request,
        template_name: str,
        context: dict[str, object],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        captured.update(template_name=template_name, context=context)
        return HTMLResponse("workspace", status_code=status_code)

    monkeypatch.setattr("wybra.views.templates.render_page", fake_render_page)
    app = FastAPI()
    async with migrated_test_database(
        modules=("tests_support.content_types",)
    ) as database:
        site = create_test_site(
            {"app": {"modules": ("tests_support.content_types",)}},
            app=app,
        )
        site.provide_capability(DatabaseCapability, database.capability())
        site.provide_capability(
            ContentTypesCapability,
            ContentTypeRegistry.from_models(database.capability().models()),
        )
        article = await Article.create(title="First")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/articles",
                "query_string": f"search=first&edit={article.id}".encode(),
                "headers": [],
                "app": app,
            }
        )

        response = await ArticleView().dispatch(
            request,
            _route_type=RouteType.PAGE,
            _collection_path="/articles",
        )

    context = captured["context"]
    assert response.status_code == 200
    assert captured["template_name"] == "views/generic/view.html"
    assert isinstance(context, dict)
    assert context["collection_url"] == "/articles?search=first"
    assert context["form_action"] == f"/articles/{article.id}?search=first"


@pytest.mark.anyio
async def test_html_view_renders_declared_page() -> None:
    class ExampleHTMLView(HTMLView):
        page_name = "<main>hello</main>"

    response = await ExampleHTMLView().dispatch(_request())

    assert isinstance(response, HTMLResponse)
    assert response.body == b"<main>hello</main>"


@pytest.mark.anyio
async def test_html_view_uses_overridable_page_hook() -> None:
    class ExampleHTMLView(HTMLView):
        page_name = "unused"

        def get_page(self) -> str:
            return "<main>overridden</main>"

    response = await ExampleHTMLView().dispatch(_request())

    assert response.body == b"<main>overridden</main>"


@pytest.mark.anyio
async def test_get_only_html_view_rejects_post() -> None:
    class ExampleHTMLView(HTMLView):
        page_name = "<main>hello</main>"

    response = await ExampleHTMLView().dispatch(_request("POST"))

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"


@pytest.mark.anyio
async def test_template_view_dispatches_template_response_with_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered: dict[str, Any] = {}

    async def fake_render_page(
        request: Request,
        template_name: str,
        context: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        rendered.update(
            request=request,
            template_name=template_name,
            context=context,
            status_code=status_code,
        )
        return HTMLResponse("<main>home</main>", status_code=status_code)

    monkeypatch.setattr("wybra.views.templates.render_page", fake_render_page)

    class ExampleTemplateView(TemplateView):
        template_name = "pages/home.html"

        async def get_context(
            self,
            context: dict[str, Any],
            request: Request,
            **kwargs: Any,
        ) -> dict[str, Any]:
            assert context == {}
            assert request.method == "GET"
            return {**context, "item_id": kwargs["item_id"]}

    request = _request()
    response = await ExampleTemplateView().dispatch(request, item_id="42")

    assert response.body == b"<main>home</main>"
    assert rendered == {
        "request": request,
        "template_name": "pages/home.html",
        "context": {"item_id": "42"},
        "status_code": 200,
    }


@pytest.mark.anyio
async def test_template_response_can_be_returned_by_any_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_render_page(
        _request: Request,
        template_name: str,
        _context: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        return HTMLResponse(template_name, status_code=status_code)

    monkeypatch.setattr("wybra.views.templates.render_page", fake_render_page)

    class ExampleView(View):
        def get(self, request: Request) -> TemplateResponse:
            return TemplateResponse(request, "pages/other.html", {})

    response = await ExampleView().dispatch(_request())

    assert response.body == b"pages/other.html"


def test_template_view_requires_a_declared_template() -> None:
    view = TemplateView()

    with pytest.raises(ValueError, match="template_name"):
        view.get_template()


@pytest.mark.anyio
async def test_api_view_renders_paged_api_result() -> None:
    class ExampleAPIView(APIView):
        def get(self, _request: Request) -> APIResult:
            return APIResult(
                data=[{"name": "Ada"}],
                paging=ApiPaging(cursor="abc", limit=25, has_more=False),
            )

    response = await ExampleAPIView(api=DefaultApiCapability(ApiSettings())).dispatch(
        _request()
    )

    assert isinstance(response, JSONResponse)
    assert response.body == (
        b'{"data":[{"name":"Ada"}],"links":[],"paging":{"cursor":"abc",'
        b'"limit":25,"has_more":false}}'
    )


@pytest.mark.anyio
async def test_api_view_can_delegate_final_formatting() -> None:
    class ApiFormatter(ApiCapability):
        def is_api_request(self, request: Request, *, route_type=None) -> bool:
            return True

        def response(self, data, *, status_code=200, headers=None, metadata=None):
            return JSONResponse({"wrapped": data}, status_code=status_code)

        def paged_response(
            self,
            items,
            *,
            paging,
            status_code=200,
            headers=None,
            metadata=None,
        ):
            return JSONResponse(
                {"wrapped": list(items), "links": len(paging.links)},
                status_code=status_code,
            )

        def error_response(self, error, *, status_code, headers=None):
            return JSONResponse({"error": error.message}, status_code=status_code)

        def status_response(
            self,
            *,
            status_code,
            message=None,
            headers=None,
            metadata=None,
        ):
            return JSONResponse({"status": message}, status_code=status_code)

        def validation_error_response(
            self,
            errors,
            *,
            status_code=422,
            headers=None,
        ):
            return JSONResponse({"errors": list(errors)}, status_code=status_code)

        def streaming_response(
            self,
            body,
            *,
            status_code=200,
            headers=None,
            media_type=None,
        ):
            return Response(status_code=status_code, headers=headers)

    class ExampleAPIView(APIView):
        def get(self, _request: Request) -> dict[str, str]:
            return {"ok": "yes"}

    api: ApiCapability = ApiFormatter()
    response = await ExampleAPIView(api=api).dispatch(_request())

    assert response.body == b'{"wrapped":{"ok":"yes"}}'


@pytest.mark.anyio
async def test_api_view_reports_missing_api_capability() -> None:
    class ExampleAPIView(APIView):
        def get(self, _request: Request) -> dict[str, str]:
            return {"ok": "yes"}

    with pytest.raises(SiteCapabilityError, match="configure wybra.api"):
        await ExampleAPIView().dispatch(_request())
