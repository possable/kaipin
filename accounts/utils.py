"""用户名/拼音工具函数"""
import logging
from django.contrib.auth.models import User
from pypinyin import pinyin, Style

logger = logging.getLogger(__name__)

DEFAULT_PASSWORD = 'Aa123456'


def name_to_pinyin(chinese_name):
    """中文姓名转拼音用户名：每个字首字母大写，如 张三 → ZhangSan"""
    if not chinese_name:
        return None
    py_list = pinyin(chinese_name, style=Style.NORMAL)
    parts = [p[0] for p in py_list]
    username = ''.join(part.capitalize() for part in parts)
    # 修复 pypinyin 的 ü 拼写问题：Lve → Lue
    if 'Lve' in username:
        username = username.replace('Lve', 'Lue')
    return username


def get_or_create_user_from_wechat(wechat_userid, chinese_name, dept_ids=None):
    """根据企业微信信息获取或创建用户，统一定规则：
    - username = 姓名拼音（首字母大写）
    - 密码 = 默认密码（可登录，大小写不敏感）
    - 绑定 wechat_userid 和部门
    返回 (user, created)
    """
    username = name_to_pinyin(chinese_name)
    if not username:
        username = wechat_userid  # 兜底用企微ID

    # 大小写不敏感查找已有用户
    user = User.objects.filter(username__iexact=username).first()
    if user:
        changed = False
        if not user.is_active:
            user.is_active = True
            changed = True
        if user.first_name != chinese_name:
            user.first_name = chinese_name
            changed = True
        if user.username != username:
            user.username = username
            changed = True
        if changed:
            user.save(update_fields=['is_active', 'first_name', 'username'])
            logger.info(f'企微同步更新用户: {chinese_name} -> {username}')
        created = False
    else:
        user = User.objects.create(
            username=username,
            first_name=chinese_name,
            is_active=True,
        )
        user.set_password(DEFAULT_PASSWORD)
        user.save()
        logger.info(f'企微同步创建用户: {chinese_name} -> {username}')
        created = True

    # 更新企微信息
    profile = user.profile
    if profile.wechat_userid != wechat_userid:
        profile.wechat_userid = wechat_userid
        profile.save(update_fields=['wechat_userid'])

    if dept_ids:
        from accounts.models import Department
        main_dept = Department.objects.filter(wechat_dept_id=dept_ids[0]).first()
        if main_dept and profile.department_id != main_dept.id:
            profile.department = main_dept
            profile.save(update_fields=['department'])

    return user, created
