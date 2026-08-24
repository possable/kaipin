from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied


def admin_required(view_func):
    """要求当前用户为管理员角色的装饰器"""
    def check_admin(user):
        if not user.is_authenticated:
            return False
        return user.profile.is_admin

    return user_passes_test(check_admin, login_url='/accounts/login/')(view_func)


def can_create_product(user):
    """用户是否有创建项目的权限：管理员 或 产品部成员"""
    if not user.is_authenticated:
        return False
    if user.profile.is_admin:
        return True
    dept = user.profile.department
    return dept is not None and dept.name == '产品部'
