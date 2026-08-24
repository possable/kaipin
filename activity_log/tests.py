from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from accounts.models import Department
from .models import ActivityLog
from .utils import log_action


class ActivityListFilterTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='策划部')
        self.alice = User.objects.create_user(username='alice', password='pass')
        self.alice.profile.department = self.dept
        self.alice.profile.save()
        self.bob = User.objects.create_user(username='bob', password='pass')
        self.bob.profile.department = self.dept
        self.bob.profile.save()

        log_action(self.alice, '创建新品', 'product', 1, '包装设计品', '草稿')
        log_action(self.alice, '标记任务完成', 'task', 1, '包装设计品 / 内部评审', '状态: 未开始 → 已完成')
        log_action(self.bob, '创建新品', 'product', 2, '营销方案品', '草稿')

        self.client.login(username='alice', password='pass')

    def test_no_filter_shows_all(self):
        resp = self.client.get(reverse('activity_list'))
        self.assertEqual(len(resp.context['logs']), 3)

    def test_filter_by_user(self):
        resp = self.client.get(reverse('activity_list'), {'user': self.bob.pk})
        logs = resp.context['logs']
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].user, self.bob)

    def test_filter_by_action(self):
        resp = self.client.get(reverse('activity_list'), {'action': '创建新品'})
        logs = resp.context['logs']
        self.assertEqual(len(logs), 2)
        self.assertTrue(all(l.action == '创建新品' for l in logs))

    def test_search_by_target_name(self):
        resp = self.client.get(reverse('activity_list'), {'q': '营销方案'})
        logs = resp.context['logs']
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].target_name, '营销方案品')

    def test_search_by_detail(self):
        resp = self.client.get(reverse('activity_list'), {'q': '已完成'})
        logs = resp.context['logs']
        self.assertEqual(len(logs), 1)
        self.assertIn('已完成', logs[0].detail)

    def test_combined_filters(self):
        resp = self.client.get(reverse('activity_list'), {'user': self.alice.pk, 'action': '创建新品', 'q': '包装'})
        logs = resp.context['logs']
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].target_name, '包装设计品')

    def test_no_match_returns_empty(self):
        resp = self.client.get(reverse('activity_list'), {'q': '不存在的关键词'})
        self.assertEqual(len(resp.context['logs']), 0)
