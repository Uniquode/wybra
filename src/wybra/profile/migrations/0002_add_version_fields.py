from tortoise import migrations
from tortoise.migrations import operations as ops
from wybra.db.versioning import VersionField
from tortoise.migrations.constraints import CheckConstraint

class Migration(migrations.Migration):
    dependencies = [('wybra_auth', '0001_initial'), ('wybra_media', '0001_initial'), ('wybra_profile', '0001_initial')]

    initial = False

    operations = [
        ops.AddField(
            model_name='UserProfile',
            name='version',
            field=VersionField(default=0),
        ),
        ops.AddConstraint(
            model_name='UserProfile',
            constraint=CheckConstraint(check='version >= 0', name='profile_user_profile_version_non_negative_3c250be0'),
        ),
    ]
