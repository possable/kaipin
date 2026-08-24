from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver


class Department(models.Model):
    """部门，如策划部、设计部等"""
    name = models.CharField(max_length=50, unique=True, verbose_name='部门名称')
    wechat_dept_id = models.IntegerField(
        null=True, blank=True, unique=True,
        verbose_name='企业微信部门ID'
    )

    class Meta:
        verbose_name = '部门'
        verbose_name_plural = '部门'
        ordering = ['id']

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    """扩展 Django User，关联部门和微信 UserID"""
    ROLE_CHOICES = [
        ('admin', '管理员'),
        ('member', '普通成员'),
    ]
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='profile',
        verbose_name='用户'
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True,
        verbose_name='所属部门'
    )
    wechat_userid = models.CharField(
        max_length=100, blank=True, default='',
        verbose_name='企业微信UserID'
    )
    role = models.CharField(
        max_length=10, choices=ROLE_CHOICES, default='member',
        verbose_name='角色'
    )

    class Meta:
        verbose_name = '用户资料'
        verbose_name_plural = '用户资料'

    def __str__(self):
        dept_name = self.department.name if self.department else '未分配部门'
        return f'{self.user.username} - {dept_name}'

    @property
    def is_admin(self):
        return self.role == 'admin'


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """新建 User 时自动创建空的 UserProfile（department 需后续补填）"""
    if created:
        UserProfile.objects.create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    """保存 User 时同步保存 profile"""
    if hasattr(instance, 'profile'):
        instance.profile.save()


class TodoItem(models.Model):
    """个人待办事项，用户自己管理（每个账号独立）。
    可选关联到 products.Task —— 任务负责人自动获得对应 auto_todo。"""
    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='todos', verbose_name='用户'
    )
    content = models.CharField(max_length=200, verbose_name='内容')
    due_at = models.DateTimeField(null=True, blank=True, verbose_name='截止时间')
    is_done = models.BooleanField(default=False, verbose_name='已完成')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    # 自动生成的待办关联到源任务/阶段/项目；手动加的待办三个都为 null
    source_task = models.OneToOneField(
        'products.Task', on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='auto_todo',
        verbose_name='来源任务'
    )
    source_stage = models.OneToOneField(
        'products.ProductStage', on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='auto_todo',
        verbose_name='来源阶段'
    )
    source_product = models.OneToOneField(
        'products.Product', on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='auto_todo',
        verbose_name='来源项目'
    )
    is_auto = models.BooleanField(default=False, verbose_name='系统自动生成')

    class Meta:
        verbose_name = '待办事项'
        verbose_name_plural = '待办事项'
        # 未完成置顶，然后按截止时间正序，最后按创建时间倒序
        ordering = ['is_done', 'due_at', '-created_at']

    def __str__(self):
        return f'{self.user.username}: {self.content}'


class Announcement(models.Model):
    """全局公告，管理员在后台发布，所有登录用户在看板侧边栏可见。"""
    title = models.CharField(max_length=100, verbose_name='标题')
    content = models.TextField(verbose_name='内容')
    is_pinned = models.BooleanField(default=False, verbose_name='置顶')
    is_active = models.BooleanField(default=True, verbose_name='生效')
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='announcements', verbose_name='发布人'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='发布时间')

    class Meta:
        verbose_name = '公告'
        verbose_name_plural = '公告'
        ordering = ['-is_pinned', '-created_at']

    def __str__(self):
        return self.title
