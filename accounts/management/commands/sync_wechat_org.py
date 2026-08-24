"""每日同步企业微信组织架构：部门 + 成员。已离职的设为 is_active=False。"""
import logging
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from accounts.models import Department, UserProfile
from accounts.utils import get_or_create_user_from_wechat
from reminders.wechat import get_access_token, get_department_list, get_department_users, get_user_detail

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '从企业微信同步全公司组织架构（部门+成员），每日运行一次'

    def handle(self, *args, **options):
        self.stdout.write('开始同步企业微信组织架构...')
        token = get_access_token()
        if not token:
            self.stderr.write('获取企业微信 access_token 失败，中止同步')
            return

        # 1. 同步部门
        dept_list = get_department_list(token)
        if dept_list is None:
            self.stderr.write('获取部门列表失败，中止同步')
            return

        dept_id_map = {}  # wechat_dept_id → Department
        for d in dept_list:
            dept, created = Department.objects.update_or_create(
                wechat_dept_id=d['id'],
                defaults={'name': d['name']},
            )
            dept_id_map[d['id']] = dept
            if created:
                self.stdout.write(f'  新增部门: {dept.name}')

        # 2. 从根部门(1)获取所有用户
        userlist = get_department_users(1, token, fetch_child=True)
        if userlist is None:
            self.stderr.write('获取用户列表失败，中止同步')
            return

        synced_user_ids = set()
        for u in userlist:
            synced_user_ids.add(u['userid'])
            # 获取用户详细信息（含姓名、部门列表）
            detail = get_user_detail(u['userid'])
            name = (detail.get('name', '') if detail else '') or u.get('name', '') or u['userid']
            dept_ids = (detail.get('department', []) if detail else []) or u.get('department', [])

            was_inactive = User.objects.filter(
                profile__wechat_userid=u['userid'], is_active=False
            ).exists()

            user, created = get_or_create_user_from_wechat(u['userid'], name, dept_ids)
            if created:
                self.stdout.write(f'  新增用户: {name} ({user.username})')
            elif was_inactive:
                self.stdout.write(f'  重新激活用户: {name} ({user.username})')

        # 3. 标记已离职：有企业微信ID但在本次同步中未出现的用户
        departed = User.objects.filter(
            is_active=True,
            profile__wechat_userid__gt='',
        ).exclude(profile__wechat_userid__in=synced_user_ids)

        for user in departed:
            user.is_active = False
            user.save()
            self.stdout.write(f'  标记离职: {user.first_name or user.username} ({user.profile.wechat_userid})')

        self.stdout.write(self.style.SUCCESS(
            f'同步完成：{Department.objects.count()} 个部门，'
            f'{len(synced_user_ids)} 个在职用户，{departed.count()} 个离职'
        ))
