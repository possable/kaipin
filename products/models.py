from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils import timezone
from accounts.models import Department
from templates_app.models import StageTemplate


def compute_task_status(task, today=None):
    """任务有效状态的唯一判定口径，供 Task.update_status() 和看板展示逻辑共用。

    规则：已完成保持完成；开始时间或预计结束时间任一缺失→待开始；
    今天早于开始时间→待开始；今天晚于预计结束→已逾期；否则→进行中。
    """
    if task.status == 'completed':
        return 'completed'
    if today is None:
        today = timezone.now().date()
    started = task.started_at.date() if task.started_at else None
    expected = task.expected_end_date
    if not started or not expected:
        return 'pending'
    if today < started:
        return 'pending'
    if today > expected:
        return 'overdue'
    return 'in_progress'


def compute_task_color(task):
    """任务状态对应的展示颜色，供任务状态标签和看板阶段进度点共用。

    五色规则：
    - pending（未开始）→ gray
    - in_progress（进行中）→ white
    - overdue（已超期未完成）→ red
    - completed 且实际完成时间未晚于预计结束 → green
    - completed 且实际完成时间晚于预计结束（超期后才完成）→ yellow
    """
    if task.status == 'completed':
        if (
            task.expected_end_date
            and task.actual_end_date
            and task.actual_end_date.date() > task.expected_end_date
        ):
            return 'yellow'
        return 'green'
    if task.status == 'overdue':
        return 'red'
    if task.status == 'in_progress':
        return 'white'
    return 'gray'


class Product(models.Model):
    """品（新品开发项目）"""
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('active', '进行中'),
        ('completed', '已完成'),
        ('cancelled', '已取消'),
    ]
    name = models.CharField(max_length=200, verbose_name='品名')

    # ---- 产品资料（选填，创建后可逐步补充） ----
    product_name = models.CharField(max_length=200, blank=True, verbose_name='产品名称')
    brand = models.CharField(max_length=100, blank=True, verbose_name='品牌')
    platforms = models.CharField(max_length=200, blank=True, verbose_name='上架平台')
    category = models.CharField(max_length=100, blank=True, verbose_name='所属类目')
    positioning = models.CharField(max_length=200, blank=True, verbose_name='产品定位')
    dosage_form = models.CharField(max_length=100, blank=True, verbose_name='剂型')
    specification = models.CharField(max_length=200, blank=True, verbose_name='规格')
    main_ingredients = models.TextField(blank=True, verbose_name='主要成分及含量')
    efficacy = models.TextField(blank=True, verbose_name='功效描述')
    target_audience = models.CharField(max_length=200, blank=True, verbose_name='目标人群')
    usage_scenario = models.CharField(max_length=200, blank=True, verbose_name='场景应用')
    selling_points = models.TextField(blank=True, verbose_name='核心卖点')
    material_advantage = models.TextField(blank=True, verbose_name='原料优势')
    suggested_retail_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='建议零售价'
    )
    suggested_cost_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='建议成本价'
    )
    expected_gross_margin = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True, verbose_name='预期毛利率(%)'
    )
    demand_completion_date = models.DateField(null=True, blank=True, verbose_name='需求完成时间')
    demand_launch_date = models.DateField(null=True, blank=True, verbose_name='需求上市时间')
    project_rationale = models.TextField(blank=True, verbose_name='立项依据')

    creator = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='products',
        verbose_name='创建人'
    )
    assignee = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='owned_products', verbose_name='负责人'
    )
    current_stage_order = models.PositiveIntegerField(default=1, verbose_name='当前阶段序号')
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='active',
        verbose_name='整体状态'
    )
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    expected_end_date = models.DateField(null=True, blank=True, verbose_name='预计结束日期')
    actual_end_date = models.DateTimeField(null=True, blank=True, verbose_name='实际结束时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '品'
        verbose_name_plural = '品'
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def get_current_stage(self):
        """返回当前进行中的 ProductStage，没有则返回 None"""
        return self.stages.filter(status='in_progress').first()

    def has_overdue_tasks(self):
        """当前阶段是否有超期任务"""
        current = self.get_current_stage()
        if not current:
            return False
        return current.tasks.filter(status='overdue').exists()

    def create_stages_from_templates(self):
        """从当前 StageTemplate 快照生成 ProductStage 和 Task。非草稿时自动激活第一阶段。"""
        templates = StageTemplate.objects.all()
        for i, tmpl in enumerate(templates):
            ps = ProductStage.objects.create(
                product=self,
                name=tmpl.name,
                order=tmpl.order,
                department=tmpl.department,
                allow_parallel=tmpl.allow_parallel,
                assignee=tmpl.default_assignee,
                status='in_progress' if (i == 0 and self.status != 'draft') else 'pending',
                started_at=timezone.now() if (i == 0 and self.status != 'draft') else None,
            )
            for j, t_tmpl in enumerate(tmpl.task_templates.all()):
                task = Task.objects.create(
                    product_stage=ps,
                    name=t_tmpl.name,
                    order=j + 1,
                    status='pending',
                    is_milestone=t_tmpl.is_milestone,
                )
                for k, c_tmpl in enumerate(t_tmpl.checklist_item_templates.all()):
                    TaskChecklistItem.objects.create(
                        task=task,
                        name=c_tmpl.name,
                        order=k + 1,
                    )
        if self.status != 'draft':
            self.current_stage_order = 1
            self.save(update_fields=['current_stage_order'])

    def publish(self):
        """发布草稿：激活第一个阶段，项目正式开始"""
        if self.status != 'draft':
            return
        first_stage = self.stages.order_by('order').first()
        if first_stage:
            first_stage.status = 'in_progress'
            first_stage.started_at = timezone.now()
            first_stage.save()
        self.status = 'active'
        self.current_stage_order = 1
        if not self.started_at:
            self.started_at = timezone.now()
        self.save()

    def can_be_managed_by(self, user):
        """是否可编辑该品的负责人/时间等字段（管理员或品负责人）"""
        return user.profile.is_admin or self.assignee == user

    def sync_auto_todo(self):
        """项目层 auto_todo 同步：项目总负责人自动获得一条'总盯项目：XX'待办。
        换负责人/改时间/项目完成时调用。仅正向同步——勾选待办不反向结项目。"""
        from datetime import time
        from accounts.models import TodoItem

        todo = getattr(self, 'auto_todo', None)

        if not self.assignee_id:
            if todo:
                todo.delete()
            return

        content = f'总盯项目：{self.name}'[:200]
        due_at = None
        if self.expected_end_date:
            naive = timezone.datetime.combine(self.expected_end_date, time(23, 59))
            due_at = timezone.make_aware(naive)
        is_done = (self.status == 'completed')
        completed_at = self.actual_end_date if is_done else None

        if todo:
            todo.user_id = self.assignee_id
            todo.content = content
            todo.due_at = due_at
            todo.is_done = is_done
            todo.completed_at = completed_at
            todo.save()
        else:
            TodoItem.objects.create(
                user_id=self.assignee_id,
                content=content,
                due_at=due_at,
                is_done=is_done,
                completed_at=completed_at,
                source_product=self,
                is_auto=True,
            )


class ProductStage(models.Model):
    """品-阶段实例（品创建时从模板快照生成）"""
    STAGE_STATUS = [
        ('pending', '未开始'),
        ('in_progress', '进行中'),
        ('completed', '已完成'),
    ]
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='stages',
        verbose_name='所属品'
    )
    name = models.CharField(max_length=100, verbose_name='阶段名称')
    order = models.PositiveIntegerField(verbose_name='顺序号')
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, verbose_name='负责部门'
    )
    assignee = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='owned_stages', verbose_name='阶段负责人'
    )
    status = models.CharField(
        max_length=20, choices=STAGE_STATUS, default='pending',
        verbose_name='状态'
    )
    allow_parallel = models.BooleanField(default=False, verbose_name='允许并行')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    expected_end_date = models.DateField(null=True, blank=True, verbose_name='预计结束日期')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='实际结束时间')

    class Meta:
        verbose_name = '品阶段'
        verbose_name_plural = '品阶段'
        ordering = ['order']
        unique_together = ['product', 'order']

    def __str__(self):
        return f'{self.product.name} - {self.name}'

    def all_tasks_completed(self):
        """检查该阶段下所有 Task 是否均已完成"""
        return not self.tasks.exclude(status='completed').exists()

    def sync_auto_todo(self):
        """阶段层 auto_todo 同步：阶段负责人自动获得'推进阶段：XX · YY'待办。
        换负责人/改时间/阶段完成时调用。仅正向同步——勾选待办不反向结束阶段。"""
        from datetime import time
        from accounts.models import TodoItem

        todo = getattr(self, 'auto_todo', None)

        if not self.assignee_id:
            if todo:
                todo.delete()
            return

        content = f'推进阶段：{self.product.name} · {self.name}'[:200]
        due_at = None
        if self.expected_end_date:
            naive = timezone.datetime.combine(self.expected_end_date, time(23, 59))
            due_at = timezone.make_aware(naive)
        is_done = (self.status == 'completed')
        completed_at = self.completed_at if is_done else None

        if todo:
            todo.user_id = self.assignee_id
            todo.content = content
            todo.due_at = due_at
            todo.is_done = is_done
            todo.completed_at = completed_at
            todo.save()
        else:
            TodoItem.objects.create(
                user_id=self.assignee_id,
                content=content,
                due_at=due_at,
                is_done=is_done,
                completed_at=completed_at,
                source_stage=self,
                is_auto=True,
            )

    def can_be_managed_by(self, user):
        """是否可编辑该阶段（负责人/时间等字段）：管理员、品负责人、或阶段进行中时的阶段负责人/所属部门成员；品为草稿时，仅管理员/品负责人可编辑"""
        product = self.product
        if product.status == 'draft':
            return user.profile.is_admin or product.assignee == user
        return (
            user.profile.is_admin
            or product.assignee == user
            or (
                self.status == 'in_progress'
                and (self.assignee == user or self.department == user.profile.department)
            )
        )

    def can_start(self):
        """判断此阶段是否可以开始：前面所有非并行阶段必须已完成"""
        if self.status != 'pending':
            return False
        # 找到前面最近的 sequential（非并行）阶段，它必须已完成
        prev_stages = ProductStage.objects.filter(
            product=self.product,
            order__lt=self.order,
            allow_parallel=False,
        ).order_by('-order')
        for ps in prev_stages:
            if ps.status != 'completed':
                return False
            break  # 只检查最近的那个非并行阶段
        return True

    def complete(self):
        """完成当前阶段。只有非并行阶段会触发下一阶段激活"""
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.save()

        from reminders.upward_notify import notify_upward
        notify_upward(self, 'completed', actor=self.assignee)

        # 找到下一个非并行阶段并激活
        next_seq = ProductStage.objects.filter(
            product=self.product, order__gt=self.order, allow_parallel=False
        ).order_by('order').first()
        if next_seq and next_seq.status == 'pending':
            next_seq.status = 'in_progress'
            next_seq.started_at = timezone.now()
            next_seq.save()
            self.product.current_stage_order = next_seq.order

        # 检查是否所有阶段（含并行）都已完成
        if not ProductStage.objects.filter(
            product=self.product, status__in=['in_progress', 'pending']
        ).exists():
            self.product.status = 'completed'
            self.product.actual_end_date = timezone.now()

        self.product.save()

        if self.product.status == 'completed':
            notify_upward(self.product, 'completed', actor=self.assignee)


class Task(models.Model):
    """子任务实例"""
    TASK_STATUS = [
        ('pending', '未开始'),
        ('in_progress', '进行中'),
        ('completed', '已完成'),
        ('overdue', '已延期'),
    ]
    product_stage = models.ForeignKey(
        ProductStage, on_delete=models.CASCADE, related_name='tasks',
        verbose_name='所属阶段'
    )
    name = models.CharField(max_length=200, verbose_name='任务名称')
    assignee = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_tasks', verbose_name='负责人'
    )
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    deadline = models.DateField(null=True, blank=True, verbose_name='截止日期')
    expected_end_date = models.DateField(null=True, blank=True, verbose_name='预计结束日期')
    actual_end_date = models.DateTimeField(null=True, blank=True, verbose_name='实际结束时间')
    status = models.CharField(
        max_length=20, choices=TASK_STATUS, default='pending',
        verbose_name='状态'
    )
    order = models.PositiveIntegerField(default=0, verbose_name='排序')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    is_milestone = models.BooleanField(default=False, verbose_name='是否里程碑')

    class Meta:
        verbose_name = '子任务'
        verbose_name_plural = '子任务'
        ordering = ['order']

    def __str__(self):
        return self.name

    def mark_completed(self):
        self.status = 'completed'
        now = timezone.now()
        self.completed_at = now
        self.actual_end_date = now
        self.save()

        from reminders.upward_notify import notify_upward
        notify_upward(self, 'completed', actor=self.assignee)

    def mark_overdue(self):
        """由定时任务调用：将任务标记为延期"""
        if self.status not in ('completed', 'overdue'):
            self.status = 'overdue'
            self.save(update_fields=['status'])

            from reminders.upward_notify import notify_upward
            notify_upward(self, 'overdue', actor=self.assignee)

    def sync_auto_todo(self):
        """根据当前任务状态同步 auto_todo（任务变更时调用）。
        - 无 assignee 时删除 auto_todo
        - 有 assignee 时更新或创建 auto_todo，同步 user/content/due_at/is_done"""
        from datetime import time
        from accounts.models import TodoItem

        todo = getattr(self, 'auto_todo', None)

        if not self.assignee_id:
            if todo:
                todo.delete()
            return

        stage = self.product_stage
        product = stage.product
        content = f'{product.name} · {stage.name} · {self.name}'[:200]
        due_at = None
        if self.expected_end_date:
            naive = timezone.datetime.combine(self.expected_end_date, time(23, 59))
            due_at = timezone.make_aware(naive)
        is_done = (self.status == 'completed')
        completed_at = self.actual_end_date if is_done else None

        if todo:
            todo.user_id = self.assignee_id
            todo.content = content
            todo.due_at = due_at
            todo.is_done = is_done
            todo.completed_at = completed_at
            todo.save()
        else:
            TodoItem.objects.create(
                user_id=self.assignee_id,
                content=content,
                due_at=due_at,
                is_done=is_done,
                completed_at=completed_at,
                source_task=self,
                is_auto=True,
            )

    def update_status(self, commit=True):
        """根据开始时间+预计结束时间+当前时间自动更新状态。
        判定口径与看板展示逻辑共用 compute_task_status()，避免看板和详情页状态不一致。"""
        if self.status == 'completed':
            return  # 已完成的不再自动变更

        new_status = compute_task_status(self)
        if self.status != new_status:
            self.status = new_status
            if commit:
                self.save(update_fields=['status'])


class TaskChecklistItem(models.Model):
    """任务下的最小事项，供成员拆解任务、逐项打勾"""
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name='checklist_items',
        verbose_name='所属任务'
    )
    name = models.CharField(max_length=200, verbose_name='事项名称')
    is_done = models.BooleanField(default=False, verbose_name='已完成')
    notes = models.TextField(blank=True, default='', verbose_name='填写内容')
    order = models.PositiveIntegerField(default=0, verbose_name='排序')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')

    class Meta:
        verbose_name = '最小事项'
        verbose_name_plural = '最小事项'
        ordering = ['order', 'id']

    def __str__(self):
        return self.name

    def mark_done(self, done=True):
        self.is_done = done
        self.completed_at = timezone.now() if done else None
        self.save(update_fields=['is_done', 'completed_at'])


class TaskChecklistLog(models.Model):
    """最小事项下的日志，可多次追加"""
    item = models.ForeignKey(
        TaskChecklistItem, on_delete=models.CASCADE, related_name='logs',
        verbose_name='所属事项'
    )
    user = models.ForeignKey(User, on_delete=models.PROTECT, verbose_name='记录人')
    content = models.TextField(verbose_name='日志内容')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='记录时间')

    class Meta:
        verbose_name = '事项日志'
        verbose_name_plural = '事项日志'
        ordering = ['created_at']

    def __str__(self):
        return f'{self.item.name}: {self.content[:20]}'


class TaskAttachment(models.Model):
    """附件"""
    ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'zip']
    MAX_SIZE_MB = 20

    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name='attachments',
        verbose_name='所属任务'
    )
    file = models.FileField(upload_to='attachments/%Y/%m/', verbose_name='文件')
    uploaded_by = models.ForeignKey(
        User, on_delete=models.PROTECT, verbose_name='上传人'
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name='上传时间')

    class Meta:
        verbose_name = '附件'
        verbose_name_plural = '附件'

    def __str__(self):
        return self.file.name.split('/')[-1]

    def get_download_url(self):
        from django.urls import reverse
        return reverse('attachment_download', args=[self.pk])


# ========================================
# 查看权限工具函数（管理员看全部，普通成员只看自己相关）
# ========================================

def visible_products_for(user):
    """返回该用户能查看的所有 Product QuerySet。
    管理员看全部；普通成员能看到自己是总负责人 / 阶段负责人 / 任务负责人的项目，
    以及自己所属部门当前有进行中阶段的项目（与 can_be_managed_by/_check_task_permission
    里"部门成员可操作进行中阶段"的编辑权限保持一致，避免能编辑却看不到）。"""
    if not user.is_authenticated:
        return Product.objects.none()
    if hasattr(user, 'profile') and user.profile.is_admin:
        return Product.objects.all()
    department = user.profile.department if hasattr(user, 'profile') else None
    query = (
        Q(assignee=user)
        | Q(stages__assignee=user)
        | Q(stages__tasks__assignee=user)
    )
    if department:
        query |= Q(stages__department=department, stages__status='in_progress')
    return Product.objects.filter(query).distinct()


def is_visible_to(product, user):
    """单个项目的可见性判断。用在详情/弹窗视图开头做入口校验。
    与 visible_products_for 保持同一套规则，避免"列表看不到但详情能看到"或反过来的不一致。"""
    if not user.is_authenticated:
        return False
    if hasattr(user, 'profile') and user.profile.is_admin:
        return True
    if product.assignee_id == user.id:
        return True
    if product.stages.filter(assignee=user).exists():
        return True
    if Task.objects.filter(product_stage__product=product, assignee=user).exists():
        return True
    department = user.profile.department if hasattr(user, 'profile') else None
    if department and product.stages.filter(department=department, status='in_progress').exists():
        return True
    return False
