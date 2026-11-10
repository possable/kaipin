from django.db import migrations, models


def copy_predecessors(apps, schema_editor):
    """把原来的单个前置任务外键搬到新的多对多关系里。"""
    TaskTemplate = apps.get_model('templates_app', 'TaskTemplate')
    for task in TaskTemplate.objects.exclude(depends_on_legacy=None).iterator():
        task.depends_on.add(task.depends_on_legacy_id)


class Migration(migrations.Migration):

    dependencies = [
        ('templates_app', '0007_tasktemplate_depends_on'),
    ]

    operations = [
        migrations.RenameField(
            model_name='tasktemplate',
            old_name='depends_on',
            new_name='depends_on_legacy',
        ),
        migrations.AddField(
            model_name='tasktemplate',
            name='depends_on',
            field=models.ManyToManyField(
                blank=True, related_name='dependents', symmetrical=False,
                to='templates_app.tasktemplate', verbose_name='前置任务',
            ),
        ),
        migrations.RunPython(copy_predecessors, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='tasktemplate',
            name='depends_on_legacy',
        ),
    ]
