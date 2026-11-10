import logging
from datetime import time, timedelta

from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils import timezone
from accounts.models import Department
from templates_app.models import StageTemplate

logger = logging.getLogger(__name__)


def _cached_auto_todo(obj):
    """取 obj.auto_todo；实例上缓存着已删除对象（pk=None）时当作没有。

    Django 的反向一对一关系会把对象缓存在 _state.fields_cache 里，delete()
    之后缓存仍然指向那个 pk 已被清空的对象。项目暂停清空待办后如果再用
    同一个实例同步，就会拿着这个幽灵对象去 delete/save 而报错，所以这里
    顺手把失效缓存清掉。
    """
    todo = getattr(obj, 'auto_todo', None)
    if todo is not None and todo.pk is None:
        obj._state.fields_cache.pop('auto_todo', None)
        return None
    return todo


def compute_task_status(task, today=None):
    """任务有效状态的唯一判定口径，供 Task.update_status() 和看板展示逻辑共用。

    规则：已完成保持完成；前置任务未完成→待开始；开始时间或预计结束时间
    任一缺失→待开始；今天早于开始时间→待开始；今天晚于预计结束→已逾期；
    否则→进行中。
    """
    if task.status == 'completed':
        return 'completed'
    if task.is_blocked:
        return 'pending'
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


# 项目类型选项（原先散落在看板筛选栏和产品资料表单里，统一到这里）
PROJECT_TYPE_CHOICES = [
    ('老品迭代', '老品迭代'),
    ('新品开发', '新品开发'),
]

# 项目分类选项（三个渠道分类，2026-09 从项目类型里挪出来）
# 注：brand 字段不设 choices —— 值域靠表单里的 <select> 约束就够了，
# 设 choices 只会多出一次无意义的迁移。曾短暂存在的自由填写历史值
# （「莱特维健」「柏澳斯」）已在迁移 0015 里清空。
PROJECT_CATEGORY_CHOICES = [
    ('跨境自营', '跨境自营'),
    ('京东京造', '京东京造'),
    ('内地自营（含大贸）', '内地自营（含大贸）'),
]


class Product(models.Model):
    """品（新品开发项目）"""
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('active', '进行中'),
        ('paused', '暂停'),
        ('completed', '已完成'),
        ('cancelled', '已取消'),
    ]
    name = models.CharField(max_length=200, verbose_name='品名')

    # ---- 产品资料（选填，创建后可逐步补充） ----
    product_name = models.CharField(max_length=200, blank=True, verbose_name='产品名称')
    brand = models.CharField(max_length=100, blank=False, verbose_name='项目分类')
    platforms = models.CharField(max_length=200, blank=True, verbose_name='上架平台')
    category = models.CharField(
        max_length=100, blank=False, choices=PROJECT_TYPE_CHOICES, verbose_name='项目类型'
    )
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

    def create_stages_from_templates(self, stage_setup=None):
        """从当前 StageTemplate 快照生成 ProductStage 和 Task。非草稿时自动激活第一阶段。

        stage_setup: {StageTemplate.pk: {'assignee': User, 'started_at': datetime,
        'expected_end_date': date}}，新建项目第二步填的各阶段分工；没给的阶段
        仍用模板里的默认负责人。
        """
        stage_setup = stage_setup or {}
        templates = StageTemplate.objects.all()
        # 前置任务可以跨阶段，先把所有 Task 建出来，再按模板里的依赖关系回填
        template_task_map = {}
        created_tasks = []
        for i, tmpl in enumerate(templates):
            setup = stage_setup.get(tmpl.pk) or {}
            stage_assignee = setup.get('assignee') or tmpl.default_assignee
            ps = ProductStage.objects.create(
                product=self,
                name=tmpl.name,
                order=tmpl.order,
                department=tmpl.department,
                allow_parallel=tmpl.allow_parallel,
                assignee=stage_assignee,
                status='in_progress' if (i == 0 and self.status != 'draft') else 'pending',
                started_at=(
                    timezone.now() if (i == 0 and self.status != 'draft')
                    else setup.get('started_at')
                ),
                expected_end_date=setup.get('expected_end_date'),
            )
            for j, t_tmpl in enumerate(tmpl.task_templates.all()):
                # 立项阶段（第一阶段）建好就要开始推进，子任务默认归阶段负责人，
                # 避免新项目一创建就出现一批「负责人未指定」的信息缺失提醒。
                task_assignee = stage_assignee if i == 0 else None
                task = Task.objects.create(
                    product_stage=ps,
                    name=t_tmpl.name,
                    order=j + 1,
                    status='pending',
                    is_milestone=t_tmpl.is_milestone,
                    assignee=task_assignee,
                )
                template_task_map[t_tmpl.pk] = task
                created_tasks.append((task, t_tmpl))
                for k, c_tmpl in enumerate(t_tmpl.checklist_item_templates.all()):
                    TaskChecklistItem.objects.create(
                        task=task,
                        name=c_tmpl.name,
                        order=k + 1,
                    )

        for task, t_tmpl in created_tasks:
            predecessors = [
                template_task_map[pk]
                for pk in t_tmpl.depends_on.values_list('pk', flat=True)
                if pk in template_task_map
                and template_task_map[pk].pk != task.pk
            ]
            if predecessors:
                task.depends_on.set(predecessors)

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

    def pause(self):
        """暂停项目：状态改为 paused，阶段/任务原样保留。

        暂停期间所有自动通知都会跳过（定时扫描只扫 status='active'，
        notify_upward 也会拦下暂停的品），恢复后照常继续。
        """
        if self.status != 'active':
            return False
        self.status = 'paused'
        self.save(update_fields=['status'])
        # 暂停的项目不占用任何人的待办，项目/阶段/任务三层一起清掉
        self.sync_all_auto_todos()
        return True

    def resume(self):
        """恢复被暂停的项目"""
        if self.status != 'paused':
            return False
        self.status = 'active'
        self.save(update_fields=['status'])
        # 按当前负责人把三层待办重建回来
        self.sync_all_auto_todos()
        return True

    def can_be_managed_by(self, user):
        """是否可编辑该品的负责人/时间等字段（管理员或品负责人）"""
        return user.profile.is_admin or self.assignee == user

    def sync_auto_todo(self):
        """项目层 auto_todo 同步：项目总负责人自动获得一条'总盯项目：XX'待办。
        换负责人/改时间/项目完成时调用。仅正向同步——勾选待办不反向结项目。"""
        from datetime import time
        from accounts.models import TodoItem

        todo = _cached_auto_todo(self)

        # 暂停/取消的项目不占用待办：直接清掉，恢复项目时再重建
        if self.status in ('paused', 'cancelled'):
            if todo:
                todo.delete()
            return

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

    def sync_all_auto_todos(self):
        """项目、阶段、任务三层的自动待办一起刷新。

        暂停/取消时用来把整棵树的待办清掉，恢复时再按当前负责人建回来。
        """
        self.sync_auto_todo()
        for stage in self.stages.all():
            stage.sync_auto_todo()
            for task in stage.tasks.all():
                task.sync_auto_todo()


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

        todo = _cached_auto_todo(self)

        # 暂停/取消的项目下不再挂阶段待办；恢复项目时由 sync_all_auto_todos 重建
        if self.product.status in ('paused', 'cancelled'):
            if todo:
                todo.delete()
            return

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
    # 前置任务可以有多个：全部完成后本任务才解锁，开始时间以其中结束最晚的为准
    depends_on = models.ManyToManyField(
        'self', symmetrical=False, blank=True,
        related_name='dependents', verbose_name='前置任务',
    )

    class Meta:
        verbose_name = '子任务'
        verbose_name_plural = '子任务'
        ordering = ['order']

    def __str__(self):
        return self.name

    @property
    def is_blocked(self):
        """有前置任务还没完成时，本任务不能开始（也就不能设开始时间）。"""
        if not self.pk:
            return False
        # 遍历 prefetch 缓存而不是 .filter()：看板/提醒扫描里会一次查很多任务，
        # 这里再走一次 SQL 就是 N+1
        return any(p.status != 'completed' for p in self.depends_on.all())

    @property
    def blocking_predecessor_name(self):
        """还没完成的前置任务名，多个用「、」连接。"""
        incomplete = [
            p.name for p in self.depends_on.all() if p.status != 'completed'
        ]
        return '、'.join(incomplete)

    def creates_dependency_cycle(self, predecessors):
        """把 predecessors 都设为本任务的前置任务，是否会形成环。"""
        seen = set()
        stack = list(predecessors)
        while stack:
            node = stack.pop()
            if node.pk == self.pk:
                return True
            if node.pk in seen:
                continue
            seen.add(node.pk)
            stack.extend(node.depends_on.all())
        return False

    def mark_completed(self):
        self.status = 'completed'
        now = timezone.now()
        self.completed_at = now
        self.actual_end_date = now
        self.save()

        from reminders.upward_notify import notify_upward
        notify_upward(self, 'completed', actor=self.assignee)

        # 前置任务完成 → 依赖它的任务立即解锁：自动补开始/结束时间并通知三方。
        # 通知/写日志失败不能影响「前置任务已完成」这条主流程。
        try:
            self._activate_dependents(now)
        except Exception:
            logger.exception(
                '前置任务完成后自动激活后续任务失败: predecessor=%s', self.pk,
            )

    def _activate_dependents(self, completed_at):
        dependents = list(
            self.dependents.exclude(status='completed')
            .prefetch_related('depends_on')
            .select_related(
                'assignee__profile',
                'product_stage__assignee__profile',
                'product_stage__product__assignee__profile',
            )
        )
        if not dependents:
            return

        for dependent in dependents:
            predecessors = list(dependent.depends_on.all())
            # 多个前置任务时，只要还有一个没完成就不能激活；
            # 等最后一个前置完成、再次走到这里时再统一排期
            if any(p.status != 'completed' for p in predecessors):
                continue

            def _finish_date(p):
                if p.actual_end_date:
                    return timezone.localtime(p.actual_end_date).date()
                return p.expected_end_date

            dated = [p for p in predecessors if _finish_date(p)]
            latest = max(dated, key=_finish_date) if dated else None
            if latest is not None and latest.actual_end_date:
                started_at = latest.actual_end_date
            else:
                finish_date = (
                    _finish_date(latest) if latest is not None
                    else timezone.localtime(completed_at).date()
                )
                started_at = timezone.make_aware(
                    timezone.datetime.combine(finish_date, time.min)
                )
            # 以结束时间最晚的前置任务为准：开始时间接在它后面，预计结束默认 7 天后；
            # 阶段负责人之后仍可手工修改这两个时间。
            start_date = timezone.localtime(started_at).date()
            expected_end_date = start_date + timedelta(days=7)
            predecessor_names = '、'.join(p.name for p in predecessors)
            try:
                dependent.started_at = started_at
                dependent.expected_end_date = expected_end_date
                dependent.status = 'pending'
                dependent.save(update_fields=[
                    'started_at', 'expected_end_date', 'status',
                ])
                dependent.update_status()
                dependent.sync_auto_todo()
            except Exception:
                logger.exception(
                    '自动激活后续任务失败: task=%s predecessor=%s',
                    dependent.pk, self.pk,
                )
                continue

            try:
                from reminders.upward_notify import notify_dependent_started
                notify_dependent_started(dependent, self)
            except Exception:
                logger.exception(
                    '前置任务完成通知失败: task=%s predecessor=%s',
                    dependent.pk, self.pk,
                )

            try:
                from activity_log.utils import log_action
                log_action(
                    None, '前置任务完成，任务自动开始', 'task', dependent.pk,
                    f'{dependent.product_stage.product.name} / {dependent.name}',
                    f'前置任务「{predecessor_names}」已完成，'
                    f'开始时间自动设为 {start_date:%Y-%m-%d}，'
                    f'预计结束 {expected_end_date:%Y-%m-%d}',
                )
            except Exception:
                logger.exception(
                    '前置任务完成写操作日志失败: task=%s', dependent.pk,
                )

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

        todo = _cached_auto_todo(self)

        stage = self.product_stage
        product = stage.product

        # 暂停/取消的项目下不再挂任务待办；恢复项目时由 sync_all_auto_todos 重建
        if product.status in ('paused', 'cancelled'):
            if todo:
                todo.delete()
            return

        if not self.assignee_id:
            if todo:
                todo.delete()
            return

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
