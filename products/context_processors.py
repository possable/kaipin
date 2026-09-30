from products.models import PROJECT_TYPE_CHOICES, PROJECT_CATEGORY_CHOICES, Product

# 历史遗留值的后缀标记。brand 早期是自由填写，存量里留着「莱特维健」「柏澳斯」
# 这类不属于渠道分类的值，标一下让用户看出来不是新选项。
LEGACY_SUFFIX = '（旧）'


def _project_category_choices():
    """项目分类下拉 = 三个渠道分类 + 库里已经用过的历史值。

    必须把历史值也带上：下拉里没有的话，老产品打开编辑弹窗会落到「未设置」，
    一保存就把原值冲成空值。选项的 value 用原值，所以能正常回显、也能原样存回。
    查库而不是写死，是因为这些值随时可能被用户在编辑里改成三个分类而消失。
    """
    choices = list(PROJECT_CATEGORY_CHOICES)
    known = {value for value, _ in choices}
    legacy = (
        Product.objects.exclude(brand='')
        .order_by('brand')
        .values_list('brand', flat=True)
        .distinct()
    )
    for value in legacy:
        if value not in known:
            known.add(value)
            choices.append((value, f'{value}{LEGACY_SUFFIX}'))
    return choices


def project_types(request):
    """项目类型/项目分类下拉选项。看板筛选栏和产品资料表单共用同一份，避免两处硬编码写歪。"""
    return {
        'project_type_choices': PROJECT_TYPE_CHOICES,
        'project_category_choices': _project_category_choices(),
    }
