from io import BytesIO
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
import re

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from accounts.models import Department, TodoItem
from activity_log.models import ActivityLog
from templates_app.models import StageTemplate, TaskTemplate, ChecklistItemTemplate
from .models import Product, ProductStage, Task


class ProductModelTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='策划部')
        self.user = User.objects.create_user(username='testuser', password='pass')
        self.user.profile.department = self.dept
        self.user.profile.save()

        # 创建模板
        self.st1 = StageTemplate.objects.create(name='阶段1', order=1, department=self.dept)
        TaskTemplate.objects.create(stage_template=self.st1, name='任务A', order=1)
        TaskTemplate.objects.create(stage_template=self.st1, name='任务B', order=2)

        self.st2 = StageTemplate.objects.create(name='阶段2', order=2, department=self.dept)
        TaskTemplate.objects.create(stage_template=self.st2, name='任务C', order=1)

    def test_create_product_snapshots_stages(self):
        product = Product.objects.create(name='测试品', creator=self.user)
        product.create_stages_from_templates()

        self.assertEqual(product.stages.count(), 2)
        self.assertEqual(product.stages.first().status, 'in_progress')
        self.assertEqual(product.stages.last().status, 'pending')
        self.assertEqual(product.stages.first().tasks.count(), 2)
        self.assertEqual(product.stages.last().tasks.count(), 1)
        self.assertEqual(product.current_stage_order, 1)

    def test_template_changes_do_not_affect_existing_product(self):
        product = Product.objects.create(name='测试品', creator=self.user)
        product.create_stages_from_templates()

        # 修改模板
        self.st1.name = '改名后的阶段1'
        self.st1.save()
        TaskTemplate.objects.create(stage_template=self.st1, name='新增任务', order=99)

        # 老品不受影响
        ps = product.stages.first()
        self.assertEqual(ps.name, '阶段1')
        self.assertEqual(ps.tasks.count(), 2)

    def test_create_product_snapshots_task_predecessors(self):
        """流程模板里设置的前置任务会快照到新建项目的 Task 上；没设的保持为空。"""
        task_a = self.st1.task_templates.get(name='任务A')
        task_b = self.st1.task_templates.get(name='任务B')
        task_c = self.st2.task_templates.get(name='任务C')
        task_c.depends_on.set([task_a])  # 跨阶段前置

        product = Product.objects.create(name='测试品', creator=self.user)
        product.create_stages_from_templates()

        snap_a = product.stages.get(order=1).tasks.get(name='任务A')
        snap_b = product.stages.get(order=1).tasks.get(name='任务B')
        snap_c = product.stages.get(order=2).tasks.get(name='任务C')

        self.assertEqual(list(snap_c.depends_on.values_list('pk', flat=True)),
                         [snap_a.pk])
        self.assertFalse(snap_b.depends_on.exists())
        self.assertTrue(snap_c.is_blocked)
        self.assertFalse(snap_a.is_blocked)
        # 模板快照后改模板不影响已建项目
        task_c.depends_on.set([task_b])
        snap_c.refresh_from_db()
        self.assertEqual(list(snap_c.depends_on.values_list('pk', flat=True)),
                         [snap_a.pk])

    def test_create_product_snapshots_multiple_task_predecessors(self):
        """模板里勾了多个前置任务，新建项目里也原样带过来，并且未完成时锁住任务。"""
        task_a = self.st1.task_templates.get(name='任务A')
        task_b = self.st1.task_templates.get(name='任务B')
        task_c = self.st2.task_templates.get(name='任务C')
        task_c.depends_on.set([task_a, task_b])

        product = Product.objects.create(name='多前置品', creator=self.user)
        product.create_stages_from_templates()

        snap_a = product.stages.get(order=1).tasks.get(name='任务A')
        snap_b = product.stages.get(order=1).tasks.get(name='任务B')
        snap_c = product.stages.get(order=2).tasks.get(name='任务C')

        self.assertEqual(
            set(snap_c.depends_on.values_list('pk', flat=True)),
            {snap_a.pk, snap_b.pk},
        )
        self.assertTrue(snap_c.is_blocked)

    def test_create_product_snapshots_checklist_items(self):
        """子任务模板下的最小事项模板也会被快照到新品的 Task 上"""
        task_a = self.st1.task_templates.get(name='任务A')
        ChecklistItemTemplate.objects.create(task_template=task_a, name='事项1', order=1)
        ChecklistItemTemplate.objects.create(task_template=task_a, name='事项2', order=2)

        product = Product.objects.create(name='测试品', creator=self.user)
        product.create_stages_from_templates()

        task = product.stages.first().tasks.get(name='任务A')
        self.assertEqual(task.checklist_items.count(), 2)
        self.assertEqual(
            list(task.checklist_items.order_by('order').values_list('name', flat=True)),
            ['事项1', '事项2'],
        )

        other_task = product.stages.first().tasks.get(name='任务B')
        self.assertEqual(other_task.checklist_items.count(), 0)

    def test_initiation_stage_tasks_default_to_stage_assignee(self):
        """立项阶段（第一阶段）的子任务默认归阶段负责人，后续阶段不预设。"""
        default_owner = User.objects.create_user(
            username='default_owner', password='pass', first_name='模板默认负责人',
        )
        chosen_owner = User.objects.create_user(
            username='chosen_owner', password='pass', first_name='新建时选的负责人',
        )
        self.st1.default_assignee = default_owner
        self.st1.save()

        product = Product.objects.create(name='立项默认负责人品', creator=self.user)
        product.create_stages_from_templates({
            self.st1.pk: {'assignee': chosen_owner},
        })

        initiation = product.stages.get(order=1)
        self.assertEqual(initiation.assignee, chosen_owner)
        self.assertEqual(
            set(initiation.tasks.values_list('assignee_id', flat=True)),
            {chosen_owner.pk},
        )
        # 后续阶段的子任务保持原样：不自动指定负责人
        self.assertEqual(
            set(product.stages.get(order=2).tasks.values_list('assignee_id', flat=True)),
            {None},
        )


from django.test import Client


class TaskAPITest(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name='策划部')
        self.dept_b = Department.objects.create(name='设计部')

        self.admin_user = User.objects.create_user(username='admin', password='pass')
        self.admin_user.profile.department = self.dept_a
        self.admin_user.profile.role = 'admin'
        self.admin_user.profile.save()

        self.member_a = User.objects.create_user(username='member_a', password='pass')
        self.member_a.profile.department = self.dept_a
        self.member_a.profile.save()

        self.member_b = User.objects.create_user(username='member_b', password='pass')
        self.member_b.profile.department = self.dept_b
        self.member_b.profile.save()

        # 创建模板和品
        st1 = StageTemplate.objects.create(name='阶段1', order=1, department=self.dept_a)
        TaskTemplate.objects.create(stage_template=st1, name='任务A', order=1)
        st2 = StageTemplate.objects.create(name='阶段2', order=2, department=self.dept_b)
        TaskTemplate.objects.create(stage_template=st2, name='任务B', order=1)

        self.product = Product.objects.create(name='测试品', creator=self.admin_user)
        self.product.create_stages_from_templates()

        self.stage1 = self.product.stages.get(order=1)
        self.stage2 = self.product.stages.get(order=2)
        self.task1 = self.stage1.tasks.first()

    def test_member_can_complete_task_in_own_stage(self):
        """部门成员可以完成自己部门的进行中阶段的任务"""
        self.client.login(username='member_a', password='pass')
        resp = self.client.post(reverse('task_complete', args=[self.task1.pk]))
        self.assertEqual(resp.status_code, 200)
        self.task1.refresh_from_db()
        self.assertEqual(self.task1.status, 'completed')

    def test_member_cannot_complete_task_in_other_stage(self):
        """部门成员不能操作其他部门阶段的任务"""
        self.stage1.status = 'completed'
        self.stage1.save()
        self.stage2.status = 'in_progress'
        self.stage2.save()
        task2 = self.stage2.tasks.first()

        self.client.login(username='member_a', password='pass')
        resp = self.client.post(reverse('task_complete', args=[task2.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_member_cannot_complete_task_in_completed_stage(self):
        """部门成员不能操作已完成阶段的任务"""
        self.task1.status = 'completed'
        self.task1.save()
        self.stage1.status = 'completed'
        self.stage1.save()

        self.client.login(username='member_a', password='pass')
        resp = self.client.post(reverse('task_complete', args=[self.task1.pk]))
        # 权限校验不通过，因为该阶段不再是进行中状态
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_complete_any_task(self):
        """管理员可以完成任意阶段的任务"""
        self.client.login(username='admin', password='pass')
        resp = self.client.post(reverse('task_complete', args=[self.task1.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_stage_complete_requires_all_tasks_done(self):
        """阶段完成需确保所有子任务已完成"""
        self.client.login(username='member_a', password='pass')
        resp = self.client.post(reverse('stage_complete', args=[self.stage1.pk]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('未完成', resp.json()['error'])

    def test_stage_complete_advances_to_next(self):
        """阶段完成后自动激活下一阶段"""
        self.task1.mark_completed()
        self.client.login(username='member_a', password='pass')
        resp = self.client.post(reverse('stage_complete', args=[self.stage1.pk]))
        self.assertEqual(resp.status_code, 200)

        self.stage1.refresh_from_db()
        self.stage2.refresh_from_db()
        self.product.refresh_from_db()

        self.assertEqual(self.stage1.status, 'completed')
        self.assertEqual(self.stage2.status, 'in_progress')
        self.assertEqual(self.product.current_stage_order, 2)


class ProductProgressOverviewTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='进度总览测试部门')
        self.design_dept = Department.objects.create(name='设计测试部门')
        self.owner = User.objects.create_user(username='overview_owner', password='pass')
        self.owner.profile.department = self.dept
        self.owner.profile.save()
        self.product = Product.objects.create(
            name='总览测试项目', creator=self.owner, assignee=self.owner,
            status='active', expected_end_date='2030-12-31',
        )
        self.stage1 = ProductStage.objects.create(
            product=self.product, name='需求确认', order=1,
            department=self.dept, assignee=self.owner, status='completed',
        )
        self.stage2 = ProductStage.objects.create(
            product=self.product, name='包装设计', order=2,
            department=self.design_dept, assignee=self.owner, status='in_progress',
            allow_parallel=True,
        )
        Task.objects.create(
            product_stage=self.stage1, name='需求任务1', order=1,
            status='completed', assignee=self.owner, is_milestone=True,
        )
        Task.objects.create(product_stage=self.stage1, name='需求任务2', order=2, status='completed')
        Task.objects.create(
            product_stage=self.stage2, name='设计任务1', order=1,
            status='pending', is_milestone=True,
        )
        Task.objects.create(product_stage=self.stage2, name='设计任务2', order=2, status='pending')

    def test_click_name_modal_contains_progress_overview(self):
        self.client.login(username='overview_owner', password='pass')
        response = self.client.get(reverse('product_progress_modal', args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'project-progress-overview')
        self.assertContains(response, 'roadmap-overview')
        self.assertContains(response, '总览测试项目 · 项目进度总览')
        self.assertNotContains(response, '<h3>产品生命周期路线图</h3>')
        self.assertContains(response, '50%')
        self.assertContains(response, '需求确认')
        self.assertContains(response, '包装设计')
        self.assertContains(response, '2 / 4')
        self.assertContains(response, 'roadmap-timeline-card')
        self.assertContains(response, 'roadmap-timeline-footer')
        self.assertContains(response, 'roadmap-export-actions')
        self.assertContains(response, 'class="roadmap-month"')
        self.assertNotContains(response, '负责部门 / 项目阶段')
        self.assertContains(response, '进度总览测试部门')
        self.assertContains(response, '设计测试部门')
        self.assertContains(response, 'class="roadmap-stage-row is-', count=2)
        self.assertContains(response, 'class="roadmap-stage-row is-completed')
        self.assertContains(response, 'class="roadmap-stage-row is-in_progress')
        self.assertContains(response, 'class="roadmap-stage-label"', count=2)
        self.assertContains(response, 'openStageModal(', count=2)
        self.assertContains(response, 'class="roadmap-stage-band"', count=2)
        self.assertContains(response, 'class="roadmap-milestone is-', count=2)
        self.assertContains(response, '需求任务1')
        self.assertContains(response, '设计任务1')
        self.assertNotContains(response, '需求任务2')
        self.assertNotContains(response, '设计任务2')
        self.assertContains(response, 'openChecklistModal(', count=2)
        self.assertNotContains(response, 'product-profile-form')
        self.assertNotContains(response, 'data-field="assignee"')
        self.assertContains(response, 'roadmap-project-info')
        self.assertContains(response, '负责人')
        self.assertContains(response, '开始时间')
        self.assertContains(response, '预计结束')
        self.assertContains(response, '实际结束')
        self.assertContains(response, '创建时间')
        self.assertContains(response, 'overview_owner')
        self.assertContains(response, '2030-12-31')
        self.assertContains(response, 'exportProjectProgressPng(this)')
        self.assertContains(response, 'printProjectProgressOverview(this)')
        self.assertContains(response, '导出 PNG')
        self.assertContains(response, '打印 / 保存 PDF')
        self.assertContains(response, 'data-export-url=')
        self.assertContains(response, 'project-risk-panel is-clear')
        self.assertContains(response, '风险预警')
        self.assertContains(response, '0 项风险')
        self.assertContains(response, '暂无延期里程碑')

        stages = response.context['progress_overview']['stages']
        self.assertEqual([stage['name'] for stage in stages], ['需求确认', '包装设计'])
        self.assertEqual(
            [[task['name'] for task in stage['tasks']] for stage in stages],
            [['需求任务1'], ['设计任务1']],
        )
        self.assertEqual(response.context['progress_overview']['total_tasks'], 4)
        self.assertEqual(response.context['progress_overview']['completed_tasks'], 2)
        self.assertEqual(response.context['progress_overview']['overdue_milestones'], [])
        self.assertEqual(response.context['progress_overview']['overdue_milestone_count'], 0)
        self.assertNotIn('swimlanes', response.context['progress_overview'])
        timeline = response.context['progress_overview']['timeline']
        self.assertGreaterEqual(timeline['month_count'], 6)
        self.assertTrue(timeline['months'])
        self.assertIn('timeline_left_pct', stages[0])
        self.assertIn('timeline_width_pct', stages[0])
        self.assertEqual(stages[0]['assignee_name'], 'overview_owner')
        self.assertEqual(stages[1]['assignee_name'], 'overview_owner')

    def test_risk_panel_lists_only_overdue_milestones(self):
        today = timezone.localdate()
        started_at = timezone.now() - timedelta(days=12)
        overdue_milestone = Task.objects.get(
            product_stage=self.stage2,
            name='设计任务1',
        )
        overdue_milestone.started_at = started_at
        overdue_milestone.expected_end_date = today - timedelta(days=5)
        overdue_milestone.save(update_fields=['started_at', 'expected_end_date'])

        Task.objects.create(
            product_stage=self.stage2,
            name='普通延期任务',
            order=3,
            started_at=started_at,
            expected_end_date=today - timedelta(days=8),
            status='overdue',
            is_milestone=False,
        )
        Task.objects.create(
            product_stage=self.stage2,
            name='已完成历史里程碑',
            order=4,
            started_at=started_at,
            expected_end_date=today - timedelta(days=9),
            actual_end_date=timezone.now(),
            status='completed',
            is_milestone=True,
        )

        self.client.login(username='overview_owner', password='pass')
        response = self.client.get(reverse('product_progress_modal', args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'project-risk-panel has-risks')
        self.assertContains(response, 'class="project-risk-item"', count=1)
        self.assertContains(response, '1 项风险')
        self.assertContains(response, '已延期 5 天')
        self.assertNotContains(response, '暂无延期里程碑')

        risks = response.context['progress_overview']['overdue_milestones']
        self.assertEqual(response.context['progress_overview']['overdue_milestone_count'], 1)
        self.assertEqual([risk['name'] for risk in risks], ['设计任务1'])
        self.assertEqual(risks[0]['stage_name'], '包装设计')
        self.assertEqual(risks[0]['assignee_name'], 'overview_owner')
        self.assertEqual(risks[0]['expected_end_date'], today - timedelta(days=5))
        self.assertEqual(risks[0]['overdue_days'], 5)

    def test_pending_stage_is_displayed_completed_when_all_tasks_are_completed(self):
        self.stage2.status = 'pending'
        self.stage2.save(update_fields=['status'])
        self.stage2.tasks.update(status='completed', actual_end_date=timezone.now())

        self.client.login(username='overview_owner', password='pass')
        response = self.client.get(reverse('product_progress_modal', args=[self.product.pk]))

        stages = response.context['progress_overview']['stages']
        self.assertEqual(stages[1]['status_key'], 'completed')
        self.assertEqual(stages[1]['status_label'], '已完成')
        self.assertEqual(response.context['progress_overview']['completed_stages'], 2)
        self.assertContains(response, 'class="roadmap-stage-row is-completed', count=2)

        self.stage2.refresh_from_db()
        self.assertEqual(self.stage2.status, 'pending')

    def test_pending_stage_is_displayed_in_progress_after_work_has_started(self):
        self.stage2.status = 'pending'
        self.stage2.save(update_fields=['status'])
        started_task = self.stage2.tasks.get(name='设计任务1')
        started_task.status = 'completed'
        started_task.actual_end_date = timezone.now()
        started_task.save(update_fields=['status', 'actual_end_date'])

        self.client.login(username='overview_owner', password='pass')
        response = self.client.get(reverse('product_progress_modal', args=[self.product.pk]))

        stages = response.context['progress_overview']['stages']
        self.assertEqual(stages[1]['status_key'], 'in_progress')
        self.assertEqual(stages[1]['status_label'], '进行中')
        self.assertEqual(response.context['progress_overview']['active_stage_text'], '包装设计')

        self.stage2.refresh_from_db()
        self.assertEqual(self.stage2.status, 'pending')

    def test_progress_png_export_returns_downloadable_image(self):
        self.client.login(username='overview_owner', password='pass')
        response = self.client.get(
            reverse('product_progress_export_png', args=[self.product.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertTrue(response.content.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertIn("filename*=UTF-8''", response['Content-Disposition'])
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertGreater(len(response.content), 10000)

        image = Image.open(BytesIO(response.content))
        self.assertEqual(image.format, 'PNG')
        self.assertGreaterEqual(image.width, 1600)
        self.assertGreater(image.height, 800)

    def test_progress_png_uses_dark_risk_panel_and_orange_risk_card(self):
        today = timezone.localdate()
        milestone = self.stage2.tasks.get(name='设计任务1')
        milestone.started_at = timezone.now() - timedelta(days=8)
        milestone.expected_end_date = today - timedelta(days=3)
        milestone.save(update_fields=['started_at', 'expected_end_date'])

        self.client.login(username='overview_owner', password='pass')
        response = self.client.get(
            reverse('product_progress_export_png', args=[self.product.pk])
        )

        self.assertEqual(response.status_code, 200)
        image = Image.open(BytesIO(response.content)).convert('RGB')
        stage_rows_height = max(150, 2 * 66)
        risk_y = 30 + 204 + 18 + (52 + stage_rows_height + 58) + 18
        panel_pixel = image.getpixel((100, risk_y + 12))
        card_pixel = image.getpixel((300, risk_y + 88))

        self.assertLess(panel_pixel[0], panel_pixel[1])
        self.assertLess(panel_pixel[1], panel_pixel[2])
        self.assertLess(max(panel_pixel), 150)
        self.assertGreater(card_pixel[0], 180)
        self.assertLess(card_pixel[1], 170)
        self.assertLess(card_pixel[2], 120)

    def test_overview_identifies_active_project_without_active_stage(self):
        self.stage2.status = 'pending'
        self.stage2.save(update_fields=['status'])
        self.client.login(username='overview_owner', password='pass')
        response = self.client.get(reverse('product_progress_modal', args=[self.product.pk]))

        self.assertContains(response, '暂无进行中阶段')
        self.assertEqual(response.context['progress_overview']['status_label'], '待启动阶段')

    def test_many_stages_are_all_rendered_in_one_timeline(self):
        for order in range(3, 9):
            ProductStage.objects.create(
                product=self.product, name=f'扩展阶段{order}', order=order,
                department=self.dept, status='pending',
            )

        self.client.login(username='overview_owner', password='pass')
        response = self.client.get(reverse('product_progress_modal', args=[self.product.pk]))

        self.assertContains(response, '--roadmap-months:')
        self.assertContains(response, 'class="roadmap-stage-row is-', count=8)
        self.assertContains(response, 'class="roadmap-stage-number"', count=8)
        self.assertContains(response, '>08</span>')
        self.assertContains(response, '扩展阶段8')

    def test_product_info_modal_is_separate_from_progress_overview(self):
        self.client.login(username='overview_owner', password='pass')
        response = self.client.get(reverse('product_info_modal', args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-field="assignee"')
        self.assertContains(response, '产品资料')
        self.assertNotContains(response, 'project-progress-overview')
        self.assertNotContains(response, 'roadmap-overview')
        self.assertNotContains(response, 'roadmap-timeline-card')
        self.assertNotContains(response, '产品生命周期路线图')


class TemplateCommentLintTest(SimpleTestCase):
    """全项目模板扫一遍「多行 {# ... #}」—— 放在这里是因为这个坑是在本文件旁边的
    _product_info.html 上踩出来的，但它对**所有**模板都成立。

    Django 的注释正则是 `{#.*?#}` 且**没有 re.DOTALL**，`.` 不匹配换行，
    所以跨行的 `{# ... #}` 压根不被当成注释，会被**原样输出到页面上**。
    失败是静默的：页面照常渲染，只是多出一段注释文字，只有肉眼能发现。
    多行注释必须写成 {% comment %} ... {% endcomment %}。
    """

    def test_no_multiline_hash_comments(self):
        offenders = []
        root = Path(settings.BASE_DIR, 'templates')
        for path in sorted(root.rglob('*.html')):
            text = path.read_text(encoding='utf-8')
            for match in re.finditer(r'\{#', text):
                end = text.find('#}', match.start())
                if end == -1:
                    continue
                if '\n' in text[match.start():end]:
                    line = text[:match.start()].count('\n') + 1
                    offenders.append(f'{path.relative_to(settings.BASE_DIR)}:{line}')
        self.assertEqual(
            offenders, [],
            '这些模板用了多行 {# #}，会被原样渲染到页面上，改用 {% comment %}：'
            + '、'.join(offenders))


class ProductActionButtonsTest(TestCase):
    """详情页 / 信息弹窗的操作按钮：取消该品保留，彻底删除已下线。

    删除整个项目会连带抹掉阶段、任务、附件和操作日志，产品系统不再提供这个入口；
    「取消该品」是唯一的关停方式，历史记录保留。
    """

    def setUp(self):
        self.dept = Department.objects.create(name='策划部')
        self.admin = User.objects.create_user(username='admin', password='pass')
        self.admin.profile.role = 'admin'
        self.admin.profile.department = self.dept
        self.admin.profile.save()
        self.owner = User.objects.create_user(username='owner', password='pass')
        self.owner.profile.department = self.dept
        self.owner.profile.save()
        self.outsider = User.objects.create_user(username='outsider', password='pass')
        self.outsider.profile.department = self.dept
        self.outsider.profile.save()

        st = StageTemplate.objects.create(name='阶段1', order=1, department=self.dept)
        TaskTemplate.objects.create(stage_template=st, name='任务A', order=1)

    def _make_product(self, status, assignee=None):
        product = Product.objects.create(
            name=f'测试品-{status}', creator=self.admin, assignee=assignee, status=status,
        )
        product.create_stages_from_templates()
        return product

    def _detail(self, product, username):
        self.client.login(username=username, password='pass')
        return self.client.get(reverse('product_detail', args=[product.pk]))

    def test_active_product_shows_cancel_but_no_delete(self):
        product = self._make_product('active', assignee=self.owner)
        resp = self._detail(product, 'owner')
        self.assertContains(resp, '取消该品')
        self.assertNotContains(resp, '删除该品')

    def test_cancelled_product_hides_all_removal_buttons(self):
        product = self._make_product('cancelled', assignee=self.owner)
        resp = self._detail(product, 'owner')
        self.assertNotContains(resp, '取消该品')
        self.assertNotContains(resp, '删除该品')

    def test_outsider_sees_no_action_buttons(self):
        """外部人（既非管理员也非品负责人）不该看到操作按钮。"""
        product = self._make_product('cancelled', assignee=self.owner)
        resp = self._detail(product, 'outsider')
        self.assertNotContains(resp, '删除该品')
        self.assertNotContains(resp, '取消该品')

    def test_info_modal_has_no_delete_button(self):
        """信息弹窗也走同一份模板，同样不能出现删除入口。"""
        product = self._make_product('active', assignee=self.owner)
        self.client.login(username='owner', password='pass')
        resp = self.client.get(reverse('product_info_modal', args=[product.pk]))
        self.assertContains(resp, '取消该品')
        self.assertNotContains(resp, '删除该品')

    def test_delete_endpoint_is_gone(self):
        """删除接口连同 URL 一起下线：旧链接不能再删掉项目。"""
        product = self._make_product('cancelled', assignee=self.owner)
        self.client.login(username='admin', password='pass')

        resp = self.client.post(f'/products/{product.pk}/delete/')

        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())


class UpwardNotificationTriggerTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='测试部门2')
        self.stage_owner = User.objects.create(username='so2', first_name='阶段负责人2')
        self.stage_owner.profile.wechat_userid = 'wx_so2'
        self.stage_owner.profile.save()
        self.product_owner = User.objects.create(username='po2', first_name='品负责人2')
        self.product_owner.profile.wechat_userid = 'wx_po2'
        self.product_owner.profile.save()
        self.task_assignee = User.objects.create(username='ta2', first_name='任务人2')

        self.product = Product.objects.create(
            name='触发测试品', creator=self.task_assignee, assignee=self.product_owner,
        )
        self.stage = ProductStage.objects.create(
            product=self.product, name='触发测试阶段', department=self.dept,
            assignee=self.stage_owner, order=1,
        )
        self.task = Task.objects.create(
            product_stage=self.stage, name='触发测试任务', assignee=self.task_assignee, order=1,
        )

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_mark_completed_triggers_notify(self, mock_send):
        self.task.mark_completed()
        mock_send.assert_called_once()

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_mark_overdue_triggers_notify(self, mock_send):
        self.task.mark_overdue()
        mock_send.assert_called_once()

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_stage_complete_triggers_notify(self, mock_send):
        # 阶段下唯一任务先标记完成，再触发阶段完成
        self.task.mark_completed()
        mock_send.reset_mock()
        self.stage.complete()

        contents = [c[0][1] for c in mock_send.call_args_list]
        # 阶段完成通知品负责人
        self.assertTrue(any(
            c.startswith('品负责人2你好') and '“触发测试阶段”阶段已完成' in c
            for c in contents
        ))
        # 这是最后一个阶段，项目随之完成，相关人都会收到项目完成通知
        self.assertTrue(any('恭喜触发测试品项目已顺利完成' in c for c in contents))

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_last_stage_complete_triggers_product_notify_to_admins(self, mock_send):
        """品下所有阶段完成后，品自动置为 completed，应触发对所有管理员的 notify_upward。"""
        admin = User.objects.create(username='admin2', first_name='管理员2')
        admin.profile.wechat_userid = 'wx_admin2'
        admin.profile.role = 'admin'
        admin.profile.save()

        stage2 = ProductStage.objects.create(
            product=self.product, name='触发测试阶段2', department=self.dept,
            assignee=self.stage_owner, order=2,
        )

        # 第一个阶段模拟已完成（不通过 complete() 走完整流程，避免干扰断言）
        self.stage.status = 'completed'
        self.stage.save(update_fields=['status'])

        mock_send.reset_mock()
        stage2.complete()

        self.product.refresh_from_db()
        self.assertEqual(self.product.status, 'completed')
        mock_send.assert_called()
        called_userids = [call.args[0] for call in mock_send.call_args_list]
        self.assertIn('wx_admin2', called_userids)


class ProductCreatePublishTest(TestCase):
    """新建项目直接发布：不再有「创建草稿 → 手动发布」这一道。"""

    def setUp(self):
        self.dept = Department.objects.create(name='策划部')
        self.admin = User.objects.create_user(
            username='create-admin', password='pass',
        )
        self.admin.profile.role = 'admin'
        self.admin.profile.department = self.dept
        self.admin.profile.save()
        self.owner = User.objects.create_user(
            username='create-owner', password='pass', first_name='负责人',
        )
        self.owner.profile.department = self.dept
        self.owner.profile.save()

        st = StageTemplate.objects.create(name='阶段1', order=1, department=self.dept)
        TaskTemplate.objects.create(stage_template=st, name='任务A', order=1)
        StageTemplate.objects.create(name='阶段2', order=2, department=self.dept)
        self.client.login(username='create-admin', password='pass')

    def _create_data(self, name='新品'):
        data = {
            'name': name,
            'assignee': self.owner.pk,
            'started_at': timezone.localdate().isoformat(),
            'expected_end_date': (timezone.localdate() + timedelta(days=60)).isoformat(),
            'brand': '跨境自营',
            'category': '新品开发',
        }
        for tmpl in StageTemplate.objects.all():
            data[f'stage_assignee_{tmpl.pk}'] = self.owner.pk
        return data

    def _post_create(self, name='新品'):
        return self.client.post(reverse('product_create'), self._create_data(name))

    def test_new_product_is_published_immediately(self):
        response = self._post_create()
        product = Product.objects.get(name='新品')

        self.assertEqual(response.status_code, 302)
        # 创建成功后直接回看板，不再进详情页/弹设置窗口
        self.assertEqual(response.url, reverse('kanban'))
        self.assertEqual(product.status, 'active')
        first_stage = product.stages.order_by('order').first()
        self.assertEqual(first_stage.status, 'in_progress')
        self.assertIsNotNone(first_stage.started_at)
        self.assertEqual(product.current_stage_order, 1)

    def test_stage_setup_from_second_step_is_applied(self):
        """第二步填的阶段负责人/时间要落到生成的阶段上。"""
        data = self._create_data('分工落库')
        stage2_tmpl = StageTemplate.objects.get(order=2)
        data[f'stage_assignee_{stage2_tmpl.pk}'] = self.owner.pk
        data[f'stage_expected_end_date_{stage2_tmpl.pk}'] = (
            timezone.localdate() + timedelta(days=30)
        ).isoformat()

        self.client.post(reverse('product_create'), data)

        stage2 = Product.objects.get(name='分工落库').stages.get(order=2)
        self.assertEqual(stage2.assignee, self.owner)
        self.assertEqual(
            stage2.expected_end_date, timezone.localdate() + timedelta(days=30),
        )

    def test_missing_stage_assignee_blocks_creation(self):
        """第二步漏填阶段负责人：提示并禁止创建项目。"""
        data = self._create_data('漏填阶段负责人')
        stage2_tmpl = StageTemplate.objects.get(order=2)
        data[f'stage_assignee_{stage2_tmpl.pk}'] = ''

        response = self.client.post(reverse('product_create'), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '负责人不能为空')
        self.assertFalse(Product.objects.filter(name='漏填阶段负责人').exists())

    @patch('products.views._notify_project_kickoff')
    def test_repeated_submit_does_not_create_duplicate_products(self, mock_notify):
        """网络慢时连续点「创建并发布」，同一份表单只应建出一个项目。"""
        self._post_create('重复提交')
        second = self._post_create('重复提交')

        product = Product.objects.get(name='重复提交')
        self.assertEqual(Product.objects.filter(name='重复提交').count(), 1)
        self.assertEqual(second.url, reverse('kanban'))

    def test_create_page_is_two_steps_and_requires_stage_owners(self):
        """创建页第一步是「下一步」，第二步列出各阶段负责人（必填）。"""
        body = self.client.get(reverse('product_create')).content.decode()

        self.assertIn('下一步', body)
        self.assertIn('上一步', body)
        self.assertIn('stage_assignee_', body)
        self.assertIn('阶段1', body)
        self.assertIn('阶段2', body)

    def test_detail_page_shows_only_one_publish_button_for_legacy_draft(self):
        """草稿项目详情页上只能有一个「发布项目」按钮（详情页头 + 资料区块各放过一个）。"""
        product = Product.objects.create(
            name='遗留草稿', creator=self.admin, assignee=self.owner,
            status='draft',
        )
        ProductStage.objects.create(
            product=product, name='阶段1', order=1, department=self.dept,
        )

        body = self.client.get(
            reverse('product_detail', args=[product.pk]),
        ).content.decode()

        self.assertEqual(body.count('发布项目'), 1)


class ProjectKickoffNotificationTest(TestCase):
    """立项通知：项目负责人一条，各阶段负责人各一条（去重、排除项目负责人）。"""

    def setUp(self):
        self.dept = Department.objects.create(name='立项通知部')
        self.creator = User.objects.create_user(
            username='kickoff-creator', password='pass',
        )
        self.creator.profile.role = 'admin'
        self.creator.profile.department = self.dept
        self.creator.profile.save()

        self.owner = User.objects.create_user(
            username='kickoff-owner', password='pass', first_name='张三',
        )
        self.owner.profile.department = self.dept
        self.owner.profile.wechat_userid = 'wx_owner'
        self.owner.profile.save()

        self.stage_owner = User.objects.create_user(
            username='kickoff-stage-owner', password='pass', first_name='李四',
        )
        self.stage_owner.profile.department = self.dept
        self.stage_owner.profile.wechat_userid = 'wx_stage_owner'
        self.stage_owner.profile.save()

        StageTemplate.objects.create(
            name='立项', order=1, department=self.dept,
            default_assignee=self.owner,
        )
        StageTemplate.objects.create(
            name='包装设计', order=2, department=self.dept,
            default_assignee=self.stage_owner,
        )
        self.client.login(username='kickoff-creator', password='pass')

    def _create(self, name='立项通知品'):
        data = {
            'name': name,
            'assignee': self.owner.pk,
            'started_at': timezone.localdate().isoformat(),
            'expected_end_date': (
                timezone.localdate() + timedelta(days=60)
            ).isoformat(),
            'brand': '跨境自营',
            'category': '新品开发',
        }
        # 第二步：每个阶段都要指定负责人（用模板默认的，没默认的给阶段负责人）
        for tmpl in StageTemplate.objects.all():
            data[f'stage_assignee_{tmpl.pk}'] = (
                tmpl.default_assignee_id or self.stage_owner.pk
            )
        return self.client.post(reverse('product_create'), data)

    def _sent(self, mock_send):
        """{企微 userid: 消息正文}，便于按收件人断言。"""
        return {call.args[0]: call.args[1] for call in mock_send.call_args_list}

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_project_owner_and_stage_owner_each_get_one_message(self, mock_send):
        self._create()

        sent = self._sent(mock_send)
        self.assertEqual(set(sent), {'wx_owner', 'wx_stage_owner'})

        owner_text = sent['wx_owner']
        self.assertIn('张三你好，我是项目管理智能机器人。', owner_text)
        self.assertIn('你负责的项目“立项通知品”已立项。', owner_text)
        self.assertIn(
            f'开始时间：{timezone.localdate().isoformat()}　'
            f'预计结束：{(timezone.localdate() + timedelta(days=60)).isoformat()}',
            owner_text,
        )
        self.assertIn('共 2 个阶段，首个阶段“立项”已开始。', owner_text)
        self.assertIn(f'点击查看：{settings.SITE_URL}', owner_text)

        stage_text = sent['wx_stage_owner']
        self.assertIn('李四你好，我是项目管理智能机器人。', stage_text)
        self.assertIn('你被安排为“立项通知品”项目“包装设计”阶段的负责人。', stage_text)
        self.assertIn('阶段预计结束：待填写', stage_text)
        self.assertIn(f'点击查看：{settings.SITE_URL}', stage_text)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_one_message_per_stage_owner_after_dedupe(self, mock_send):
        StageTemplate.objects.create(
            name='打样', order=3, department=self.dept,
            default_assignee=self.stage_owner,
        )

        self._create()

        sent = self._sent(mock_send)
        self.assertEqual(len(mock_send.call_args_list), 2)  # 项目负责人 + 阶段负责人
        stage_text = sent['wx_stage_owner']
        self.assertIn('“包装设计”“打样”阶段的负责人', stage_text)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_stage_owner_without_wechat_is_skipped(self, mock_send):
        self.stage_owner.profile.wechat_userid = ''
        self.stage_owner.profile.save()

        response = self._create()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(self._sent(mock_send)), {'wx_owner'})
        self.assertFalse(
            ActivityLog.objects.filter(action='发送立项通知', target_id=self.stage_owner.pk).exists()
        )

    @patch('products.views.send_message_to_user', side_effect=RuntimeError('企微挂了'))
    def test_send_failure_does_not_break_product_creation(self, mock_send):
        response = self._create('发送失败也建成功')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Product.objects.filter(name='发送失败也建成功').exists())
        self.assertEqual(mock_send.call_count, 2)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_project_owner_who_also_owns_a_stage_gets_no_duplicate(self, mock_send):
        """项目负责人同时是阶段负责人：只发项目立项那一条。"""
        StageTemplate.objects.filter(name='包装设计').update(default_assignee=self.owner)

        self._create()

        sent = self._sent(mock_send)
        self.assertEqual(list(sent), ['wx_owner'])
        self.assertEqual(mock_send.call_count, 1)


class ProductCreatePermissionTest(TestCase):
    """建项目的权限：管理员或「产品部」成员。

    新建入口统一在左侧导航「创建新品」：所有登录用户都能看见，
    没有权限的人点击后由后端提示无权限。看板右上角不再单独放按钮。
    """

    def setUp(self):
        self.product_dept = Department.objects.create(name='产品部')
        self.other_dept = Department.objects.create(name='策划部')

        self.product_member = User.objects.create_user(
            username='product-member', password='pass',
        )
        self.product_member.profile.department = self.product_dept
        self.product_member.profile.save()

        self.outsider = User.objects.create_user(
            username='create-outsider', password='pass',
        )
        self.outsider.profile.department = self.other_dept
        self.outsider.profile.save()

    def test_product_department_member_sees_sidebar_create_entry(self):
        self.client.login(username='product-member', password='pass')

        response = self.client.get(reverse('kanban'))

        # 新建入口只在左侧导航「创建新品」，看板右上角不再重复放按钮
        self.assertContains(response, '创建新品')
        self.assertNotContains(response, '新建项目')

    def test_product_department_member_can_open_the_create_page(self):
        self.client.login(username='product-member', password='pass')

        response = self.client.get(reverse('product_create'))

        self.assertEqual(response.status_code, 200)

    def test_other_department_member_sees_entry_and_gets_no_permission_hint(self):
        """入口对所有人可见，点了才提示无权限，而不是直接 403。"""
        self.client.login(username='create-outsider', password='pass')

        kanban = self.client.get(reverse('kanban'))
        self.assertContains(kanban, '创建新品')
        self.assertNotContains(kanban, '新建项目')

        response = self.client.get(reverse('product_create'), follow=True)
        self.assertRedirects(response, reverse('kanban'))
        self.assertContains(response, '你没有新建项目的权限')

    def test_outsider_cannot_create_project_even_by_posting(self):
        """页面提示之外，后端仍然拦住建项目的 POST。"""
        self.client.login(username='create-outsider', password='pass')
        before = Product.objects.count()

        response = self.client.post(
            reverse('product_create'),
            {'name': '偷偷建的项目', 'assignee': self.outsider.pk},
            follow=True,
        )

        self.assertRedirects(response, reverse('kanban'))
        self.assertContains(response, '你没有新建项目的权限')
        self.assertEqual(Product.objects.count(), before)
        self.assertFalse(Product.objects.filter(name='偷偷建的项目').exists())

    def test_assignee_picker_only_lists_active_users(self):
        """人员选择只显示在职员工，离职员工不出现在下拉候选里。"""
        User.objects.create_user(
            username='active-picker', password='pass', first_name='在职员工',
        )
        User.objects.create_user(
            username='left-picker', password='pass', first_name='离职员工',
            is_active=False,
        )
        self.client.login(username='product-member', password='pass')

        response = self.client.get(reverse('product_create'))

        self.assertContains(response, '在职员工')
        self.assertNotContains(response, '离职员工')


class ProductSetupStagesTest(TestCase):
    """新建项目后的「设置项目流程」窗口：一次性设置阶段负责人和起止时间。"""

    def setUp(self):
        self.dept = Department.objects.create(name='产品部')
        self.other_dept = Department.objects.create(name='策划部')

        self.creator = User.objects.create_user(username='setup-creator', password='pass')
        self.creator.profile.department = self.dept
        self.creator.profile.save()

        self.owner = User.objects.create_user(username='setup-owner', password='pass')
        self.owner.profile.department = self.other_dept
        self.owner.profile.save()

        # 产品部普通同事：能看到项目（本部门有进行中阶段），但不是创建人/负责人
        self.outsider = User.objects.create_user(username='setup-outsider', password='pass')
        self.outsider.profile.department = self.dept
        self.outsider.profile.save()

        self.today = timezone.localdate()
        self.product = Product.objects.create(
            name='设置流程品', creator=self.creator, assignee=self.owner,
            status='active', started_at=timezone.now(),
            expected_end_date=self.today + timedelta(days=60),
        )
        self.stage1 = ProductStage.objects.create(
            product=self.product, name='阶段1', order=1,
            department=self.dept, status='in_progress',
        )
        self.stage2 = ProductStage.objects.create(
            product=self.product, name='阶段2', order=2,
            department=self.dept, status='pending',
        )

    def _post(self, **overrides):
        data = {
            f'assignee_{self.stage1.pk}': str(self.owner.pk),
            f'started_at_{self.stage1.pk}': self.today.isoformat(),
            f'expected_end_date_{self.stage1.pk}': (
                self.today + timedelta(days=10)
            ).isoformat(),
            f'assignee_{self.stage2.pk}': str(self.outsider.pk),
            f'started_at_{self.stage2.pk}': '',
            f'expected_end_date_{self.stage2.pk}': '',
        }
        data.update(overrides)
        return self.client.post(
            reverse('product_setup_stages', args=[self.product.pk]), data,
        )

    def test_creator_sees_the_setup_window(self):
        self.client.login(username='setup-creator', password='pass')

        response = self.client.get(
            reverse('product_detail', args=[self.product.pk]), {'setup': '1'},
        )

        self.assertContains(response, '设置项目流程')
        self.assertContains(response, 'setup-stages-form')

    def test_outsider_does_not_see_the_setup_window(self):
        self.client.login(username='setup-outsider', password='pass')

        response = self.client.get(
            reverse('product_detail', args=[self.product.pk]), {'setup': '1'},
        )

        self.assertNotContains(response, 'setup-stages-form')

    def test_setup_saves_assignee_and_dates(self):
        self.client.login(username='setup-creator', password='pass')

        response = self._post()

        self.assertEqual(response.status_code, 302)
        self.stage1.refresh_from_db()
        self.assertEqual(self.stage1.assignee_id, self.owner.pk)
        self.assertEqual(timezone.localtime(self.stage1.started_at).date(), self.today)
        self.assertEqual(self.stage1.expected_end_date, self.today + timedelta(days=10))

    def test_blank_stage_assignee_is_rejected(self):
        """设置流程时也不允许把阶段负责人清空。"""
        self.client.login(username='setup-creator', password='pass')

        response = self._post(**{f'assignee_{self.stage2.pk}': ''})

        self.assertEqual(response.status_code, 302)
        self.assertIn('?setup=1', response.url)
        self.stage1.refresh_from_db()
        self.assertIsNone(self.stage1.assignee_id)
        self.stage2.refresh_from_db()
        self.assertIsNone(self.stage2.assignee_id)

    def test_start_after_end_is_rejected(self):
        self.client.login(username='setup-creator', password='pass')

        response = self._post(**{
            f'started_at_{self.stage1.pk}': (self.today + timedelta(days=20)).isoformat(),
            f'expected_end_date_{self.stage1.pk}': self.today.isoformat(),
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn('?setup=1', response.url)
        self.stage1.refresh_from_db()
        self.assertIsNone(self.stage1.expected_end_date)

    def test_outsider_cannot_submit_setup(self):
        self.client.login(username='setup-outsider', password='pass')

        response = self._post()

        self.assertEqual(response.status_code, 403)
        self.stage1.refresh_from_db()
        self.assertIsNone(self.stage1.assignee_id)

    @patch('products.views.send_message_to_user', return_value=True)
    def test_setup_assignment_notification_includes_dates(self, mock_send):
        """分工时新负责人收到的提醒要带阶段起止时间，而不只是一句“请关注”。"""
        self.owner.profile.wechat_userid = 'wx_setup_owner'
        self.owner.profile.save()
        self.client.login(username='setup-creator', password='pass')

        self._post()

        mock_send.assert_called_once()
        recipient, content = mock_send.call_args.args
        self.assertEqual(recipient, self.owner)
        self.assertIn(f'开始时间：{self.today.isoformat()}', content)
        self.assertIn(
            f'预计结束：{(self.today + timedelta(days=10)).isoformat()}',
            content,
        )


class ProductPauseResumeTest(TestCase):
    """暂停项目：状态变 paused，只影响通知，不动阶段/任务。"""

    def setUp(self):
        self.dept = Department.objects.create(name='暂停测试部')
        self.admin = User.objects.create_user(username='pause-admin', password='pass')
        self.admin.profile.role = 'admin'
        self.admin.profile.department = self.dept
        self.admin.profile.save()
        self.owner = User.objects.create_user(username='pause-owner', password='pass')
        self.owner.profile.department = self.dept
        self.owner.profile.save()
        self.outsider = User.objects.create_user(username='pause-outsider', password='pass')
        self.outsider.profile.department = self.dept
        self.outsider.profile.save()

        self.product = Product.objects.create(
            name='暂停品', creator=self.admin, assignee=self.owner,
        )
        self.stage = ProductStage.objects.create(
            product=self.product, name='阶段1', order=1, department=self.dept,
            status='in_progress',
        )

    def test_owner_can_pause_and_resume(self):
        self.client.login(username='pause-owner', password='pass')

        paused = self.client.post(reverse('product_pause', args=[self.product.pk]))
        self.product.refresh_from_db()
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(self.product.status, 'paused')
        # 阶段不受影响，恢复后能接着做
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, 'in_progress')

        resumed = self.client.post(reverse('product_resume', args=[self.product.pk]))
        self.product.refresh_from_db()
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(self.product.status, 'active')

    def test_only_active_can_be_paused(self):
        self.product.status = 'completed'
        self.product.save()
        self.client.login(username='pause-admin', password='pass')

        response = self.client.post(reverse('product_pause', args=[self.product.pk]))

        self.assertEqual(response.status_code, 400)

    def test_outsider_cannot_pause(self):
        self.client.login(username='pause-outsider', password='pass')

        response = self.client.post(reverse('product_pause', args=[self.product.pk]))

        self.assertEqual(response.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.status, 'active')

    def test_paused_product_can_still_be_cancelled(self):
        self.product.status = 'paused'
        self.product.save()
        self.client.login(username='pause-admin', password='pass')

        response = self.client.post(reverse('product_cancel', args=[self.product.pk]))

        self.product.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.product.status, 'cancelled')
        # 取消时间写入 actual_end_date，项目归档页按它排序/筛选
        self.assertIsNotNone(self.product.actual_end_date)


class ProductAutoTodoTest(TestCase):
    """暂停/取消的项目不该在待办里占位，恢复后要按当前负责人重建。"""

    def setUp(self):
        self.dept = Department.objects.create(name='待办部')
        self.admin = User.objects.create_user(username='todo-admin', password='pass')
        self.admin.profile.role = 'admin'
        self.admin.profile.department = self.dept
        self.admin.profile.save()
        self.owner = User.objects.create_user(username='todo-owner', password='pass')
        self.owner.profile.department = self.dept
        self.owner.profile.save()
        self.stage_owner = User.objects.create_user(username='todo-stage', password='pass')
        self.stage_owner.profile.department = self.dept
        self.stage_owner.profile.save()
        self.executor = User.objects.create_user(username='todo-exec', password='pass')
        self.executor.profile.department = self.dept
        self.executor.profile.save()

        self.product = Product.objects.create(
            name='待办品', creator=self.admin, assignee=self.owner, status='active',
        )
        self.stage = ProductStage.objects.create(
            product=self.product, name='阶段1', order=1, department=self.dept,
            assignee=self.stage_owner, status='in_progress',
        )
        self.task = Task.objects.create(
            product_stage=self.stage, name='任务1', order=1, assignee=self.executor,
        )
        self.product.sync_all_auto_todos()

    def _auto_todo_users(self):
        return set(
            TodoItem.objects.filter(is_auto=True).values_list('user_id', flat=True)
        )

    def test_sync_creates_one_todo_per_level(self):
        self.assertEqual(TodoItem.objects.filter(is_auto=True).count(), 3)
        self.assertEqual(
            self._auto_todo_users(),
            {self.owner.pk, self.stage_owner.pk, self.executor.pk},
        )

    def test_pause_removes_all_auto_todos(self):
        self.client.login(username='todo-owner', password='pass')

        response = self.client.post(reverse('product_pause', args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TodoItem.objects.filter(is_auto=True).exists())

    def test_resume_recreates_auto_todos(self):
        self.product.pause()
        self.assertFalse(TodoItem.objects.filter(is_auto=True).exists())

        self.product.resume()

        self.product.refresh_from_db()
        self.assertEqual(self.product.status, 'active')
        self.assertEqual(TodoItem.objects.filter(is_auto=True).count(), 3)
        self.assertEqual(
            self._auto_todo_users(),
            {self.owner.pk, self.stage_owner.pk, self.executor.pk},
        )

    def test_cancel_removes_all_auto_todos(self):
        self.client.login(username='todo-admin', password='pass')

        response = self.client.post(reverse('product_cancel', args=[self.product.pk]))

        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.status, 'cancelled')
        self.assertFalse(TodoItem.objects.filter(is_auto=True).exists())

    def test_sync_does_not_touch_manual_todos(self):
        manual = TodoItem.objects.create(user=self.owner, content='自己加的待办')

        self.product.pause()

        self.assertTrue(TodoItem.objects.filter(pk=manual.pk).exists())
        self.assertFalse(TodoItem.objects.filter(is_auto=True).exists())

    def test_sync_while_paused_does_not_recreate(self):
        """暂停期间改了负责人/时间，同步也不会把待办又建回来。"""
        self.product.pause()
        self.task.assignee = self.stage_owner
        self.task.save(update_fields=['assignee'])

        self.task.sync_auto_todo()
        self.stage.sync_auto_todo()
        self.product.sync_auto_todo()

        self.assertFalse(TodoItem.objects.filter(is_auto=True).exists())


class TaskAssigneePermissionTest(TestCase):
    """任务执行人不能改自己的负责人；管理员/品负责人/阶段负责人可以。"""

    def setUp(self):
        self.dept = Department.objects.create(name='执行部')
        self.admin = User.objects.create_user(username='asg-admin', password='pass')
        self.admin.profile.role = 'admin'
        self.admin.profile.department = self.dept
        self.admin.profile.save()
        self.product_owner = User.objects.create_user(
            username='asg-po', password='pass', first_name='品负责人',
        )
        self.product_owner.profile.department = self.dept
        self.product_owner.profile.save()
        self.stage_owner = User.objects.create_user(
            username='asg-so', password='pass', first_name='阶段负责人',
        )
        self.stage_owner.profile.department = self.dept
        self.stage_owner.profile.save()
        self.executor = User.objects.create_user(
            username='asg-exec', password='pass', first_name='执行人',
        )
        self.executor.profile.department = self.dept
        self.executor.profile.save()
        self.other = User.objects.create_user(
            username='asg-other', password='pass', first_name='其他人',
        )
        self.other.profile.department = self.dept
        self.other.profile.save()

        self.product = Product.objects.create(
            name='执行人权限品', creator=self.admin,
            assignee=self.product_owner, status='active',
        )
        self.stage = ProductStage.objects.create(
            product=self.product, name='执行阶段', order=1,
            department=self.dept, assignee=self.stage_owner,
            status='in_progress', started_at=timezone.now(),
        )
        self.task = Task.objects.create(
            product_stage=self.stage, name='执行任务', order=1,
            assignee=self.executor, status='in_progress',
            started_at=timezone.now(),
            expected_end_date=timezone.localdate() + timedelta(days=7),
        )

    def _set_assignee(self, user, assignee):
        self.client.login(username=user.username, password='pass')
        return self.client.post(
            reverse('task_update_field', args=[self.task.pk]),
            {'field': 'assignee', 'value': assignee.pk if assignee else ''},
        )

    def test_executor_cannot_change_own_assignee(self):
        response = self._set_assignee(self.executor, self.other)

        self.assertEqual(response.status_code, 403)
        self.assertIn('执行人', response.json()['error'])
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.executor.pk)

    def test_executor_cannot_clear_own_assignee(self):
        response = self._set_assignee(self.executor, None)

        self.assertEqual(response.status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.executor.pk)

    def test_stage_owner_product_owner_and_admin_can_change_assignee(self):
        for user in (self.stage_owner, self.product_owner, self.admin):
            with self.subTest(user=user.username):
                self.task.assignee = self.executor
                self.task.save(update_fields=['assignee'])

                response = self._set_assignee(user, self.other)

                self.assertEqual(response.status_code, 200)
                self.task.refresh_from_db()
                self.assertEqual(self.task.assignee_id, self.other.pk)

    def test_executor_sees_assignee_as_readonly_text(self):
        self.client.login(username='asg-exec', password='pass')

        body = self.client.get(
            reverse('stage_detail_modal', args=[self.stage.pk]),
        ).content.decode()

        self.assertNotIn('data-field="assignee"', body)
        self.assertIn('执行人', body)

    def test_stage_owner_sees_editable_assignee(self):
        self.client.login(username='asg-so', password='pass')

        body = self.client.get(
            reverse('stage_detail_modal', args=[self.stage.pk]),
        ).content.decode()

        self.assertIn('data-field="assignee"', body)


class TaskPredecessorTest(TestCase):
    """前置任务：未完成时锁定开始，完成后自动排期并通知三方。"""

    def setUp(self):
        self.dept = Department.objects.create(name='前置任务部')
        self.stage_owner = User.objects.create_user(
            username='pred-stage', password='pass', first_name='阶段负责人',
        )
        self.stage_owner.profile.department = self.dept
        self.stage_owner.profile.wechat_userid = 'wx_pred_stage'
        self.stage_owner.profile.save()
        self.product_owner = User.objects.create_user(
            username='pred-product', password='pass', first_name='项目负责人',
        )
        self.product_owner.profile.department = self.dept
        self.product_owner.profile.wechat_userid = 'wx_pred_product'
        self.product_owner.profile.save()
        self.executor = User.objects.create_user(
            username='pred-executor', password='pass', first_name='执行人',
        )
        self.executor.profile.department = self.dept
        self.executor.profile.wechat_userid = 'wx_pred_executor'
        self.executor.profile.save()

        self.product = Product.objects.create(
            name='前置任务项目', creator=self.product_owner,
            assignee=self.product_owner, status='active',
        )
        self.stage = ProductStage.objects.create(
            product=self.product, name='设计阶段', order=1,
            department=self.dept, assignee=self.stage_owner,
            status='in_progress',
        )
        self.other_stage = ProductStage.objects.create(
            product=self.product, name='包装阶段', order=2,
            department=self.dept, assignee=self.stage_owner,
            status='pending',
        )
        self.predecessor = Task.objects.create(
            product_stage=self.stage, name='完成设计稿', order=1,
            assignee=self.executor,
        )
        self.dependent = Task.objects.create(
            product_stage=self.stage, name='开始打样', order=2,
            assignee=self.executor,
        )
        self.foreign_task = Task.objects.create(
            product_stage=self.other_stage, name='其他阶段任务', order=1,
        )

    def test_dependent_cannot_set_start_while_predecessor_incomplete(self):
        self.client.login(username='pred-stage', password='pass')
        response = self.client.post(
            reverse('task_update_field', args=[self.dependent.pk]),
            {'field': 'predecessor', 'value': self.predecessor.pk},
        )
        self.assertEqual(response.status_code, 200)

        start = timezone.localdate().isoformat()
        response = self.client.post(
            reverse('task_update_field', args=[self.dependent.pk]),
            {'field': 'started_at', 'value': start},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('未完成', response.json()['error'])

        self.dependent.refresh_from_db()
        self.assertIsNone(self.dependent.started_at)
        self.assertEqual(self.dependent.status, 'pending')

    def test_blocked_task_cannot_be_marked_completed(self):
        self.dependent.depends_on.set([self.predecessor])

        self.client.login(username='pred-executor', password='pass')
        response = self.client.post(
            reverse('task_complete', args=[self.dependent.pk]),
        )

        self.assertEqual(response.status_code, 400)
        self.dependent.refresh_from_db()
        self.assertEqual(self.dependent.status, 'pending')

    def test_cycle_is_rejected(self):
        self.dependent.depends_on.set([self.predecessor])

        self.client.login(username='pred-stage', password='pass')
        response = self.client.post(
            reverse('task_update_field', args=[self.predecessor.pk]),
            {'field': 'predecessor', 'value': self.dependent.pk},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('不能设为前置任务', response.json()['error'])
        self.predecessor.refresh_from_db()
        self.assertFalse(self.predecessor.depends_on.exists())

    def test_predecessor_can_come_from_another_stage(self):
        self.client.login(username='pred-stage', password='pass')
        response = self.client.post(
            reverse('task_update_field', args=[self.dependent.pk]),
            {'field': 'predecessor', 'value': self.foreign_task.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.dependent.refresh_from_db()
        self.assertEqual(
            list(self.dependent.depends_on.values_list('pk', flat=True)),
            [self.foreign_task.pk],
        )
        self.assertTrue(self.dependent.is_blocked)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_cross_stage_predecessor_completion_starts_dependent(self, mock_send):
        self.dependent.depends_on.set([self.foreign_task])

        self.foreign_task.mark_completed()

        self.dependent.refresh_from_db()
        self.assertIsNotNone(self.dependent.started_at)
        self.assertEqual(
            self.dependent.expected_end_date,
            timezone.localtime(self.dependent.started_at).date()
            + timedelta(days=7),
        )

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_completing_predecessor_starts_dependent_and_notifies(self, mock_send):
        self.dependent.depends_on.set([self.predecessor])

        self.predecessor.mark_completed()

        self.dependent.refresh_from_db()
        self.predecessor.refresh_from_db()
        self.assertIsNotNone(self.dependent.started_at)
        self.assertEqual(self.dependent.started_at, self.predecessor.actual_end_date)
        expected = (
            timezone.localtime(self.dependent.started_at).date()
            + timedelta(days=7)
        )
        self.assertEqual(self.dependent.expected_end_date, expected)
        self.assertEqual(self.dependent.status, 'in_progress')

        called_userids = [call.args[0] for call in mock_send.call_args_list]
        for userid in ('wx_pred_executor', 'wx_pred_stage', 'wx_pred_product'):
            self.assertIn(userid, called_userids)
        contents = [call.args[1] for call in mock_send.call_args_list]
        self.assertTrue(any(
            '“前置任务项目”·“设计阶段”子任务“开始打样”的前置任务'
            '“完成设计稿”已完成。' in content
            and '“开始打样”已自动开始' in content
            and '请您合理安排工作，把控进度，保证项目顺利完成。' in content
            for content in contents
        ))

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_completing_predecessor_without_roles_still_sets_dates(self, mock_send):
        """没有执行人/负责人也要自动排期，只是没人可通知。"""
        task = Task.objects.create(
            product_stage=self.stage, name='无负责人任务', order=3,
        )
        task.depends_on.set([self.predecessor])

        self.predecessor.mark_completed()

        task.refresh_from_db()
        self.assertIsNotNone(task.started_at)
        self.assertIsNotNone(task.expected_end_date)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_multiple_predecessors_must_all_complete_before_start(self, mock_send):
        """两个前置任务：只完成一个不解锁，全部完成才自动开始。"""
        second = Task.objects.create(
            product_stage=self.other_stage, name='确认包装', order=2,
            assignee=self.executor,
        )
        self.dependent.depends_on.set([self.predecessor, second])

        self.predecessor.mark_completed()
        self.dependent.refresh_from_db()
        self.assertIsNone(self.dependent.started_at)
        self.assertTrue(self.dependent.is_blocked)
        self.assertEqual(self.dependent.blocking_predecessor_name, '确认包装')

        second.mark_completed()
        self.dependent.refresh_from_db()
        self.assertIsNotNone(self.dependent.started_at)
        self.assertFalse(self.dependent.is_blocked)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_start_time_follows_latest_finishing_predecessor(self, mock_send):
        """开始时间以结束最晚的前置任务为准，而不是随便某个前置任务。"""
        earlier = Task.objects.create(
            product_stage=self.other_stage, name='早完成的任务', order=2,
        )
        earlier.status = 'completed'
        earlier.actual_end_date = timezone.now() - timedelta(days=5)
        earlier.save(update_fields=['status', 'actual_end_date'])
        self.dependent.depends_on.set([earlier, self.predecessor])

        self.predecessor.mark_completed()

        self.dependent.refresh_from_db()
        self.assertEqual(
            self.dependent.started_at, self.predecessor.actual_end_date,
        )
        self.assertEqual(
            self.dependent.expected_end_date,
            timezone.localtime(self.predecessor.actual_end_date).date()
            + timedelta(days=7),
        )

    def test_can_set_and_clear_multiple_predecessors_via_api(self):
        second = Task.objects.create(
            product_stage=self.other_stage, name='确认包装', order=2,
        )
        self.client.login(username='pred-stage', password='pass')

        response = self.client.post(
            reverse('task_update_field', args=[self.dependent.pk]),
            {'field': 'predecessor',
             'value': f'{self.predecessor.pk},{second.pk}'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(self.dependent.depends_on.values_list('pk', flat=True)),
            {self.predecessor.pk, second.pk},
        )

        response = self.client.post(
            reverse('task_update_field', args=[self.dependent.pk]),
            {'field': 'predecessor', 'value': ''},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.dependent.depends_on.exists())

    def test_stage_detail_renders_predecessor_column_and_lock(self):
        self.dependent.depends_on.set([self.predecessor])

        self.client.login(username='pred-stage', password='pass')
        response = self.client.get(
            reverse('stage_detail_modal', args=[self.stage.pk]),
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # 下拉按钮 + 复选框多选菜单
        self.assertIn('class="pred-dropdown"', body)
        self.assertIn('class="pred-menu"', body)
        self.assertIn('type="checkbox"', body)
        self.assertIn('待前置完成', body)
        self.assertIn('完成设计稿', body)
        # 跨阶段候选按阶段分组显示
        self.assertIn('class="pred-group">包装阶段', body)
        self.assertIn('其他阶段任务', body)
