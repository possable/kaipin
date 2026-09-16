"""每日同步企业微信组织架构（部门 + 成员）。多企业版。

每个企业各同步一次：
- 部门写进 WeComDept（再映射到**共享的** Department），
  **不再写 Department.wechat_dept_id** —— 三个企业的部门 id 互相撞号，
  往那一列写会把 A 的部门映射改坏。
- 成员走 accounts.utils.get_or_create_user_from_wechat，按
  「本企业身份 → 姓名拼音 → 新建」认人，同一个人跨企业归并到同一个账号。

用法：
    manage.py sync_wechat_org                     # 只同步默认企业
    manage.py sync_wechat_org --corp b-corp       # 指定一个企业
    manage.py sync_wechat_org --all               # 所有启用的企业
    manage.py sync_wechat_org --all --dry-run     # 只看会改什么，不落库
    manage.py sync_wechat_org --all --no-deactivate

⚠️ 离职判定只在本命令**纳入范围的企业**里成立：
    一个人从 B 离职但还在 A，不会被停用（否则 A 那边的待办就没人处理了）。
    只有「他在所有纳入同步的企业里都查无此人」才会被停用。
"""
import logging

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from accounts.models import Department, WeComDept, WeComCorp, WeComIdentity
from accounts.utils import (
    decide_department, get_or_create_user_from_wechat, name_to_pinyin,
    resolve_shared_department,
)
from reminders.wechat import (
    get_access_token, get_department_list, get_department_users,
)

logger = logging.getLogger(__name__)


def _dept_name(user):
    dept = getattr(getattr(user, 'profile', None), 'department', None)
    return dept.name if dept is not None else '未设置'


class Command(BaseCommand):
    help = '从企业微信同步组织架构（部门+成员），支持多企业'

    def add_arguments(self, parser):
        parser.add_argument('--corp', help='只同步这个 code 的企业（默认：默认企业）')
        parser.add_argument('--all', action='store_true', help='同步所有启用的企业')
        parser.add_argument('--dry-run', action='store_true',
                            help='只打印会做什么，不写数据库')
        parser.add_argument('--no-deactivate', action='store_true',
                            help='不做离职停用')

    def handle(self, *args, **options):
        self.dry_run = options['dry_run']
        self.no_deactivate = options['no_deactivate']

        corps = self._resolve_corps(options)
        if self.dry_run:
            self.stdout.write(self.style.WARNING('=== 预演模式，不会写数据库 ==='))

        # corp_id → 本次在该企业通讯录里见到的 userid 集合
        synced = {}
        for corp in corps:
            self.stdout.write(f'\n===== {corp.name}（{corp.code}）=====')
            seen = self._sync_one(corp)
            if seen is None:
                self.stderr.write(self.style.ERROR(f'  {corp.code} 同步失败，跳过'))
                continue
            synced[corp.pk] = seen

        if not self.no_deactivate and synced:
            self._deactivate_departed(synced)
        elif self.no_deactivate:
            self.stdout.write('\n（--no-deactivate，跳过离职判定）')

        self.stdout.write(self.style.SUCCESS('\n同步结束'))

    # ------------------------------------------------------------------
    def _resolve_corps(self, options):
        if options['all']:
            corps = list(WeComCorp.objects.filter(is_active=True))
            if not corps:
                raise CommandError('没有启用的企业')
            return corps
        if options['corp']:
            corp = WeComCorp.objects.filter(code=options['corp']).first()
            if corp is None:
                raise CommandError(f'没有 code={options["corp"]!r} 的企业')
            return [corp]
        corp = (WeComCorp.objects.filter(is_default=True).first()
                or WeComCorp.objects.filter(is_active=True).first())
        if corp is None:
            raise CommandError('没有任何企业，先到 Django admin 里加一个')
        return [corp]

    def _sync_one(self, corp):
        """同步一个企业，返回本次见到的 userid 集合；失败返回 None。"""
        # ⚠️ gettoken **不受**「企业可信 IP」限制（2026-09-15 实测三家都是 errcode=0），
        # 所以这里失败基本只有一种原因：corp_id / app_secret 填错了。
        # IP 没白名单会在下一步的 business 接口上以 60020 报出来，别在这里提示 60020。
        token = get_access_token(corp)
        if not token:
            self.stderr.write('  取 access_token 失败（corp_id 或 app_secret 不对？）')
            return None

        dept_list = get_department_list(corp=corp)
        if dept_list is None:
            self.stderr.write('  取部门列表失败 —— errcode 说明见上一行日志')
            return None

        dept_map = self._sync_departments(corp, dept_list)

        root_ids = [d['id'] for d in dept_list if not d.get('parentid')]
        root_id = root_ids[0] if root_ids else 1

        userlist = get_department_users(root_id, corp=corp, fetch_child=True)
        if userlist is None:
            self.stderr.write('  取成员列表失败')
            return None

        seen = set()
        created_n = merged_n = 0
        for item in userlist:
            userid = item['userid']
            seen.add(userid)

            # user/list 已经带回了姓名和部门，不必再逐个调 user/get
            # （170 人就是 170 次多余请求，还容易触发频率限制）。
            name = item.get('name') or userid
            # 企微明确给了主部门，比「取数组第一个」可靠 —— 那个数组含所有
            # 上级部门且顺序不保证。把主部门排到最前，由 resolve 按序取第一个能映射的。
            dept_ids = list(item.get('department') or [])
            main_dept = item.get('main_department')
            if main_dept is not None:
                dept_ids = [main_dept] + [d for d in dept_ids if d != main_dept]

            if self.dry_run:
                action = self._predict(userid, name, corp, dept_ids, dept_map)
                if action:
                    self.stdout.write(f'    {action}')
                    if action.startswith('归并'):
                        merged_n += 1
                    elif action.startswith('新建'):
                        created_n += 1
                continue

            was_inactive = User.objects.filter(
                is_active=False, wecom_identities__corp=corp, wecom_identities__userid=userid,
            ).exists()
            user, created = get_or_create_user_from_wechat(
                userid, name, dept_ids, corp=corp)
            if created:
                created_n += 1
                self.stdout.write(f'  新增用户: {name} ({user.username})')
            elif was_inactive:
                self.stdout.write(f'  重新激活: {name} ({user.username})')

        self.stdout.write(f'  通讯录 {len(seen)} 人，新建 {created_n} 人'
                          + (f'，计划归并 {merged_n} 人' if self.dry_run else ''))
        return seen

    def _sync_departments(self, corp, dept_list):
        """把企微部门写进 WeComDept，并按名称自动映射到共享 Department。

        按名称映射正是设计意图：三个企业的「设计部」都指向同一个
        Department('设计部') 行，所以 `stage.department == user.profile.department`
        这些权限判断一行都不用改。

        映射不出来就留空，等人工在 admin 里指 —— 绝不猜。

        返回同步**之后**生效的 `{企微部门 id: Department}`（只含已映射的非根部门），
        预演拿它当 overrides 喂给成员解析，否则预演算不出部门。
        """
        created = mapped = 0
        resolved = {}
        for d in dept_list:
            wx_id, wx_name = d['id'], d['name']
            is_root = not d.get('parentid')
            row = WeComDept.objects.filter(corp=corp, wechat_dept_id=wx_id).first()

            if row is None:
                if self.dry_run:
                    self.stdout.write(f'  新增部门映射: {wx_id} {wx_name}'
                                      + ('（根部门）' if is_root else ''))
                    created += 1
                    # ⚠️ 这里**不能 continue**：新建的行同样会走下面的自动映射。
                    # 提前返回会让预演少报映射，「先 --dry-run 人工核对」就失去意义。
                    # 用一个未保存的实例把后续判断跑在同一套逻辑上。
                    row = WeComDept(corp=corp, wechat_dept_id=wx_id,
                                    name=wx_name, is_root=is_root)
                else:
                    row = WeComDept.objects.create(
                        corp=corp, wechat_dept_id=wx_id, name=wx_name, is_root=is_root)
                    created += 1
            elif not self.dry_run:
                changed = []
                if row.name != wx_name:
                    row.name, _ = wx_name, changed.append('name')
                if row.is_root != is_root:
                    row.is_root, _ = is_root, changed.append('is_root')
                if changed:
                    row.save(update_fields=changed)

            # 人工指定过的映射永不覆盖 —— 否则每天 07:50 会把修正打回去
            if row.is_manual or row.department_id or is_root:
                target = None if is_root else row.department
            else:
                target = Department.objects.filter(name=wx_name).first()
                if target is None:
                    continue
                if self.dry_run:
                    self.stdout.write(f'  自动映射: {corp.code}/{wx_id} {wx_name} → {target.name}')
                else:
                    row.department = target
                    row.save(update_fields=['department'])
                mapped += 1

            if target is not None:
                resolved[wx_id] = target

        self.stdout.write(f'  部门 {len(dept_list)} 个，新建映射 {created} 个，自动映射 {mapped} 个')
        return resolved

    def _predict(self, userid, name, corp, dept_ids, dept_map):
        """dry-run 用：不落库地判断这个人会被怎么处理。

        部门那一支走**和实跑同一个** decide_department。分开写两份判断必然走偏，
        而预演说「不动部门」实跑却动了，正是预演最该挡住的那类事故。
        """
        ident = (WeComIdentity.objects.filter(corp=corp, userid=userid)
                 .select_related('user', 'user__profile', 'user__profile__department').first())
        if ident is not None:
            user = ident.user
            dept, conflict = decide_department(user, corp, dept_ids, overrides=dept_map)
            if dept is not None:
                return f'改部门: {name} {_dept_name(user)} → {dept.name}'
            if conflict:
                return f'部门冲突(不改): {name} 主身份在 {conflict}'
            if not user.is_active:
                return f'重新激活: {name} ({user.username})'
            return None

        base = name_to_pinyin(name) or userid
        cand = User.objects.filter(username__iexact=base).first()
        if cand is not None and WeComIdentity.objects.filter(corp=corp, user=cand).exists():
            cand = None

        base_dept = resolve_shared_department(corp, dept_ids, overrides=dept_map)

        if cand is not None:
            # 归并的人若主身份在别处，部门不受本企业通讯录摆布（见 decide_department）
            dept, conflict = decide_department(cand, corp, dept_ids, overrides=dept_map)
            if conflict:
                return (f'归并: {name} → 已有账号 {cand.username} (pk={cand.pk})'
                        f'，部门冲突(不改，主身份在 {conflict})')
            if dept is not None:
                return (f'归并: {name} → 已有账号 {cand.username} (pk={cand.pk})'
                        f'，部门 {_dept_name(cand)} → {dept.name}')
            return (f'归并: {name} → 已有账号 {cand.username} (pk={cand.pk})'
                    f'，部门不变（{_dept_name(cand)}）')

        # 新建的人在这家企业落下第一条身份，没有别的身份，所以这条就是主身份
        # → 部门按本企业通讯录写。
        suffix = f'，部门={base_dept.name}' if base_dept is not None else '，部门未映射'
        return f'新建: {name} ({base}){suffix}'

    def _deactivate_departed(self, synced):
        """把查无此人的账号停用。

        ⚠️ 只在「他在本次纳入同步的所有企业里都没出现」时才停用。
        否则一个人从 B 离职但还在 A，一跑同步就把他在 A 的账号停了，
        A 那边挂在他名下的待办和品就没人管了。
        """
        scope = set(synced)
        self.stdout.write('\n===== 离职判定 =====')
        n = 0
        for ident in WeComIdentity.objects.filter(corp_id__in=scope).select_related('user', 'corp'):
            if ident.userid in synced[ident.corp_id]:
                continue
            user = ident.user

            outside = WeComIdentity.objects.filter(user=user).exclude(corp_id__in=scope)
            if outside.exists():
                names = '、'.join(i.corp.code for i in outside)
                self.stdout.write(f'  跳过 {user.first_name or user.username}：'
                                  f'已在 {ident.corp.code} 查无此人，但在 {names} 仍有身份')
                continue

            # 在本企业（及本次范围）查无此人，且没有别的企业身份 → 真离职
            still = WeComIdentity.objects.filter(user=user).exclude(
                userid__in=synced[ident.corp_id]).exclude(corp_id=ident.corp_id).exists()
            if still:
                continue
            if user.is_active:
                if self.dry_run:
                    self.stdout.write(f'  会停用: {user.first_name or user.username}'
                                      f'（{ident.corp.code}/{ident.userid}）')
                else:
                    user.is_active = False
                    user.save(update_fields=['is_active'])
                    self.stdout.write(f'  标记离职: {user.first_name or user.username}'
                                      f'（{ident.corp.code}/{ident.userid}）')
                n += 1
        self.stdout.write(f'  共 {n} 人{"（预演）" if self.dry_run else ""}')
