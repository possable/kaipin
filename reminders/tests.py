from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from datetime import date, timedelta
from unittest.mock import patch, MagicMock
from accounts.models import Department
from templates_app.models import StageTemplate, TaskTemplate
from products.models import Product, ProductStage, Task
from .models import ReminderLog, UpwardNotifyLog
from .scheduler import scan_and_remind
from .upward_notify import notify_upward


def send_message_url(entity_type, entity_id):
    return reverse('send_wechat_message_view', args=[entity_type, entity_id])


class ReminderTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='策划部')
        self.user = User.objects.create_user(username='tester', password='pass')
        self.user.profile.department = self.dept
        self.user.profile.wechat_userid = 'test_wechat_id'
        self.user.profile.save()

        st = StageTemplate.objects.create(name='阶段1', order=1, department=self.dept)
        TaskTemplate.objects.create(stage_template=st, name='任务A', order=1)

        self.product = Product.objects.create(name='测试品', creator=self.user)
        self.product.create_stages_from_templates()
        self.task = self.product.stages.first().tasks.first()
        self.task.assignee = self.user
        self.task.deadline = date.today() - timedelta(days=1)  # 昨天 → 超期
        self.task.save()

    @patch('reminders.wechat.send_wechat_message')
    def test_overdue_reminder_sent(self, mock_send):
        mock_send.return_value = True
        scan_and_remind()

        # 检查提醒记录
        self.assertTrue(ReminderLog.objects.filter(
            task=self.task, reminder_type='overdue'
        ).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'overdue')

    @patch('reminders.wechat.send_wechat_message')
    def test_reminder_not_sent_twice_same_day(self, mock_send):
        mock_send.return_value = True
        scan_and_remind()
        scan_and_remind()  # 第二次不应发送

        # 按收件人过滤而不是数总调用数：聚合通知路径共用同一个 patch 点，
        # 数总数会在别的路径发消息时假绿。
        to_tester = [c for c in mock_send.call_args_list if c[0][0] == 'test_wechat_id']
        self.assertEqual(len(to_tester), 1)
        self.assertEqual(ReminderLog.objects.filter(task=self.task).count(), 1)

    @patch('reminders.wechat.send_wechat_message')
    def test_upcoming_reminder(self, mock_send):
        mock_send.return_value = True
        self.task.deadline = date.today() + timedelta(days=2)  # 2天后 → 临近
        self.task.status = 'pending'
        self.task.save()

        scan_and_remind()
        self.assertTrue(ReminderLog.objects.filter(
            task=self.task, reminder_type='upcoming'
        ).exists())

    @patch('reminders.wechat.send_wechat_message')
    def test_no_reminder_for_future_task(self, mock_send):
        mock_send.return_value = True
        self.task.deadline = date.today() + timedelta(days=10)
        self.task.status = 'pending'
        self.task.save()

        scan_and_remind()
        mock_send.assert_not_called()


class SendMessageViewTest(TestCase):
    """发消息权限与编辑权限同步：谁能编辑某个人负责的品/阶段/任务，谁就能给这个人发消息"""

    def setUp(self):
        self.dept_a = Department.objects.create(name='策划部')
        self.dept_b = Department.objects.create(name='设计部')

        self.admin = User.objects.create_user(username='admin', password='pass')
        self.admin.profile.department = self.dept_a
        self.admin.profile.role = 'admin'
        self.admin.profile.save()

        # 品负责人，也绑定了企业微信
        self.product_owner = User.objects.create_user(username='owner', password='pass')
        self.product_owner.profile.department = self.dept_a
        self.product_owner.profile.wechat_userid = 'wx_owner'
        self.product_owner.profile.save()

        # 阶段（设计部）负责人
        self.stage_assignee = User.objects.create_user(username='stage_lead', password='pass')
        self.stage_assignee.profile.department = self.dept_b
        self.stage_assignee.profile.wechat_userid = 'wx_stage_lead'
        self.stage_assignee.profile.save()

        # 设计部普通成员（阶段进行中时可编辑该阶段）
        self.dept_b_member = User.objects.create_user(username='designer', password='pass')
        self.dept_b_member.profile.department = self.dept_b
        self.dept_b_member.profile.save()

        # 与本品无关的部门成员
        self.outsider = User.objects.create_user(username='outsider', password='pass')
        self.outsider.profile.department = self.dept_a
        self.outsider.profile.save()

        # 任务负责人（无企业微信绑定，用于测试未绑定场景）
        self.task_assignee_no_wechat = User.objects.create_user(username='task_guy', password='pass')
        self.task_assignee_no_wechat.profile.department = self.dept_b
        self.task_assignee_no_wechat.profile.save()

        st1 = StageTemplate.objects.create(name='阶段1', order=1, department=self.dept_a)
        TaskTemplate.objects.create(stage_template=st1, name='任务A', order=1)
        st2 = StageTemplate.objects.create(name='阶段2', order=2, department=self.dept_b)
        TaskTemplate.objects.create(stage_template=st2, name='任务B', order=1)

        self.product = Product.objects.create(
            name='测试品', creator=self.admin, assignee=self.product_owner,
        )
        self.product.create_stages_from_templates()
        self.stage1 = self.product.stages.get(order=1)
        self.stage2 = self.product.stages.get(order=2)
        self.stage2.assignee = self.stage_assignee
        self.stage2.status = 'in_progress'
        self.stage2.save()

        self.task_in_stage2 = self.stage2.tasks.first()
        self.task_in_stage2.assignee = self.task_assignee_no_wechat
        self.task_in_stage2.save()

    @patch('reminders.wechat.send_wechat_message')
    def test_admin_can_message_product_owner(self, mock_send):
        mock_send.return_value = True
        self.client.login(username='admin', password='pass')
        resp = self.client.post(send_message_url('product', self.product.pk), {'content': '进度如何？'})
        self.assertEqual(resp.status_code, 200)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][0], 'wx_owner')

    @patch('reminders.wechat.send_wechat_message')
    def test_dept_member_can_message_stage_assignee_when_in_progress(self, mock_send):
        """阶段进行中时，所属部门成员可以给阶段负责人发消息（权限与编辑该阶段一致）"""
        mock_send.return_value = True
        self.client.login(username='designer', password='pass')
        resp = self.client.post(send_message_url('stage', self.stage2.pk), {'content': '设计稿进展？'})
        self.assertEqual(resp.status_code, 200)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][0], 'wx_stage_lead')

    @patch('reminders.wechat.send_wechat_message')
    def test_outsider_cannot_message_stage_assignee(self, mock_send):
        """与该阶段无关的用户不能发消息"""
        self.client.login(username='outsider', password='pass')
        resp = self.client.post(send_message_url('stage', self.stage2.pk), {'content': '你好'})
        self.assertEqual(resp.status_code, 403)
        mock_send.assert_not_called()

    @patch('reminders.wechat.send_wechat_message')
    def test_dept_member_can_message_task_assignee_when_stage_in_progress(self, mock_send):
        """阶段进行中时，所属部门成员对该阶段下任务有编辑权限，因此也能给任务负责人发消息"""
        mock_send.return_value = False  # 该任务负责人没绑定企业微信，不会走到这一步
        self.client.login(username='designer', password='pass')
        resp = self.client.post(send_message_url('task', self.task_in_stage2.pk), {'content': '你好'})
        self.assertEqual(resp.status_code, 400)  # 权限通过，但未绑定企业微信
        self.assertIn('企业微信', resp.json()['error'])

    def test_cannot_send_to_entity_without_assignee(self):
        self.client.login(username='admin', password='pass')
        resp = self.client.post(send_message_url('stage', self.stage1.pk), {'content': '你好'})
        self.assertEqual(resp.status_code, 400)

    @patch('reminders.wechat.send_wechat_message')
    def test_empty_content_rejected(self, mock_send):
        self.client.login(username='admin', password='pass')
        resp = self.client.post(send_message_url('product', self.product.pk), {'content': '  '})
        self.assertEqual(resp.status_code, 400)
        mock_send.assert_not_called()

    def test_invalid_entity_type_rejected(self):
        self.client.login(username='admin', password='pass')
        resp = self.client.post(send_message_url('user', 1), {'content': '你好'})
        self.assertEqual(resp.status_code, 400)

    def test_requires_login(self):
        resp = self.client.post(send_message_url('product', self.product.pk), {'content': '你好'})
        self.assertEqual(resp.status_code, 302)  # 重定向到登录页


class StageProductOverdueScanTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='扫描测试部门')
        self.stage_owner = User.objects.create(username='scan_so', first_name='扫描阶段负责人')
        self.product_owner = User.objects.create(username='scan_po', first_name='扫描品负责人')
        self.product_owner.profile.wechat_userid = 'wx_scan_po'
        self.product_owner.profile.save()
        self.task_assignee = User.objects.create(username='scan_ta', first_name='扫描任务人')

        self.product = Product.objects.create(
            name='扫描测试品', creator=self.task_assignee, assignee=self.product_owner,
        )
        self.stage = ProductStage.objects.create(
            product=self.product, name='扫描测试阶段', department=self.dept,
            assignee=self.stage_owner, order=1,
        )
        self.task = Task.objects.create(
            product_stage=self.stage, name='扫描测试任务', assignee=self.task_assignee,
            order=1, status='overdue', deadline=date.today() - timedelta(days=2),
        )

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_scan_notifies_product_owner_for_overdue_stage(self, mock_send):
        from reminders.scheduler import scan_and_remind
        scan_and_remind()
        calls = [c for c in mock_send.call_args_list if c[0][0] == 'wx_scan_po']
        self.assertEqual(len(calls), 1)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_scan_does_not_duplicate_same_day(self, mock_send):
        from reminders.scheduler import scan_and_remind
        scan_and_remind()
        scan_and_remind()
        calls = [c for c in mock_send.call_args_list if c[0][0] == 'wx_scan_po']
        self.assertEqual(len(calls), 1)

    def test_scan_creates_upward_notify_log(self):
        from reminders.scheduler import scan_and_remind
        with patch('reminders.wechat.send_wechat_message', return_value=True):
            scan_and_remind()
        self.assertTrue(
            UpwardNotifyLog.objects.filter(
                content_type_label='stage', object_id=self.stage.pk,
                event_type='overdue', sent_date=date.today(),
            ).exists()
        )


class StageOverdueAggregationTest(TestCase):
    """品总负责人同时管多个品、每个品多个阶段超期时，只应收到一条消息。"""

    def setUp(self):
        self.dept = Department.objects.create(name='阶段聚合部门')
        self.creator = User.objects.create(username='sagg_creator', first_name='阶段聚合创建人')

        self.owner = User.objects.create(username='sagg_owner', first_name='阶段聚合负责人')
        self.owner.profile.wechat_userid = 'wx_sagg_owner'
        self.owner.profile.save()

        # 三个品都归同一个人负责，每个品下两个超期阶段
        for p_idx in (1, 2, 3):
            product = Product.objects.create(
                name=f'阶段聚合品{p_idx}', creator=self.creator, assignee=self.owner,
            )
            for s_idx in (1, 2):
                stage = ProductStage.objects.create(
                    product=product, name=f'阶段{p_idx}-{s_idx}',
                    department=self.dept, order=s_idx,
                )
                Task.objects.create(
                    product_stage=stage, name=f'任务{p_idx}-{s_idx}',
                    order=1, status='overdue',
                    deadline=date.today() - timedelta(days=1),
                )

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_many_overdue_stages_send_one_message(self, mock_send):
        scan_and_remind()

        calls = [c for c in mock_send.call_args_list if c[0][0] == 'wx_sagg_owner']
        self.assertEqual(len(calls), 1, '6 个超期阶段应收敛成 1 条消息')

        content = calls[0][0][1]
        self.assertIn('你负责的 3 个品存在超期阶段', content)
        for p_idx in (1, 2, 3):
            self.assertIn(f'阶段聚合品{p_idx}', content)
            # 同一个品下的两个阶段要合并到同一行
            self.assertIn(f'阶段{p_idx}-1、阶段{p_idx}-2', content)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_stage_log_written_per_stage(self, mock_send):
        scan_and_remind()
        self.assertEqual(
            UpwardNotifyLog.objects.filter(
                content_type_label='stage', event_type='overdue', sent_date=date.today(),
            ).count(),
            6,
        )

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_ownerless_product_skipped(self, mock_send):
        for product in Product.objects.filter(name__startswith='阶段聚合品'):
            product.assignee = None
            product.save()
        scan_and_remind()
        self.assertEqual(
            [c for c in mock_send.call_args_list if c[0][0] == 'wx_sagg_owner'], [],
        )


class ProductOverdueAggregationTest(TestCase):
    """多个品同时超期时，每个管理员每天只应收到 **一条** 聚合消息。"""

    def setUp(self):
        self.dept = Department.objects.create(name='聚合测试部门')
        self.creator = User.objects.create(username='agg_creator', first_name='聚合创建人')

        self.admin1 = User.objects.create(username='agg_admin1', first_name='管理员甲')
        self.admin1.profile.role = 'admin'
        self.admin1.profile.wechat_userid = 'wx_admin1'
        self.admin1.profile.save()

        self.admin2 = User.objects.create(username='agg_admin2', first_name='管理员乙')
        self.admin2.profile.role = 'admin'
        self.admin2.profile.wechat_userid = 'wx_admin2'
        self.admin2.profile.save()

        # 管理员丙没绑企微 —— 应被跳过，且不影响其他人
        self.admin3 = User.objects.create(username='agg_admin3', first_name='管理员丙')
        self.admin3.profile.role = 'admin'
        self.admin3.profile.save()

        self.products = []
        for idx in (1, 2):
            product = Product.objects.create(
                name=f'聚合超期品{idx}', creator=self.creator,
            )
            stage = ProductStage.objects.create(
                product=product, name=f'聚合阶段{idx}', department=self.dept, order=1,
            )
            Task.objects.create(
                product_stage=stage, name=f'聚合任务{idx}',
                order=1, status='overdue', deadline=date.today() - timedelta(days=1),
            )
            self.products.append(product)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_two_overdue_products_send_one_message_per_admin(self, mock_send):
        scan_and_remind()

        for userid in ('wx_admin1', 'wx_admin2'):
            calls = [c for c in mock_send.call_args_list if c[0][0] == userid]
            self.assertEqual(len(calls), 1, f'{userid} 应只收到 1 条聚合消息')
            # 一条消息里要同时列出两个品
            content = calls[0][0][1]
            self.assertIn('聚合超期品1', content)
            self.assertIn('聚合超期品2', content)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_unbound_admin_skipped(self, mock_send):
        scan_and_remind()
        called_userids = [c[0][0] for c in mock_send.call_args_list]
        self.assertEqual(set(called_userids), {'wx_admin1', 'wx_admin2'})

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_non_admin_product_owner_not_aggregated(self, mock_send):
        """品负责人不是管理员时，只收阶段超期那条（单品），不收品级聚合。"""
        owner = User.objects.create(username='agg_owner', first_name='聚合品负责人')
        owner.profile.wechat_userid = 'wx_owner'
        owner.profile.save()
        self.products[0].assignee = owner
        self.products[0].save()

        scan_and_remind()

        stage_calls = [c for c in mock_send.call_args_list if c[0][0] == 'wx_owner']
        self.assertEqual(len(stage_calls), 1)
        self.assertNotIn('聚合超期品2', stage_calls[0][0][1])

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_dedup_log_written_per_product(self, mock_send):
        """聚合发送了，但 UpwardNotifyLog 仍逐品记录，保证明天不会重复计入。"""
        scan_and_remind()
        for product in self.products:
            self.assertTrue(
                UpwardNotifyLog.objects.filter(
                    content_type_label='product', object_id=product.pk,
                    event_type='overdue', sent_date=date.today(),
                ).exists(),
                f'品 {product.pk} 应有超期通知日志',
            )

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_second_scan_same_day_sends_nothing_new(self, mock_send):
        scan_and_remind()
        first = len(mock_send.call_args_list)
        scan_and_remind()
        self.assertEqual(len(mock_send.call_args_list), first)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_only_newly_overdue_product_listed(self, mock_send):
        """昨天已通知过品1，今天只有品2超期 → 消息里只有品2。"""
        UpwardNotifyLog.objects.create(
            content_type_label='product', object_id=self.products[0].pk,
            event_type='overdue', sent_date=date.today(),
        )
        scan_and_remind()

        calls = [c for c in mock_send.call_args_list if c[0][0] == 'wx_admin1']
        self.assertEqual(len(calls), 1)
        content = calls[0][0][1]
        self.assertIn('聚合超期品2', content)
        self.assertNotIn('聚合超期品1', content)


class UpwardNotifyLogTest(TestCase):
    def test_unique_constraint_prevents_duplicate_same_day(self):
        from datetime import date
        UpwardNotifyLog.objects.create(
            content_type_label='stage', object_id=1,
            event_type='overdue', sent_date=date(2026, 7, 21),
        )
        with self.assertRaises(Exception):
            UpwardNotifyLog.objects.create(
                content_type_label='stage', object_id=1,
                event_type='overdue', sent_date=date(2026, 7, 21),
            )


class NotifyUpwardTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='测试部门')
        self.stage_owner = User.objects.create(username='stage_owner', first_name='阶段负责人')
        self.stage_owner.profile.department = self.dept
        self.stage_owner.profile.wechat_userid = 'wx_stage_owner'
        self.stage_owner.profile.save()

        self.product_owner = User.objects.create(username='product_owner', first_name='品负责人')
        self.product_owner.profile.wechat_userid = 'wx_product_owner'
        self.product_owner.profile.save()

        self.task_assignee = User.objects.create(username='task_worker', first_name='任务执行人')

        self.product = Product.objects.create(
            name='测试品', creator=self.task_assignee, assignee=self.product_owner,
        )
        self.stage = ProductStage.objects.create(
            product=self.product, name='测试阶段', department=self.dept,
            assignee=self.stage_owner, order=1,
        )
        self.task = Task.objects.create(
            product_stage=self.stage, name='测试任务', assignee=self.task_assignee, order=1,
        )

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_task_completed_notifies_stage_owner(self, mock_send):
        notify_upward(self.task, 'completed', actor=self.task_assignee)
        mock_send.assert_called_once()
        called_userid, called_content = mock_send.call_args[0]
        self.assertEqual(called_userid, 'wx_stage_owner')
        self.assertIn('阶段负责人', called_content)
        self.assertIn('测试任务', called_content)

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_stage_completed_notifies_product_owner(self, mock_send):
        notify_upward(self.stage, 'completed', actor=self.stage_owner)
        mock_send.assert_called_once()
        called_userid, called_content = mock_send.call_args[0]
        self.assertEqual(called_userid, 'wx_product_owner')

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_skips_when_actor_is_upward_owner(self, mock_send):
        # 阶段负责人自己完成了任务，且阶段负责人和上级(品负责人)不是同一人时应正常通知；
        # 但若操作人恰好就是阶段负责人本人（= 上级），应跳过
        notify_upward(self.task, 'completed', actor=self.stage_owner)
        mock_send.assert_not_called()

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_skips_when_no_upward_owner(self, mock_send):
        self.stage.assignee = None
        self.stage.save()
        notify_upward(self.task, 'completed', actor=self.task_assignee)
        mock_send.assert_not_called()

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_skips_when_owner_has_no_wechat_userid(self, mock_send):
        self.stage_owner.profile.wechat_userid = ''
        self.stage_owner.profile.save()
        notify_upward(self.task, 'completed', actor=self.task_assignee)
        mock_send.assert_not_called()

    @patch('reminders.wechat.send_wechat_message', side_effect=Exception('网络错误'))
    def test_does_not_raise_when_send_fails(self, mock_send):
        try:
            notify_upward(self.task, 'completed', actor=self.task_assignee)
        except Exception:
            self.fail('notify_upward 不应向外抛出异常')

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_product_completed_notifies_all_admins(self, mock_send):
        admin1 = User.objects.create(username='admin1', first_name='管理员一')
        admin1.profile.wechat_userid = 'wx_admin1'
        # UserProfile.is_admin 是基于 role 字段的只读 property（role == 'admin'）
        admin1.profile.role = 'admin'
        admin1.profile.save()

        notify_upward(self.product, 'completed', actor=self.product_owner)
        mock_send.assert_called_once()
        called_userid, called_content = mock_send.call_args[0]
        self.assertEqual(called_userid, 'wx_admin1')

    def test_one_recipient_failure_does_not_block_others(self):
        """多个收件人场景下，其中一个发送失败（抛异常）不应影响其他收件人正常收到通知，
        也不应让 notify_upward 向外抛出异常。

        ⚠️ side_effect 必须接收 **kwargs：send_message_to_user 现在会带 corp= 调用，
        签名不匹配会抛 TypeError —— 那样**两个**收件人都「失败」，
        而本用例只断言两人都被调用过，就会在什么都没验证的情况下变绿。
        所以下面额外断言另一个收件人**真的发成功了**。
        """
        admin1 = User.objects.create(username='admin1', first_name='管理员一')
        admin1.profile.wechat_userid = 'wx_admin1'
        admin1.profile.role = 'admin'
        admin1.profile.save()

        admin2 = User.objects.create(username='admin2', first_name='管理员二')
        admin2.profile.wechat_userid = 'wx_admin2'
        admin2.profile.role = 'admin'
        admin2.profile.save()

        delivered = []

        def side_effect(userid, content, **kwargs):
            if userid == 'wx_admin1':
                raise Exception('网络错误')
            delivered.append(userid)
            return True

        with patch('reminders.wechat.send_wechat_message', side_effect=side_effect) as mock_send:
            try:
                notify_upward(self.product, 'completed', actor=self.product_owner)
            except Exception:
                self.fail('notify_upward 不应向外抛出异常')

            called_userids = [call.args[0] for call in mock_send.call_args_list]
            self.assertIn('wx_admin1', called_userids)
            self.assertIn('wx_admin2', called_userids)
            # 关键断言：失败的那个之外，另一个必须真的发出去了
            self.assertEqual(delivered, ['wx_admin2'])
