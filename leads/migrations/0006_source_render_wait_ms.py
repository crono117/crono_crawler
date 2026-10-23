from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("leads", "0005_source_structured_employer"),
    ]

    operations = [
        migrations.AddField(
            model_name="source",
            name="render_wait_ms",
            field=models.PositiveIntegerField(default=1500),
        ),
    ]

