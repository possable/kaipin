import django.db.models.deletion
from datetime import time
from django.db import migrations, models
from django.utils import timezone


def backfill_product_stage_todos(apps, schema_editor):
    """遍历现有 Product/ProductStage，为有 assignee 的生成 auto_todo"""
    Product = apps.get_model('products', 'Product')
    ProductStage = apps.get_model('products', 'ProductStage')
    TodoItem = apps.get_model('accounts', 'TodoItem')

    # 项目级 auto_todo
    for product in Product.objects.select_related('assignee').all():
        if not product.assignee_id:
            continue
        if TodoItem.objects.filter(source_product_id=product.id).exists():
            continue
        content = f'总盯项目：{product.name}'[:200]
        due_at = None
        if product.expected_end_date:
            naive = timezone.datetime.combine(product.expected_end_date, time(23, 59))
            due_at = timezone.make_aware(naive)
        is_done = (product.status == 'completed')
        completed_at = product.actual_end_date if is_done else None
        TodoItem.objects.create(
            user_id=product.assignee_id,
            content=content,
            due_at=due_at,
            is_done=is_done,
            completed_at=completed_at,
            source_product_id=product.id,
            is_auto=True,
        )

    # 阶段级 auto_todo
    for stage in ProductStage.objects.select_related('assignee', 'product').all():
        if not stage.assignee_id:
            continue
        if TodoItem.objects.filter(source_stage_id=stage.id).exists():
            continue
        content = f'推进阶段：{stage.product.name} · {stage.name}'[:200]
        due_at = None
        if stage.expected_end_date:
            naive = timezone.datetime.combine(stage.expected_end_date, time(23, 59))
            due_at = timezone.make_aware(naive)
        is_done = (stage.status == 'completed')
        completed_at = stage.completed_at if is_done else None
        TodoItem.objects.create(
            user_id=stage.assignee_id,
            content=content,
            due_at=due_at,
            is_done=is_done,
            completed_at=completed_at,
            source_stage_id=stage.id,
            is_auto=True,
        )


def delete_product_stage_todos(apps, schema_editor):
    """反向迁移：删除项目/阶段级 auto_todo"""
    TodoItem = apps.get_model('accounts', 'TodoItem')
    TodoItem.objects.filter(
        source_product__isnull=False
    ).delete()
    TodoItem.objects.filter(
        source_stage__isnull=False
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_todoitem_source_task'),
        ('products', '0004_productstage_assignee'),
    ]

    operations = [
        migrations.AddField(
            model_name='todoitem',
            name='source_stage',
            field=models.OneToOneField(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='auto_todo', to='products.productstage',
                verbose_name='来源阶段',
            ),
        ),
        migrations.AddField(
            model_name='todoitem',
            name='source_product',
            field=models.OneToOneField(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='auto_todo', to='products.product',
                verbose_name='来源项目',
            ),
        ),
        migrations.RunPython(backfill_product_stage_todos, delete_product_stage_todos),
    ]
