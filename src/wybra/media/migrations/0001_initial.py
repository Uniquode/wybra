from tortoise import migrations
from tortoise.migrations import operations as ops
from time import time
from tortoise.fields.base import OnDelete
from uuid import uuid4
from tortoise import fields
from tortoise.indexes import Index

class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name='MediaItem',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('category', fields.CharField(max_length=120)),
                ('storage_key', fields.CharField(unique=True, max_length=1024)),
                ('content_type', fields.CharField(null=True, max_length=255)),
                ('size', fields.IntField()),
                ('created_at', fields.FloatField(default=time)),
                ('modified_at', fields.FloatField(default=time)),
            ],
            options={'table': 'media_item', 'app': 'wybra_media', 'indexes': [Index(fields=['category']), Index(fields=['created_at'])], 'pk_attr': 'id', 'table_description': 'Catalogued media item stored under the configured media root.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='MediaResourceKey',
            fields=[
                ('resource_key', fields.CharField(primary_key=True, unique=True, db_index=True, max_length=255)),
                ('media', fields.ForeignKeyField('wybra_media.MediaItem', source_field='media_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'media_resource_key', 'app': 'wybra_media', 'pk_attr': 'resource_key', 'table_description': 'Lookup key assigned to a media item for stable resource references.'},
            bases=['Model'],
        ),
    ]
