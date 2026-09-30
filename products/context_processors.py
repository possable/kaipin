from products.models import PROJECT_TYPE_CHOICES


def project_types(request):
    """项目类型下拉选项。看板筛选栏和产品资料表单共用同一份，避免两处硬编码写歪。"""
    return {'project_type_choices': PROJECT_TYPE_CHOICES}
