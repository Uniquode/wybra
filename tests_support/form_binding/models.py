"""Models that exercise form persistence against native migrations."""

from wybra.db import VersionField, fields
from wybra.db.models import Model


class FormAddress(Model):
    id = fields.IntField(primary_key=True)
    street = fields.CharField(max_length=120)

    class Meta:
        table = "test_form_address"


class FormPhone(Model):
    id = fields.IntField(primary_key=True)
    number = fields.CharField(max_length=120)

    class Meta:
        table = "test_form_phone"


class FormContact(Model):
    id = fields.IntField(primary_key=True)
    address = fields.ForeignKeyField("tests_support_form_binding.FormAddress")
    phone = fields.ForeignKeyField("tests_support_form_binding.FormPhone")
    name = fields.CharField(max_length=120, unique=True)

    class Meta:
        table = "test_form_contact"


class FormLabel(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=120)

    class Meta:
        table = "test_form_label"


class FormDocument(Model):
    id = fields.IntField(primary_key=True)
    labels = fields.ManyToManyField("tests_support_form_binding.FormLabel")

    class Meta:
        table = "test_form_document"


class FormVersionedRecord(Model):
    id = fields.IntField(primary_key=True)
    data = fields.CharField(max_length=120)
    enabled = fields.BooleanField(default=True)
    version = VersionField()

    class Meta:
        table = "test_form_versioned_record"


__all__ = (
    "FormAddress",
    "FormContact",
    "FormDocument",
    "FormLabel",
    "FormPhone",
    "FormVersionedRecord",
)
