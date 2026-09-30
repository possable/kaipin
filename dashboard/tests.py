from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from products.models import Product


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
            [p['name'] for p in response.context['completed_products']],
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
        self.assertEqual(response.context['completed_products'], [])
