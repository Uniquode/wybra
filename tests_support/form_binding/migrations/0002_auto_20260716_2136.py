from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from tortoise import fields
from tortoise.migrations.constraints import CheckConstraint

class Migration(migrations.Migration):
    dependencies = [('tests_support_form_binding', '0001_initial')]

    initial = False

    operations = [
        ops.AlterField(
            model_name='FormContact',
            name='address',
            field=fields.ForeignKeyField('tests_support_form_binding.FormAddress', source_field='address_id', db_constraint=True, to_field='id', on_delete=OnDelete.CASCADE),
        ),
        ops.AlterField(
            model_name='FormContact',
            name='phone',
            field=fields.ForeignKeyField('tests_support_form_binding.FormPhone', source_field='phone_id', db_constraint=True, to_field='id', on_delete=OnDelete.CASCADE),
        ),
        ops.AlterField(
            model_name='FormDocument',
            name='labels',
            field=fields.ManyToManyField('tests_support_form_binding.FormLabel', unique=True, db_constraint=True, through='test_form_document_test_form_label', forward_key='formlabel_id', backward_key='test_form_document_id', related_name='test_form_documents', on_delete=OnDelete.CASCADE),
        ),
        ops.AddConstraint(
            model_name='FormVersionedRecord',
            constraint=CheckConstraint(check='version >= 0', name='test_form_versioned_record_version_non_negative_002fc68d'),
        ),
    ]
