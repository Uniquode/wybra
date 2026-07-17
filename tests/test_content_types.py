from __future__ import annotations

import pytest
from tests_support.content_types.models import Article, Person

from wybra.content_types import (
    ContentTypeError,
    ContentTypeRegistry,
    ContentTypesCapability,
    UnknownContentTypeError,
)
from wybra.site import get_site
from wybra.testing import (
    WybraTestClient,
    application_test_config,
    create_test_application,
    migrated_test_database,
)


@pytest.mark.anyio
async def test_registry_discovers_finalised_models_and_resolves_both_directions() -> (
    None
):
    async with migrated_test_database(modules=("tests_support.content_types",)):
        registry = ContentTypeRegistry.from_models((Article, Person))

    article = registry.for_model(Article)

    assert article.identifier == "tests_support_content_types.article"
    assert article.verbose_name == "Article"
    assert article.verbose_name_plural == "Articles"
    assert article.actions == frozenset({"list", "view", "create", "update", "delete"})
    assert registry.for_identifier(article.identifier) is article

    person = registry.for_model(Person)
    assert person.verbose_name_plural == "People"
    assert person.actions == frozenset({"list", "view", "create", "update"})


@pytest.mark.anyio
async def test_database_capability_exposes_configured_finalised_models() -> None:
    async with migrated_test_database(
        modules=("tests_support.content_types",)
    ) as database:
        models = database.capability().models()

    assert {Article, Person} <= set(models)


@pytest.mark.anyio
async def test_site_lifecycle_finalises_content_types_capability() -> None:
    application = create_test_application(
        application_test_config(
            modules=(
                "wybra.db",
                "wybra.content_types",
                "tests_support.content_types",
            )
        )
    )
    async with WybraTestClient(application):
        site = get_site(application)
        content_types = site.require_capability(ContentTypesCapability)

        assert content_types.for_model(Article).verbose_name_plural == "Articles"
        assert content_types.for_model(Person).actions == frozenset(
            {"list", "view", "create", "update"}
        )


@pytest.mark.anyio
async def test_identifier_is_stable_when_model_class_name_changes() -> None:
    async with migrated_test_database(modules=("tests_support.content_types",)):
        original_identifier = (
            ContentTypeRegistry.from_models((Article,)).for_model(Article).identifier
        )

        class RenamedArticle(Article):
            class Meta:
                abstract = False
                app = Article._meta.app
                table = Article._meta.db_table

        renamed_content_type = ContentTypeRegistry.from_models(
            (RenamedArticle,)
        ).for_model(RenamedArticle)
        renamed_identifier = renamed_content_type.identifier

    assert renamed_identifier == original_identifier


@pytest.mark.anyio
async def test_registry_derives_title_case_labels_and_meta_overrides() -> None:
    async with migrated_test_database(modules=("tests_support.content_types",)):

        class APIKey(Article):
            class Meta:
                abstract = False
                app = Article._meta.app
                table = "api_key"

        class BlogPost(Article):
            class Meta:
                abstract = False
                app = Article._meta.app
                table = "blog_post"

        class CustomResource(Article):
            class Meta:
                abstract = False
                app = Article._meta.app
                table = "custom_resource"
                verbose_name = "Custom Resource"
                content_actions = {"list", "view", "delete"}
                content_exclude = {"delete"}

        registry = ContentTypeRegistry.from_models((APIKey, BlogPost, CustomResource))

    assert registry.for_model(APIKey).verbose_name == "API Key"
    assert registry.for_model(BlogPost).verbose_name == "Blog Post"
    custom_resource = registry.for_model(CustomResource)
    assert custom_resource.verbose_name == "Custom Resource"
    assert custom_resource.actions == frozenset({"list", "view"})


@pytest.mark.anyio
async def test_registry_rejects_invalid_metadata() -> None:
    async with migrated_test_database(modules=("tests_support.content_types",)):

        class UnknownAction(Article):
            class Meta:
                abstract = False
                app = Article._meta.app
                table = "unknown_action"
                content_actions = {"archive"}

        with pytest.raises(ContentTypeError, match="unknown content action.*archive"):
            ContentTypeRegistry.from_models((UnknownAction,))

        class EmptyVerboseName(Article):
            class Meta:
                abstract = False
                app = Article._meta.app
                table = "empty_verbose_name"
                verbose_name = ""

        with pytest.raises(ContentTypeError, match="Meta.verbose_name"):
            ContentTypeRegistry.from_models((EmptyVerboseName,))

        class DottedTable(Article):
            class Meta:
                abstract = False
                app = Article._meta.app
                table = "dotted.table"

        with pytest.raises(
            ContentTypeError, match="schema and table names must not contain dots"
        ):
            ContentTypeRegistry.from_models((DottedTable,))

        class DuplicateArticle(Article):
            class Meta:
                abstract = False
                app = Article._meta.app
                table = Article._meta.db_table

        with pytest.raises(ContentTypeError, match="Duplicate content type identifier"):
            ContentTypeRegistry.from_models((Article, DuplicateArticle))


@pytest.mark.anyio
async def test_registry_reports_unknown_model_and_identifier() -> None:
    async with migrated_test_database(modules=("tests_support.content_types",)):
        registry = ContentTypeRegistry.from_models((Article,))

    with pytest.raises(
        UnknownContentTypeError,
        match="Unknown content type identifier",
    ):
        registry.for_identifier("unknown.model")
    with pytest.raises(
        UnknownContentTypeError,
        match="Model has no registered content type",
    ):
        registry.for_model(Person)
