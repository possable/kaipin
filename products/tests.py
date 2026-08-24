from io import BytesIO
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from accounts.models import Department
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


class ProductDeleteTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='策划部')

        self.admin = User.objects.create_user(username='admin', password='pass')
        self.admin.profile.department = self.dept
        self.admin.profile.role = 'admin'
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

    def test_admin_can_delete_draft(self):
        product = self._make_product('draft')
        self.client.login(username='admin', password='pass')
        resp = self.client.post(reverse('product_delete', args=[product.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_owner_can_delete_completed(self):
        product = self._make_product('completed', assignee=self.owner)
        self.client.login(username='owner', password='pass')
        resp = self.client.post(reverse('product_delete', args=[product.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_outsider_cannot_delete(self):
        product = self._make_product('cancelled', assignee=self.owner)
        self.client.login(username='outsider', password='pass')
        resp = self.client.post(reverse('product_delete', args=[product.pk]))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_cannot_delete_active_product(self):
        """进行中的品不能直接删除，必须先取消"""
        product = self._make_product('active', assignee=self.owner)
        self.client.login(username='admin', password='pass')
        resp = self.client.post(reverse('product_delete', args=[product.pk]))
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_delete_cascades_stages_and_tasks(self):
        """删除品会级联删除其阶段和任务"""
        product = self._make_product('draft')
        stage_ids = list(product.stages.values_list('id', flat=True))
        self.assertTrue(stage_ids)

        self.client.login(username='admin', password='pass')
        self.client.post(reverse('product_delete', args=[product.pk]))

        self.assertEqual(ProductStage.objects.filter(id__in=stage_ids).count(), 0)


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

    @patch('reminders.upward_notify.send_wechat_message', return_value=True)
    def test_mark_completed_triggers_notify(self, mock_send):
        self.task.mark_completed()
        mock_send.assert_called_once()

    @patch('reminders.upward_notify.send_wechat_message', return_value=True)
    def test_mark_overdue_triggers_notify(self, mock_send):
        self.task.mark_overdue()
        mock_send.assert_called_once()

    @patch('reminders.upward_notify.send_wechat_message', return_value=True)
    def test_stage_complete_triggers_notify(self, mock_send):
        # 阶段下唯一任务先标记完成，再触发阶段完成
        self.task.mark_completed()
        mock_send.reset_mock()
        self.stage.complete()
        mock_send.assert_called_once()

    @patch('reminders.upward_notify.send_wechat_message', return_value=True)
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
