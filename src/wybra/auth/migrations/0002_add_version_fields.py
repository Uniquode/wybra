from tortoise import migrations
from tortoise.migrations import operations as ops
from wybra.db.versioning import VersionField
from tortoise.migrations.constraints import CheckConstraint

class Migration(migrations.Migration):
    dependencies = [('wybra_auth', '0001_initial')]

    initial = False

    operations = [
        ops.AddField(
            model_name='User',
            name='version',
            field=VersionField(default=0),
        ),
        ops.AddConstraint(
            model_name='User',
            constraint=CheckConstraint(check='version >= 0', name='identity_user_version_non_negative_ee0e26cd'),
        ),
    ]
