from tortoise import fields
from tortoise.models import Model


class Article(Model):
    title = fields.CharField(max_length=200)


class Person(Model):
    name = fields.CharField(max_length=200)

    class Meta:
        verbose_name_plural = "People"
        content_exclude = {"delete"}
