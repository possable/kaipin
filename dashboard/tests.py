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
        for status in ('all', 'active', 'overdue', 'completed', 'cancelled', 'draft'):
            with self.subTest(status=status):
                response = self.client.get(reverse('kanban'), {'status': status})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['page_obj'].paginator.per_page, 20)
