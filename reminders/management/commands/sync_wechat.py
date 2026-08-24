"""
企业微信组织架构同步命令

用法: python manage.py sync_wechat

1. 拉取企业微信部门列表 → 同步到 Department 模型
2. 拉取每个部门的成员列表 → 创建/更新 Django User + UserProfile
"""
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils.crypto import get_random_string
from accounts.models import Department, UserProfile
from reminders.wechat import get_access_token
import requests

WECHAT_API = 'https://qyapi.weixin.qq.com/cgi-bin'


class Command(BaseCommand):
    help = '从企业微信同步部门与成员到本地系统'

    def handle(self, *args, **options):
        token = get_access_token()
        if not token:
            self.stderr.write(self.style.ERROR('获取企业微信 access_token 失败，请检查配置'))
            return

        # Step 1: 同步部门
        self.stdout.write('\n=== 同步部门 ===')
        dept_list = self._fetch_departments(token)
        if dept_list is None:
            self.stderr.write(self.style.ERROR('获取部门列表失败'))
            return

        dept_map = {}  # wechat_dept_id → Department 实例
        for wx_dept in dept_list:
            dept = self._sync_department(wx_dept)
            dept_map[wx_dept['id']] = dept

        # 列出仍未关联企业微信的旧部门
        old_depts = Department.objects.filter(wechat_dept_id__isnull=True)
        if old_depts.exists():
            self.stdout.write(f'\n  未关联企业微信的部门: {", ".join(d.name for d in old_depts)}（保留不动）')

        # Step 2: 同步用户
        self.stdout.write('\n=== 同步成员 ===')
        total_created = 0
        total_updated = 0

        for wx_dept_id, dept in dept_map.items():
            user_list = self._fetch_users(token, wx_dept_id)
            if user_list is None:
                self.stderr.write(f'  获取部门 {dept.name} 成员列表失败，跳过')
                continue

            for wx_user in user_list:
                userid = wx_user['userid']
                display_name = wx_user.get('name', userid)

                # 创建或更新 Django User
                user, created = User.objects.update_or_create(
                    username=userid,
                    defaults={
                        'first_name': display_name,
                        'email': wx_user.get('email', '') or f'{userid}@kaipin.local',
                    },
                )

                if created:
                    # 新用户设随机初始密码
                    user.set_password(get_random_string(length=12))
                    user.save()
                    total_created += 1
                    self.stdout.write(f'  + {display_name} ({userid}) → {dept.name}')
                else:
                    total_updated += 1

                # 同步 UserProfile
                profile = user.profile
                profile.department = dept
                profile.wechat_userid = userid
                profile.save()

        self.stdout.write(self.style.SUCCESS(
            f'\n同步完成: 部门 {len(dept_map)} 个, 新建用户 {total_created} 人, 更新用户 {total_updated} 人'
        ))

    def _sync_department(self, wx_dept):
        """同步单个部门：先按 wechat_dept_id 匹配，再按 name 匹配，都不行则新建"""
        wx_id = wx_dept['id']
        wx_name = wx_dept['name']

        # 1. 按 wechat_dept_id 精确匹配
        try:
            dept = Department.objects.get(wechat_dept_id=wx_id)
            dept.name = wx_name
            dept.save()
            self.stdout.write(f'  {dept.name} [ID:{wx_id}] (已存在)')
            return dept
        except Department.DoesNotExist:
            pass

        # 2. 按名称匹配（处理之前手动创建、未关联 wechat_dept_id 的部门）
        try:
            dept = Department.objects.get(name=wx_name, wechat_dept_id__isnull=True)
            dept.wechat_dept_id = wx_id
            dept.save()
            self.stdout.write(f'  {dept.name} [ID:{wx_id}] (关联已有部门)')
            return dept
        except Department.DoesNotExist:
            pass

        # 3. 名称存在但已关联了其他 wechat_dept_id（企业微信中有同名部门）
        try:
            dept = Department.objects.get(name=wx_name)
            self.stderr.write(f'  ⚠ {wx_name} 已关联 ID:{dept.wechat_dept_id}，跳过企业微信 ID:{wx_id}')
            return dept
        except Department.DoesNotExist:
            pass

        # 4. 完全新建
        dept = Department.objects.create(name=wx_name, wechat_dept_id=wx_id)
        self.stdout.write(f'  + {dept.name} [ID:{wx_id}] (新建)')
        return dept

    def _fetch_departments(self, token):
        """获取企业微信部门列表"""
        resp = requests.get(
            f'{WECHAT_API}/department/list',
            params={'access_token': token},
            timeout=15,
        )
        data = resp.json()
        if data.get('errcode') != 0:
            self.stderr.write(f'  错误: {data}')
            return None
        return data.get('department', [])

    def _fetch_users(self, token, dept_id):
        """获取某部门下的成员列表（简单信息）"""
        resp = requests.get(
            f'{WECHAT_API}/user/list',
            params={
                'access_token': token,
                'department_id': dept_id,
                'fetch_child': 0,  # 只取本部门直属成员，不递归
            },
            timeout=15,
        )
        data = resp.json()
        if data.get('errcode') != 0:
            self.stderr.write(f'  部门 {dept_id} 错误: {data}')
            return None
        return data.get('userlist', [])
