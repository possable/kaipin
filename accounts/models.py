from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver


class WeComCorp(models.Model):
    """一个企业微信企业，对应一套 corp_id / agent_id / app_secret。

    三个企业共用一个 Django 实例、一套项目数据、一套职能部门，
    差别只在「从哪个企业微信登录」和「用哪个企业的应用发消息」。
    每个企业配一个入口 URL：/accounts/c/<code>/。

    code 是给人看的短标识（也出现在 URL 里），corp_id 是企业微信那边的 ID，
    两者不要混用：code 可以随便起，corp_id 必须和企业微信后台一致。
    """
    code = models.SlugField(
        max_length=32, unique=True,
        verbose_name='标识', help_text='用于 URL，如 default / b-corp，仅限字母数字和下划线连字符'
    )
    name = models.CharField(max_length=100, verbose_name='企业名称')
    corp_id = models.CharField(max_length=100, verbose_name='企业ID (corp_id)')
    agent_id = models.CharField(max_length=32, verbose_name='应用ID (agent_id)')
    app_secret = models.CharField(max_length=200, verbose_name='应用密钥 (Secret)')
    is_active = models.BooleanField(default=True, verbose_name='启用')
    is_default = models.BooleanField(
        default=False, verbose_name='默认企业',
        help_text='旧入口 /accounts/auto-login/ 落到的企业，全局只应有一条为真'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '企业微信企业'
        verbose_name_plural = '企业微信企业'
        ordering = ['id']

    def __str__(self):
        return f'{self.name}({self.code})'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # 保证「默认企业」全局唯一：把别的行取消掉
        if self.is_default:
            WeComCorp.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)


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


class WeComDept(models.Model):
    """一个企业微信部门 → 一个共享 Department 的映射。

    三个企业的部门 id 各自独立、且**会互相撞号**（A 的 wechat_dept_id=16 是
    「产品部」，B 的 id=16 是「采购部」）。所以绝不能拿企微返回的部门 id 直接查
    `Department.wechat_dept_id` —— 那正是「B 的采购部员工拿到产品部编辑权限」的成因。
    必须先经过本表换算成共享部门。

    Department 表本身**保持共享、不加企业字段**：三个企业的「设计部」都指向同一行，
    所以所有 `stage.department == user.profile.department` 的权限判断一行都不用改。

    is_manual=True 的记录，同步命令永不覆盖（否则每天 07:50 会把人工修正打回去）。
    """
    corp = models.ForeignKey(
        WeComCorp, on_delete=models.CASCADE, related_name='depts', verbose_name='企业'
    )
    wechat_dept_id = models.IntegerField(verbose_name='企业微信部门ID')
    name = models.CharField(max_length=100, verbose_name='企微部门名称')
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='wecom_depts', verbose_name='映射到的共享部门',
        help_text='留空表示该企微部门暂未对应到任何共享部门'
    )
    is_manual = models.BooleanField(
        default=False, verbose_name='人工指定',
        help_text='勾选后自动同步不会覆盖本行的映射'
    )
    is_root = models.BooleanField(
        default=False, verbose_name='根部门',
        help_text='企业自身那一层，不参与「主部门」解析'
    )

    class Meta:
        verbose_name = '企业微信部门映射'
        verbose_name_plural = '企业微信部门映射'
        ordering = ['corp_id', 'wechat_dept_id']
        # 同一企业内部门 id 唯一；不同企业可以同名同 id
        unique_together = [('corp', 'wechat_dept_id')]

    def __str__(self):
        target = self.department.name if self.department else '未映射'
        return f'{self.corp.code}/{self.wechat_dept_id} {self.name} → {target}'


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
    wecom_corp = models.ForeignKey(
        WeComCorp, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='profiles', verbose_name='所属企业',
        help_text='只有「主身份」写在这里，完整的多企业身份见「企业微信身份」'
    )
    role = models.CharField(
        max_length=10, choices=ROLE_CHOICES, default='member',
        verbose_name='角色'
    )

    class Meta:
        verbose_name = '用户资料'
        verbose_name_plural = '用户资料'
        # 同一个企业内，一个企微 userid 只能属于一个账号。
        # MySQL 唯一索引里 NULL 互不冲突，所以未绑定的行（wecom_corp=NULL）可以有多条。
        # ⚠️ 本约束只在「同企业内」成立：同一个人在两个企业有两套 userid，
        #    由 WeComIdentity 承载，这里存的只是主身份。
        unique_together = [('wecom_corp', 'wechat_userid')]

    def __str__(self):
        dept_name = self.department.name if self.department else '未分配部门'
        return f'{self.user.username} - {dept_name}'

    @property
    def is_admin(self):
        return self.role == 'admin'


class WeComIdentity(models.Model):
    """一个账号在某个企业微信里的身份（corp + userid）。

    为什么不能只用 UserProfile.wechat_userid 存：实测 B 企业 60 人里有 23 人
    （38%）和 A 企业是同一批人，其中 5 个人在两个企业的 userid **不一样**
    （如 阮仕云 A=Ruanivan / B=ivanRuan）。一个字段存不下两个 userid。

    同一 (企业, userid) 全局唯一；(账号, 企业) 也唯一 —— 后者保证不会给同一个人
    在同一个企业里建出两条身份。
    """
    corp = models.ForeignKey(
        WeComCorp, on_delete=models.CASCADE, related_name='identities', verbose_name='企业'
    )
    userid = models.CharField(max_length=100, verbose_name='企业微信UserID')
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='wecom_identities', verbose_name='账号'
    )
    is_primary = models.BooleanField(
        default=False, verbose_name='主身份',
        help_text='发企微消息时用哪套凭证。只有一个企业时无所谓，多企业时取主身份'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '企业微信身份'
        verbose_name_plural = '企业微信身份'
        ordering = ['user_id', '-is_primary', 'id']
        unique_together = [('corp', 'userid'), ('user', 'corp')]

    def __str__(self):
        return f'{self.user.username}@{self.corp.code}({self.userid})'


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
