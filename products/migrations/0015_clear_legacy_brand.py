from django.db import migrations

# 2026-09 之前 brand（项目分类）是自由填写，存量里留着品牌名。字段语义改成
# 「渠道分类」后这些值不再合法，用户确认一并清空。
# 只清不在 PROJECT_CATEGORY_CHOICES 里的值，不误伤已经填了渠道分类的。
VALID_CATEGORIES = ['跨境自营', '京东京造', '内地自营（含大贸）']


def clear_legacy_brand(apps, schema_editor):
    Product = apps.get_model('products', 'Product')
    legacy = (
        Product.objects.exclude(brand='')
        .exclude(brand__in=VALID_CATEGORIES)
    )
    count = legacy.update(brand='')
    if count:
        print(f'\n    清空 {count} 条历史 brand 值')


def noop(apps, schema_editor):
    """不可逆：原值（哪个产品原本是什么品牌）没有留存，回滚无法还原。"""


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0014_require_brand_category'),
    ]

    operations = [
        migrations.RunPython(clear_legacy_brand, noop),
    ]
