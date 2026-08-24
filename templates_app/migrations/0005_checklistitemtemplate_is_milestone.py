from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('templates_app', '0004_checklistitemtemplate'),
    ]

    operations = [
        migrations.AddField(
            model_name='checklistitemtemplate',
            name='is_milestone',
            field=models.BooleanField(default=False, verbose_name='是否里程碑'),
        ),
    ]
