from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department
from products.models import Product, ProductStage, Task
from templates_app.models import StageTemplate


class KanbanPaginationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='pagination-admin',
            password='test-password',
        )
        self.user.profile.role = 'admin'
        self.user.profile.save()
        self.client.force_login(self.user)

    def test_project_pages_show_twenty_items_by_default(self):
        for index in range(21):
            Product.objects.create(
                name=f'Project {index + 1}',
                creator=self.user,
            )

        first_page = self.client.get(reverse('kanban'))

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context['page_obj'].paginator.per_page, 20)
        self.assertEqual(first_page.context['page_obj'].paginator.count, 21)
        self.assertEqual(len(first_page.context['products']), 20)

        second_page = self.client.get(reverse('kanban'), {'page': 2})

        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(len(second_page.context['products']), 1)

    def test_all_project_status_views_use_the_same_page_size(self):
        # 没有 'completed' —— 已完成的项目已经从看板搬到「上架归档」页了
        for status in ('all', 'active', 'overdue', 'cancelled', 'draft'):
            with self.subTest(status=status):
                response = self.client.get(reverse('kanban'), {'status': status})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['page_obj'].paginator.per_page, 20)


class ArchiveViewTests(TestCase):
    """上架归档页：已完成的项目只出现在这里，不再出现在进度看板。"""

    def setUp(self):
        self.admin = User.objects.create_user(
            username='archive-admin',
            password='test-password',
        )
        self.admin.profile.role = 'admin'
        self.admin.profile.save()

        self.outsider = User.objects.create_user(
            username='archive-outsider',
            password='test-password',
        )

        self.completed = Product.objects.create(
            name='已完成的品', creator=self.admin, status='completed',
        )
        self.running = Product.objects.create(
            name='进行中的品', creator=self.admin, status='active',
        )

    def test_archive_lists_only_completed_projects(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse('archive'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [p['name'] for p in response.context['products']],
            ['已完成的品'],
        )

    def test_kanban_no_longer_lists_completed_projects(self):
        self.client.force_login(self.admin)

        body = self.client.get(reverse('kanban')).content.decode()

        self.assertNotIn('已完成的品', body)
        self.assertIn('进行中的品', body)

    def test_legacy_completed_status_link_falls_back_to_all(self):
        """旧书签 ?status=completed 不能让看板报错或给一张空表。"""
        self.client.force_login(self.admin)

        response = self.client.get(reverse('kanban'), {'status': 'completed'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['status_filter'], 'all')
        self.assertIn('进行中的品', response.content.decode())

    def test_outsider_does_not_see_unrelated_completed_projects(self):
        self.client.force_login(self.outsider)

        response = self.client.get(reverse('archive'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['products']), [])

    def test_completion_date_filter_covers_the_whole_day(self):
        """完成时间按自然日筛，当天要算在内。

        回归点：这里不能用 __date 查询。这台 MySQL 没加载时区表，Django 会把
        __date 翻译成 DATE(CONVERT_TZ(col,'UTC','Asia/Shanghai'))，而 CONVERT_TZ
        在时区表缺失时返回 NULL —— 不报错，但一条都匹配不到。
        """
        self.completed.actual_end_date = timezone.now()
        self.completed.save()
        completed_on = timezone.localtime(self.completed.actual_end_date).date()
        self.client.force_login(self.admin)

        same_day = self.client.get(reverse('archive'), {
            'date_from': completed_on.isoformat(),
            'date_to': completed_on.isoformat(),
        })
        self.assertEqual([p['name'] for p in same_day.context['products']], ['已完成的品'])

        day_after = (completed_on + timedelta(days=1)).isoformat()
        too_late = self.client.get(reverse('archive'), {'date_from': day_after})
        self.assertEqual(list(too_late.context['products']), [])

    def test_invalid_filter_values_do_not_break_the_page(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse('archive'), {
            'date_from': 'not-a-date',
            'date_to': '2026-13-45',
            'assignee': 'not-an-int',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [p['name'] for p in response.context['products']],
            ['已完成的品'],
        )

    def test_duration_days_counts_from_start_to_finish(self):
        start = timezone.now() - timedelta(days=30)
        self.completed.started_at = start
        self.completed.actual_end_date = start + timedelta(days=12)
        self.completed.save()
        self.client.force_login(self.admin)

        response = self.client.get(reverse('archive'))

        self.assertEqual(response.context['products'][0]['duration_days'], 12)


class KanbanStageKpiTests(TestCase):
    """KPI 卡片数字 ↔ 点击卡片后的项目列表，必须一一对上。

    回归点：包装设计卡片显示 8 个「进行中」，点进去却是空列表 ——
    卡片把「已超期」的阶段也算进去了，而点击后的筛选只认 in_progress。
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username='kpi-admin', password='test-password',
        )
        self.admin.profile.role = 'admin'
        self.admin.profile.save()
        self.client.force_login(self.admin)

        self.department = Department.objects.create(name='设计部')
        StageTemplate.objects.create(
            name='包装设计', order=1, department=self.department,
            # 最后一个模板不出卡片，建一条让 kpi_stage_templates 的切片逻辑生效
        )
        StageTemplate.objects.create(name='上架归档', order=2, department=self.department)

    def _make_project(self, name, stage_status, task_expected_offset_days,
                      task_status='in_progress'):
        """建一个带「包装设计」阶段的项目。

        阶段状态由子任务聚合，所以只改 stage_status 不生效；
        task_expected_offset_days 为负即任务超期。
        """
        product = Product.objects.create(name=name, creator=self.admin)
        stage = ProductStage.objects.create(
            product=product, name='包装设计', order=1,
            department=self.department, status=stage_status,
            started_at=timezone.now() - timedelta(days=10),
        )
        Task.objects.create(
            product_stage=stage, name='设计稿', order=1, status=task_status,
            started_at=timezone.now() - timedelta(days=5),
            expected_end_date=(
                timezone.localdate() + timedelta(days=task_expected_offset_days)
            ),
        )
        return product

    def test_overdue_stage_counts_and_appears_after_click(self):
        overdue_a = self._make_project('超期品A', 'in_progress', -1)
        overdue_b = self._make_project('超期品B', 'in_progress', -1)
        running = self._make_project('进行中品', 'in_progress', 5)

        response = self.client.get(reverse('kanban'))
        self.assertEqual(response.context['stage_counts']['包装设计'], 3)

        clicked = self.client.get(reverse('kanban'), {'stage': '包装设计'})

        self.assertEqual(
            {p['name'] for p in clicked.context['products']},
            {overdue_a.name, overdue_b.name, running.name},
        )
        self.assertEqual(
            len(clicked.context['products']),
            response.context['stage_counts']['包装设计'],
        )

    def test_completed_stage_is_not_counted(self):
        self._make_project(
            '已过包装设计', 'completed', 5, task_status='completed',
        )

        response = self.client.get(reverse('kanban'))

        self.assertEqual(response.context['stage_counts']['包装设计'], 0)

    def test_counts_stay_global_when_filters_are_active(self):
        """卡片是全局口径：筛选栏只看超期时，包装设计仍应显示全部数量，
        点进去（清掉筛选）能看到同样多条。"""
        self._make_project('超期品', 'in_progress', -1)
        self._make_project('进行中品', 'in_progress', 5)

        response = self.client.get(reverse('kanban'), {'status': 'overdue'})

        self.assertEqual(response.context['stage_counts']['包装设计'], 2)


class KanbanOverdueContactTests(TestCase):
    """点开「已超期」时显示的联系人：任务没负责人就显示阶段负责人，不显示「未指定」。"""

    def setUp(self):
        self.admin = User.objects.create_user(
            username='overdue-admin', password='test-password',
        )
        self.admin.profile.role = 'admin'
        self.admin.profile.save()
        self.client.force_login(self.admin)
        self.department = Department.objects.create(name='超期联系部门')

    def _make_overdue_product(self, stage_assignee=None, task_assignee=None):
        product = Product.objects.create(name='超期联系人品', creator=self.admin)
        stage = ProductStage.objects.create(
            product=product, name='包装设计', order=1, department=self.department,
            status='in_progress', assignee=stage_assignee,
        )
        Task.objects.create(
            product_stage=stage, name='设计稿', order=1, status='in_progress',
            assignee=task_assignee,
            started_at=timezone.now() - timedelta(days=5),
            expected_end_date=timezone.localdate() - timedelta(days=1),
        )
        return product

    def test_falls_back_to_stage_assignee(self):
        stage_owner = User.objects.create(
            username='stage-owner', first_name='阶段负责人甲',
        )
        self._make_overdue_product(stage_assignee=stage_owner)

        response = self.client.get(reverse('kanban'))
        item = next(
            p for p in response.context['products'] if p['name'] == '超期联系人品'
        )

        self.assertEqual(item['overdue_tasks'][0]['assignee'], '阶段负责人甲')
        self.assertNotIn('未指定', response.content.decode())

    def test_task_assignee_takes_precedence(self):
        stage_owner = User.objects.create(
            username='stage-owner2', first_name='阶段负责人乙',
        )
        task_owner = User.objects.create(
            username='task-owner', first_name='任务负责人丙',
        )
        self._make_overdue_product(
            stage_assignee=stage_owner, task_assignee=task_owner,
        )

        response = self.client.get(reverse('kanban'))
        item = next(
            p for p in response.context['products'] if p['name'] == '超期联系人品'
        )

        self.assertEqual(item['overdue_tasks'][0]['assignee'], '任务负责人丙')

    def test_no_assignee_at_all_stays_blank(self):
        self._make_overdue_product()

        response = self.client.get(reverse('kanban'))
        item = next(
            p for p in response.context['products'] if p['name'] == '超期联系人品'
        )

        self.assertEqual(item['overdue_tasks'][0]['assignee'], '')
        self.assertNotIn('未指定', response.content.decode())
