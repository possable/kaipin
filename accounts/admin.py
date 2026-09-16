from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from .models import Department, UserProfile, WeComCorp, WeComDept, WeComIdentity


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False


class UserAdmin(BaseUserAdmin):
    inlines = [UserProfileInline]


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ['id', 'name']
    search_fields = ['name']


@admin.register(WeComCorp)
class WeComCorpAdmin(admin.ModelAdmin):
    # app_secret 故意不进 list_display / search_fields：
    # admin 的列表页和搜索都会把这些字段渲染进 HTML 与查询日志，
    # 而且 dumpdata 会把密钥整条带出来，备份脚本要当心。
    list_display = ['id', 'code', 'name', 'corp_id', 'agent_id', 'is_active', 'is_default']
    list_filter = ['is_active', 'is_default']
    search_fields = ['code', 'name', 'corp_id']
    readonly_fields = ['created_at']
    fieldsets = [
        (None, {'fields': ['code', 'name', 'is_active', 'is_default']}),
        ('企业微信凭证', {
            'fields': ['corp_id', 'agent_id', 'app_secret'],
            'description': 'Secret 明文入库。企业微信后台重置后需同步更新这里。',
        }),
        ('其他', {'fields': ['created_at']}),
    ]


@admin.register(WeComDept)
class WeComDeptAdmin(admin.ModelAdmin):
    """企微部门 → 共享部门 的映射表。

    这里的「映射到的共享部门」是唯一需要人工介入的字段：自动同步只在**名称完全相同**
    时才会自动映射，名字对不上的（比如 B 的「采购部」在共享部门里没有对应）会留空，
    由管理员在这里指定。
    """
    list_display = ['corp', 'wechat_dept_id', 'name', 'department', 'is_manual', 'is_root']
    list_editable = ['department', 'is_manual']
    list_filter = ['corp', 'is_manual', 'is_root']
    search_fields = ['name']
    ordering = ['corp_id', 'wechat_dept_id']

    def save_model(self, request, obj, form, change):
        # 人工指定了目标部门，就自动标记为人工维护 —— 否则明天的同步会把它覆盖回去。
        # 让这个勾选自动完成，管理员不必记得手动勾。
        if 'department' in form.changed_data and obj.department_id is not None:
            obj.is_manual = True
        super().save_model(request, obj, form, change)


@admin.register(WeComIdentity)
class WeComIdentityAdmin(admin.ModelAdmin):
    """账号在某个企业微信里的身份。同一个人出现在多个企业时会有多条。"""
    list_display = ['user', 'corp', 'userid', 'is_primary']
    list_filter = ['corp', 'is_primary']
    search_fields = ['userid', 'user__username', 'user__first_name']
    raw_id_fields = ['user']
    ordering = ['user_id', '-is_primary']
