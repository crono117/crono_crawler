from django.db import migrations


def seed(apps, schema_editor):
    apps.get_model('classification', 'JevControl').objects.get_or_create(key='jev')


class Migration(migrations.Migration):
    dependencies = [('classification', '0001_initial')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
