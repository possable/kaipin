"""多企业登录链路的测试。

accounts 原本零测试覆盖，而这一层正是「三个企业微信共用一套数据」改造中
唯一有实质逻辑变更的地方 —— 登录入口、corp 解析、token 分企业缓存。
"""
from unittest.mock import patch
from urllib.parse import unquote

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Department, WeComCorp, WeComDept, WeComIdentity
from .utils import get_or_create_user_from_wechat, resolve_shared_department
from .wecom import default_corp, get_corp, resolve_corp


def make_corp(code, **kwargs):
    defaults = {
        'name': f'{code} 公司',
        'corp_id': f'corpid_{code}',
        'agent_id': f'100{abs(hash(code)) % 1000}',
        'app_secret': f'secret_{code}',
    }
    defaults.update(kwargs)
    return WeComCorp.objects.create(code=code, **defaults)


class WeComCorpModelTest(TestCase):
    def test_default_is_unique(self):
        """把一条设成默认时，原来那条要自动取消，否则 is_default 就没有确定含义。"""
        first = make_corp('a', is_default=True)
        second = make_corp('b', is_default=True)

        first.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)
        self.assertEqual(WeComCorp.objects.filter(is_default=True).count(), 1)

    def test_code_is_unique(self):
        make_corp('a')
        with self.assertRaises(Exception):
            make_corp('a')


class CorpResolverTest(TestCase):
    def setUp(self):
        # 迁移 0009 会播种一条 default 企业（测试库也会跑迁移），
        # 这里清空才能测到「表为空」的分支。
        WeComCorp.objects.all().delete()

    def test_empty_table_falls_back_to_settings(self):
        """表为空时必须回退到 settings —— 迁移还没回填时登录不能挂。"""
        from django.conf import settings
        self.assertEqual(WeComCorp.objects.count(), 0)

        corp = default_corp()
        self.assertEqual(corp.corp_id, settings.WECHAT_CORP_ID)
        self.assertEqual(corp.app_secret, settings.WECHAT_APP_SECRET)
        # 回退产出的是未保存实例，不要落库
        self.assertIsNone(corp.pk)

    def test_prefers_is_default(self):
        make_corp('a')
        make_corp('b', is_default=True)
        self.assertEqual(default_corp().code, 'b')

    def test_falls_back_to_any_active_when_no_default(self):
        make_corp('a')
        make_corp('b')
        self.assertIn(default_corp().code, {'a', 'b'})

    def test_get_corp_by_code(self):
        make_corp('b-corp')
        self.assertEqual(get_corp('b-corp').code, 'b-corp')

    def test_unknown_code_falls_back_to_default(self):
        """入口 URL 是管理员在企微后台手填的，写错不该 500。"""
        WeComCorp.objects.all().delete()
        make_corp('a', is_default=True)
        self.assertEqual(get_corp('typo').code, 'a')
        self.assertEqual(get_corp('').code, 'a')
        self.assertEqual(get_corp(None).code, 'a')

    def test_inactive_corp_not_selected(self):
        make_corp('a', is_default=True)
        make_corp('b', is_active=False)
        self.assertEqual(get_corp('b').code, 'a')

    def test_resolve_corp_url_code_wins_over_session(self):
        """同一台手机三个企业共 cookie jar，URL 上的企业必须压过 session 里的。"""
        make_corp('a')
        make_corp('b')

        class FakeRequest:
            session = {'wecom_corp_code': 'a'}

        self.assertEqual(resolve_corp(FakeRequest, 'b').code, 'b')
        self.assertEqual(resolve_corp(FakeRequest).code, 'a')


class CorpLoginTest(TestCase):
    def setUp(self):
        WeComCorp.objects.all().delete()
        self.a = make_corp('a', is_default=True)
        self.b = make_corp('b')
        self.user = User.objects.create(username='u1', first_name='张三')
        self.user.profile.wechat_userid = 'wx_u1'
        self.user.profile.save()

    @staticmethod
    def _oauth_target(resp):
        """回调地址在 Location 里是 percent-encoded 的，断言前先解码。"""
        return unquote(resp['Location'])

    def test_corp_login_redirects_to_its_own_corp(self):
        """B 的入口必须用 B 的 corpid 去授权，否则换 code 时拿不到 UserId。"""
        resp = self.client.get(reverse('corp_login', args=['b']))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'appid={self.b.corp_id}', resp['Location'])
        self.assertNotIn(f'appid={self.a.corp_id}', resp['Location'])
        # 回调地址要带回企业标识
        self.assertIn('/accounts/c/b/', self._oauth_target(resp))

    def test_unknown_code_shows_an_error_instead_of_falling_back(self):
        """入口 code 写错时必须当场报错，**不能**退回默认企业。

        退回就是当初 B 登不进去的成因：后台 code 填的是 corp_id，工作台配的
        是 b-corp，员工被静默送去 A 的 OAuth，换回 OpenId 被拒，
        页面上只有一句「登录失败」—— 查日志才定位得到。
        """
        resp = self.client.get(reverse('corp_login', args=['nope']))
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('open.weixin.qq.com', resp.get('Location', ''))
        self.assertContains(resp, 'nope', status_code=404)

    def test_inactive_corp_entry_is_rejected_too(self):
        """停用的企业不该还能从入口进 —— 否则停用只是「列表里看不见」。"""
        self.b.is_active = False
        self.b.save(update_fields=['is_active'])
        resp = self.client.get(reverse('corp_login', args=['b']))
        self.assertEqual(resp.status_code, 404)

    def test_get_corp_strict_raises_for_unknown_code(self):
        from .wecom import UnknownCorp, get_corp_strict
        self.assertEqual(get_corp_strict('b'), self.b)
        with self.assertRaises(UnknownCorp):
            get_corp_strict('nope')

    def test_get_corp_still_falls_back_for_auto_login(self):
        """auto-login 没有 code，那条路径的兜底必须保留。"""
        from .wecom import get_corp
        self.assertEqual(get_corp(None), self.a)
        self.assertEqual(get_corp('nope'), self.a)

    def test_auto_login_uses_default_corp(self):
        """现网 A 企业工作台配的就是 auto-login，行为和改造前必须一致。"""
        resp = self.client.get(reverse('auto_login'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'appid={self.a.corp_id}', resp['Location'])
        # 回调路径保持原样不带企业
        self.assertIn('/accounts/auto-login/', self._oauth_target(resp))
        self.assertNotIn('/accounts/c/', self._oauth_target(resp))

    def test_same_corp_entry_reuses_session(self):
        self.client.force_login(self.user)
        session = self.client.session
        session['wecom_corp_code'] = 'a'
        session.save()

        resp = self.client.get(reverse('corp_login', args=['a']))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('kanban'))

    def test_cross_corp_entry_forces_reauth(self):
        """在 A 登录过之后点 B 的入口，不能直接进看板（否则身份还是 A 的）。"""
        self.client.force_login(self.user)
        session = self.client.session
        session['wecom_corp_code'] = 'a'
        session.save()

        resp = self.client.get(reverse('corp_login', args=['b']))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'appid={self.b.corp_id}', resp['Location'])

    @patch('accounts.views.get_user_detail', return_value={'name': '张三', 'department': []})
    @patch('accounts.views.get_userid_by_code', return_value='wx_u1')
    def test_oauth_callback_logs_in_and_records_corp(self, mock_userid, mock_detail):
        # 先走一遍入口，拿到 state 并写进 session
        self.client.get(reverse('corp_login', args=['b']))
        state = self.client.session['wecom_oauth_state']

        resp = self.client.get(
            reverse('corp_login', args=['b']), {'code': 'CODE', 'state': state},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('kanban'))
        self.assertEqual(self.client.session['wecom_corp_code'], 'b')
        # 换 code 时用的是 B 的凭证
        self.assertEqual(mock_userid.call_args[0][1].code, 'b')

    @patch('accounts.views.get_user_detail', return_value={'name': '张三', 'department': []})
    @patch('accounts.views.get_userid_by_code', return_value='wx_u1')
    def test_bad_state_rejected(self, mock_userid, mock_detail):
        self.client.get(reverse('corp_login', args=['b']))
        resp = self.client.get(
            reverse('corp_login', args=['b']), {'code': 'CODE', 'state': 'wrong'},
        )
        self.assertEqual(resp.status_code, 200)  # 错误页
        self.assertContains(resp, '登录状态校验失败')
        mock_userid.assert_not_called()
        # 错误页的重试按钮必须指回 B，否则点重试又跑到 A 去
        self.assertContains(resp, reverse('corp_login', args=['b']))

    @patch('accounts.views.get_userid_by_code', return_value=None)
    def test_error_page_has_no_raw_template_syntax(self, mock_userid):
        """Django 的 {# #} 注释**只能写在一行内**，跨行不会被当注释，
        而是原样渲染成页面上的可见文字。这个页面是给登录失败的用户看的，
        飘出一段源码注释非常难看，而且很容易在改模板时再次引入。"""
        self.client.get(reverse('corp_login', args=['b']))
        html = self.client.get(
            reverse('corp_login', args=['b']), {'code': 'CODE', 'state': 'wrong'},
        ).content.decode()

        # 先确认拿到的确实是错误页，否则下面的断言会空过
        self.assertIn('登录状态校验失败', html)
        self.assertNotIn('{#', html, '有未闭合/跨行的 {# #} 注释被渲染出来了')
        self.assertNotIn('#}', html)
        self.assertNotIn('{%', html, '有模板标签原样渲染出来了')


class SendMessageToUserTest(TestCase):
    """按收件人所在企业发送，是防止「用 A 的应用给 B 的人发消息」的护栏。"""

    def setUp(self):
        self.a = make_corp('a', is_default=True)
        self.user = User.objects.create(username='u1', first_name='张三')
        self.user.profile.wechat_userid = 'wx_u1'
        self.user.profile.save()

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_unbound_profile_falls_back_to_default_corp(self, mock_send):
        """过渡期 profile 还没有 wecom_corp 字段，行为要与改造前一致。"""
        from reminders.wechat import send_message_to_user

        self.assertTrue(send_message_to_user(self.user, '你好'))
        self.assertEqual(mock_send.call_args[0][:2], ('wx_u1', '你好'))
        self.assertEqual(mock_send.call_args[1]['corp'].code, 'a')

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_no_wechat_userid_sends_nothing(self, mock_send):
        from reminders.wechat import send_message_to_user

        other = User.objects.create(username='u2')
        self.assertFalse(send_message_to_user(other, '你好'))
        mock_send.assert_not_called()


class SharedDepartmentResolutionTest(TestCase):
    """企微部门 id → 共享部门 的换算。

    三个企业的部门 id 各自独立且**互相撞号**：A 的 id=16 是「产品部」，
    B 的 id=16 是「采购部」。绕过 WeComDept 直接查 Department.wechat_dept_id
    会让 B 的采购部员工拿到产品部的编辑权限 —— 这些用例就是拦这个的。
    """

    def setUp(self):
        WeComCorp.objects.all().delete()
        self.a = make_corp('a', is_default=True, name='甲公司')
        self.b = make_corp('b', name='乙公司')
        self.product_dept = Department.objects.create(name='产品部', wechat_dept_id=16)
        self.purchase_dept = Department.objects.create(name='采购部')
        self.root_dept = Department.objects.create(name='甲公司', wechat_dept_id=1)
        WeComDept.objects.create(corp=self.a, wechat_dept_id=16, name='产品部',
                                 department=self.product_dept)
        WeComDept.objects.create(corp=self.a, wechat_dept_id=1, name='甲公司',
                                 department=self.root_dept, is_root=True)

    def test_same_dept_id_in_another_corp_does_not_leak(self):
        """B 的 16 必须映射到 B 自己声明的部门，绝不能落到 A 的产品部。"""
        WeComDept.objects.create(corp=self.b, wechat_dept_id=16, name='采购部',
                                 department=self.purchase_dept)
        self.assertEqual(resolve_shared_department(self.b, [16]), self.purchase_dept)
        self.assertEqual(resolve_shared_department(self.a, [16]), self.product_dept)

    def test_root_department_is_skipped(self):
        """企微返回的数组含所有上级部门，根部门不能被当成主部门。"""
        self.assertIsNone(resolve_shared_department(self.a, [1]))
        # 根部门排在最前时，要跳过它取下一个
        self.assertEqual(resolve_shared_department(self.a, [1, 16]), self.product_dept)

    def test_unmapped_department_returns_none(self):
        """没映射过就返回 None，调用方保持原值 —— 绝不猜。"""
        self.assertIsNone(resolve_shared_department(self.b, [16]))
        self.assertIsNone(resolve_shared_department(self.b, [9999]))
        self.assertIsNone(resolve_shared_department(self.b, []))

    def test_string_dept_ids_are_accepted(self):
        """企微有时把 id 给成字符串。"""
        self.assertEqual(resolve_shared_department(self.a, ['16']), self.product_dept)

    def test_overrides_are_used_instead_of_the_database(self):
        """预演用 pending 映射顶掉库里的值 —— 实跑会先建 WeComDept 再解析。

        不这么做，`--dry-run` 会把所有人报成「部门未映射」，而实跑其实写上了，
        预演就失去意义。
        """
        self.assertIsNone(resolve_shared_department(self.b, [16]))
        self.assertEqual(
            resolve_shared_department(self.b, [16], overrides={16: self.purchase_dept}),
            self.purchase_dept)
        # overrides 里没有的 id 仍然解析不出来，不能凭空造
        self.assertIsNone(
            resolve_shared_department(self.b, [99], overrides={16: self.purchase_dept}))


class DepartmentAuthorityTest(TestCase):
    """部门归属：`profile.department` 只有一个值，必须由**主身份企业**说了算。

    实测冲突：陈荣标 A=财务部、B=产品部（同一个人，两个企业真的分属两个部门）。
    同步是 `--all` 循环跑的，不设这道闸就成了「谁排在后面谁的通讯录赢」——
    B 会用自己 60 人的部门盖掉 23 个重叠同事在 A 的部门，而部门直接决定
    编辑权限（`products/models.py` 的 `can_be_managed_by`）。
    """

    def setUp(self):
        WeComCorp.objects.all().delete()
        self.a = make_corp('a', is_default=True, name='甲公司')
        self.b = make_corp('b', name='乙公司')
        self.finance = Department.objects.create(name='财务部')
        self.product = Department.objects.create(name='产品部')
        WeComDept.objects.create(corp=self.a, wechat_dept_id=17, name='财务部',
                                 department=self.finance)
        WeComDept.objects.create(corp=self.b, wechat_dept_id=24, name='产品部',
                                 department=self.product)

    def test_primary_corp_may_set_the_department(self):
        user, _ = get_or_create_user_from_wechat('ChenRongBiao', '陈荣标', [17], corp=self.a)
        self.assertEqual(user.profile.department, self.finance)

    def test_non_primary_corp_does_not_move_the_department(self):
        """先按 A（财务部）建，再被 B 同步（产品部）—— 必须停在财务部。

        产品部带 39 个阶段的编辑权限，静默挪过去就是静默提权。
        """
        user, _ = get_or_create_user_from_wechat('ChenRongBiao', '陈荣标', [17], corp=self.a)

        same, created = get_or_create_user_from_wechat('ChenRongBiao', '陈荣标', [24], corp=self.b)

        self.assertFalse(created)
        self.assertEqual(same.pk, user.pk)
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.department, self.finance)

    def test_non_primary_corp_may_fill_an_empty_department(self):
        """原来是空部门时允许补上 —— 拦的是「改」，不是「填」。

        这个人主身份在 A 但 A 没映射出部门，B 能给出部门就该写上，
        否则他会卡在「其他（待设置部门）」。
        """
        user, _ = get_or_create_user_from_wechat('ChenRongBiao', '陈荣标', [], corp=self.a)
        self.assertIsNone(user.profile.department)

        get_or_create_user_from_wechat('ChenRongBiao', '陈荣标', [24], corp=self.b)

        user.profile.refresh_from_db()
        self.assertEqual(user.profile.department, self.product)

    def test_primary_corp_can_correct_a_stale_department(self):
        """阮春华在库里的「莱特维健」是旧逻辑取 dept_ids[0] 留下的错值（根部门排第一）。

        A 和 B 的通讯录都说财务部，所以主身份企业改得动它 —— 这是修 bug，
        不该被这道闸挡住。
        """
        root = Department.objects.create(name='甲公司根', wechat_dept_id=1)
        user, _ = get_or_create_user_from_wechat('ZiYu', '阮春华', [1], corp=self.a)
        user.profile.department = root
        user.profile.save(update_fields=['department'])

        get_or_create_user_from_wechat('ZiYu', '阮春华', [17], corp=self.a)

        user.profile.refresh_from_db()
        self.assertEqual(user.profile.department, self.finance)

    def test_new_user_gets_the_department_of_the_corp_that_created_them(self):
        """新建账号时这条身份就是主身份，所以管辖部门 —— 否则 B 的新人全是空部门。"""
        user, created = get_or_create_user_from_wechat('newbie', '新人', [24], corp=self.b)

        self.assertTrue(created)
        self.assertEqual(user.profile.department, self.product)


class MultiCorpIdentityTest(TestCase):
    """认人规则：① 本企业身份 → ② 姓名拼音 → ③ 新建。

    实测 B 企业 60 人里 23 人和 A 是同一批人。认错的后果是同一个人的待办
    散在两个账号里（现网曾丽萍那个 bug 的翻版），或者把他在另一边的
    消息通道改坏 —— 都是静默的。
    """

    def setUp(self):
        WeComCorp.objects.all().delete()
        self.a = make_corp('a', is_default=True, name='甲公司')
        self.b = make_corp('b', name='乙公司')

    def test_same_person_first_seen_by_identity(self):
        user, created = get_or_create_user_from_wechat('zhangsan', '张三', [], corp=self.a)
        self.assertTrue(created)

        again, created2 = get_or_create_user_from_wechat('zhangsan', '张三', [], corp=self.a)
        self.assertFalse(created2)
        self.assertEqual(again.pk, user.pk)
        self.assertEqual(WeComIdentity.objects.filter(user=user).count(), 1)

    def test_merge_by_name_across_corps(self):
        """同一个人换了个企业登录 —— 必须归并，不能建第二个账号。"""
        user, _ = get_or_create_user_from_wechat('zs_a', '张三', [], corp=self.a)
        merged, created = get_or_create_user_from_wechat('zs_b', '张三', [], corp=self.b)

        self.assertFalse(created)
        self.assertEqual(merged.pk, user.pk)
        self.assertEqual(User.objects.filter(first_name='张三').count(), 1)
        self.assertEqual(WeComIdentity.objects.filter(user=user).count(), 2)

    def test_merge_does_not_overwrite_the_other_corp_userid(self):
        """阮仕云 A=Ruanivan / B=ivanRuan。

        以前 account/utils.py 会把 profile.wechat_userid 直接覆盖成当前企业的，
        结果他在 A 那边的消息就发不出去了。主身份是 A，就必须保持 A 的 userid。
        """
        user, _ = get_or_create_user_from_wechat('Ruanivan', '阮仕云', [], corp=self.a)
        get_or_create_user_from_wechat('ivanRuan', '阮仕云', [], corp=self.b)

        user.profile.refresh_from_db()
        self.assertEqual(user.profile.wechat_userid, 'Ruanivan')
        self.assertEqual(user.profile.wecom_corp_id, self.a.pk)
        # 两个 userid 都要存得下来
        self.assertEqual(
            set(WeComIdentity.objects.filter(user=user).values_list('userid', flat=True)),
            {'Ruanivan', 'ivanRuan'})

    def test_two_people_same_name_in_one_corp_stay_separate(self):
        """同一个企业里两个同名的人 —— 一道今天不会触发的保险。

        规则是「姓名相同的共用一个账号」（跨企业归并同一个人）。但如果**同一个企业**
        里已经有人用这个名字绑过身份了，那说明是两个人，再并就会让两个人共用账号、
        互相看到对方的待办。

        实测 A 企业 170 人、B 企业 60 人，两边都没有同名的人，所以这道判断今天是
        空转的；留着的意义是防止将来招进来第二个同名的人时静默并错。
        """
        first, _ = get_or_create_user_from_wechat('ceng1', '曾丽萍', [], corp=self.a)
        second, created = get_or_create_user_from_wechat('ceng2', '曾丽萍', [], corp=self.a)

        self.assertTrue(created)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(User.objects.filter(first_name='曾丽萍').count(), 2)

    def test_same_name_in_another_corp_still_merges(self):
        """这道保险只在**同一个企业内**生效 —— 换企业的同名必须归并。

        现网唯一一组同名账号（曾丽萍）绑的是同一个 userid，本来就是一个人。
        """
        first, _ = get_or_create_user_from_wechat('ceng_a', '曾丽萍', [], corp=self.a)
        second, created = get_or_create_user_from_wechat('ceng_b', '曾丽萍', [], corp=self.b)

        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)

    def test_new_account_gets_unique_username_when_pinyin_taken(self):
        """本企业已有同名身份，新建的账号不能撞 username 唯一约束。"""
        other, _ = get_or_create_user_from_wechat('zs_1', '张三', [], corp=self.b)
        user, created = get_or_create_user_from_wechat('zs_2', '张三', [], corp=self.b)

        self.assertTrue(created)
        self.assertNotEqual(user.pk, other.pk)
        self.assertNotEqual(user.username.lower(), 'zhangsan')

    def test_identity_wins_over_a_lookalike_name(self):
        """身份优先于姓名：本企业身份指向谁就是谁，不受同名账号干扰。"""
        right, _ = get_or_create_user_from_wechat('right', '张三', [], corp=self.a)
        User.objects.create(username='ZhangSanAnother', first_name='张三')

        found, created = get_or_create_user_from_wechat('right', '张三', [], corp=self.a)
        self.assertFalse(created)
        self.assertEqual(found.pk, right.pk)

    def test_primary_identity_keeps_first_corp(self):
        user, _ = get_or_create_user_from_wechat('u_a', '李四', [], corp=self.a)
        get_or_create_user_from_wechat('u_b', '李四', [], corp=self.b)

        primaries = WeComIdentity.objects.filter(user=user, is_primary=True)
        self.assertEqual(primaries.count(), 1)
        self.assertEqual(primaries.first().corp_id, self.a.pk)

    def test_department_is_resolved_through_wecomdept(self):
        dept = Department.objects.create(name='采购部')
        WeComDept.objects.create(corp=self.b, wechat_dept_id=16, name='采购部',
                                 department=dept)
        user, _ = get_or_create_user_from_wechat('u', '王五', [16], corp=self.b)
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.department_id, dept.pk)

    def test_unmapped_department_leaves_profile_untouched(self):
        """B 的部门还没映射时不能瞎猜，保持原值（哪怕原来是空的）。"""
        user, _ = get_or_create_user_from_wechat('u', '王五', [16], corp=self.b)
        user.profile.refresh_from_db()
        self.assertIsNone(user.profile.department_id)


class SendMessageByPrimaryIdentityTest(TestCase):
    """发消息按主身份选企业 —— 拿错企业会静默发不出去。"""

    def setUp(self):
        WeComCorp.objects.all().delete()
        self.a = make_corp('a', is_default=True)
        self.b = make_corp('b')

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_uses_primary_identity(self, mock_send):
        from reminders.wechat import send_message_to_user

        user = User.objects.create(username='u', first_name='张三')
        WeComIdentity.objects.create(corp=self.a, userid='id_a', user=user, is_primary=True)
        WeComIdentity.objects.create(corp=self.b, userid='id_b', user=user)

        self.assertTrue(send_message_to_user(user, '你好'))
        self.assertEqual(mock_send.call_args[0][:2], ('id_a', '你好'))
        self.assertEqual(mock_send.call_args[1]['corp'].code, 'a')

    @patch('reminders.wechat.send_wechat_message', return_value=True)
    def test_falls_back_to_profile_when_no_identity(self, mock_send):
        """迁移之前建的账号还没有身份记录，行为必须和改造前一致。"""
        from reminders.wechat import send_message_to_user

        user = User.objects.create(username='u2', first_name='李四')
        user.profile.wechat_userid = 'wx_legacy'
        user.profile.save()

        self.assertTrue(send_message_to_user(user, '你好'))
        self.assertEqual(mock_send.call_args[0][:2], ('wx_legacy', '你好'))


class TokenCacheTest(TestCase):
    def setUp(self):
        # _token_cache 是模块级 dict，会在同一个测试进程里跨用例存活，必须清掉
        from reminders import wechat
        wechat._token_cache.clear()
        WeComCorp.objects.all().delete()
        self.a = make_corp('a', is_default=True)

    def tearDown(self):
        from reminders import wechat
        wechat._token_cache.clear()

    def _fake_get(self, token_value):
        class FakeResp:
            @staticmethod
            def json():
                return {'errcode': 0, 'access_token': token_value, 'expires_in': 7200}
        return FakeResp

    def test_token_cached_per_corp(self):
        """A 和 B 的 token 不能互相污染。"""
        from reminders import wechat

        b = make_corp('b')
        with patch.object(wechat.requests, 'get', return_value=self._fake_get('token_a')):
            self.assertEqual(wechat.get_access_token(self.a), 'token_a')
        with patch.object(wechat.requests, 'get', return_value=self._fake_get('token_b')):
            self.assertEqual(wechat.get_access_token(b), 'token_b')
        # 回到 A 应该命中缓存，不再请求（上面的 patch 已退出，真发请求会失败）
        self.assertEqual(wechat.get_access_token(self.a), 'token_a')

    def test_rotated_secret_invalidates_cache(self):
        """后台重置了 Secret 应立刻生效，不用重启进程。"""
        from reminders import wechat

        with patch.object(wechat.requests, 'get', return_value=self._fake_get('old')):
            self.assertEqual(wechat.get_access_token(self.a), 'old')

        self.a.app_secret = 'secret_rotated'
        self.a.save()

        with patch.object(wechat.requests, 'get', return_value=self._fake_get('new')) as mock_get:
            self.assertEqual(wechat.get_access_token(self.a), 'new')
            mock_get.assert_called_once()
