from django.db import migrations, models


def copy_predecessors(apps, schema_editor):
    """把原来的单个前置任务外键搬到新的多对多关系里。"""
    Task = apps.get_model('products', 'Task')
    for task in Task.objects.exclude(depends_on_legacy=None).iterator():
        task.depends_on.add(task.depends_on_legacy_id)


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0017_task_depends_on'),
    ]

    operations = [
        # 前置任务从「一个」改成「多个」：先改名保留旧列，建好多对多表、
        # 把数据搬过去，最后再删掉旧列。
        migrations.RenameField(
            model_name='task',
            old_name='depends_on',
            new_name='depends_on_legacy',
        ),
        migrations.AddField(
            model_name='task',
            name='depends_on',
            field=models.ManyToManyField(
                blank=True, related_name='dependents', symmetrical=False,
                to='products.task', verbose_name='前置任务',
            ),
        ),
        migrations.RunPython(copy_predecessors, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='task',
            name='depends_on_legacy',
        ),
    ]
