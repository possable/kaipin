import django.db.models.deletion
from datetime import time
from django.db import migrations, models
from django.utils import timezone


def backfill_auto_todos(apps, schema_editor):
    """遍历现有 Task，为有 assignee 的任务创建 auto_todo"""
    Task = apps.get_model('products', 'Task')
    TodoItem = apps.get_model('accounts', 'TodoItem')
    for task in Task.objects.select_related(
        'assignee', 'product_stage__product'
    ).all():
        if not task.assignee_id:
            continue
        # 幂等：跳过已有 auto_todo 的任务
        if TodoItem.objects.filter(source_task_id=task.id).exists():
            continue
        stage = task.product_stage
        product = stage.product
        content = f'{product.name} · {stage.name} · {task.name}'[:200]
        due_at = None
        if task.expected_end_date:
            naive = timezone.datetime.combine(task.expected_end_date, time(23, 59))
            due_at = timezone.make_aware(naive)
        is_done = (task.status == 'completed')
        completed_at = task.actual_end_date if is_done else None
        TodoItem.objects.create(
            user_id=task.assignee_id,
            content=content,
            due_at=due_at,
            is_done=is_done,
            completed_at=completed_at,
            source_task_id=task.id,
            is_auto=True,
        )


def delete_auto_todos(apps, schema_editor):
    """反向迁移：删除自动生成的待办"""
    TodoItem = apps.get_model('accounts', 'TodoItem')
    TodoItem.objects.filter(is_auto=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_todoitem'),
        ('products', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='todoitem',
            name='is_auto',
            field=models.BooleanField(default=False, verbose_name='系统自动生成'),
        ),
        migrations.AddField(
            model_name='todoitem',
            name='source_task',
            field=models.OneToOneField(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='auto_todo', to='products.task',
                verbose_name='来源任务',
            ),
        ),
        migrations.RunPython(backfill_auto_todos, delete_auto_todos),
    ]
