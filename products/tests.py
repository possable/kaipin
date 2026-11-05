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
from accounts.models import Department
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
        mock_send.assert_called_once()

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

    def _post_create(self, name='新品'):
        return self.client.post(reverse('product_create'), {
            'name': name,
            'assignee': self.owner.pk,
            'started_at': timezone.localdate().isoformat(),
            'expected_end_date': (timezone.localdate() + timedelta(days=60)).isoformat(),
            'brand': '跨境自营',
            'category': '新品开发',
        })

    def test_new_product_is_published_immediately(self):
        response = self._post_create()
        product = Product.objects.get(name='新品')

        self.assertEqual(response.status_code, 302)
        # 创建完直接进入「设置项目流程」窗口
        self.assertIn('?setup=1', response.url)
        self.assertEqual(product.status, 'active')
        first_stage = product.stages.order_by('order').first()
        self.assertEqual(first_stage.status, 'in_progress')
        self.assertIsNotNone(first_stage.started_at)
        self.assertEqual(product.current_stage_order, 1)

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
        return self.client.post(reverse('product_create'), {
            'name': name,
            'assignee': self.owner.pk,
            'started_at': timezone.localdate().isoformat(),
            'expected_end_date': (
                timezone.localdate() + timedelta(days=60)
            ).isoformat(),
            'brand': '跨境自营',
            'category': '新品开发',
        })

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
        self.assertIn('你负责的项目「立项通知品」已立项。', owner_text)
        self.assertIn(
            f'开始时间：{timezone.localdate().isoformat()}　'
            f'预计结束：{(timezone.localdate() + timedelta(days=60)).isoformat()}',
            owner_text,
        )
        self.assertIn('共 2 个阶段，首个阶段「立项」已开始。', owner_text)
        self.assertIn(f'点击查看：{settings.SITE_URL}', owner_text)

        stage_text = sent['wx_stage_owner']
        self.assertIn('李四你好，我是项目管理智能机器人。', stage_text)
        self.assertIn('你被安排为「立项通知品」项目「包装设计」阶段的负责人。', stage_text)
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
        self.assertIn('「包装设计」「打样」阶段的负责人', stage_text)

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

    回归点：后端 can_create_product() 和左侧栏都认产品部，但看板右上角
    的「新建项目」按钮只判断了 is_admin，产品部普通成员看得到侧边栏、
    却看不到看板上的按钮。
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

    def test_product_department_member_sees_the_kanban_button(self):
        self.client.login(username='product-member', password='pass')

        response = self.client.get(reverse('kanban'))

        self.assertContains(response, '新建项目')

    def test_product_department_member_can_open_the_create_page(self):
        self.client.login(username='product-member', password='pass')

        response = self.client.get(reverse('product_create'))

        self.assertEqual(response.status_code, 200)

    def test_other_department_member_sees_no_entry_and_is_denied(self):
        self.client.login(username='create-outsider', password='pass')

        self.assertNotContains(self.client.get(reverse('kanban')), '新建项目')
        response = self.client.get(reverse('product_create'))
        self.assertEqual(response.status_code, 403)


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
            f'assignee_{self.stage2.pk}': '',
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
