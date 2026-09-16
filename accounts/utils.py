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


def resolve_shared_department(corp, dept_ids, overrides=None):
    """把企微返回的 department 数组换算成共享的 Department。换算不出来返回 None。

    ⚠️ 绝不能拿企微部门 id 直接查 `Department.wechat_dept_id`：三个企业的部门 id
    各自独立且**互相撞号** —— A 的 id=16 是「产品部」，B 的 id=16 是「采购部」。
    直接查会让 B 的采购部员工拿到产品部编辑权限
    （`accounts/decorators.py` 和 `templates/base.html` 都在判断 `name == '产品部'`）。
    必须经过 WeComDept 换算。

    取「第一个已映射且非根部门」的：企微返回的数组含所有上级部门，
    顺序不保证主部门在前，所以不能像以前那样取 dept_ids[0]。

    `overrides`：`{企微部门 id: Department}`，给 `--dry-run` 用。预演不建
    WeComDept 行，但同步**第一步就是建行再解析**，所以真实运行解析得到的东西
    比预演多。不把「即将建立的映射」喂进来，预演会把 60 个人全报成「部门未映射」，
    而实跑会给其中一部分写上部门 —— 预演就白做了。传进来的映射必须已经剔掉
    根部门和未映射项（与下面的查询条件等价）。
    """
    if corp is None or not dept_ids:
        return None

    # 企微有时给字符串，统一成 int 再查
    wanted = []
    for d in dept_ids:
        try:
            wanted.append(int(d))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return None

    if overrides is not None:
        by_id = overrides
    else:
        from accounts.models import WeComDept
        rows = WeComDept.objects.filter(
            corp=corp, wechat_dept_id__in=wanted,
            department__isnull=False, is_root=False,
        )
        by_id = {r.wechat_dept_id: r.department for r in rows}

    for did in wanted:
        dept = by_id.get(did)
        if dept is not None:
            return dept
    return None


def get_primary_identity(user):
    """该账号的主身份（发企微消息用它）。没有身份则返回 None。"""
    from accounts.models import WeComIdentity
    return (WeComIdentity.objects.filter(user=user)
            .select_related('corp').order_by('-is_primary', 'id').first())


def decide_department(user, corp, dept_ids, overrides=None):
    """算出同步该给这个人写什么部门，返回 (新 Department 或 None, 冲突企业 code 或 None)。

    两条路径共用这一个函数：`get_or_create_user_from_wechat` 写库，
    `sync_wechat_org --dry-run` 只打印。分开写两份判断必然走偏 ——
    预演说「不动」而实跑动了，正是最需要预演挡住的那类事故。

    ⚠️ **主身份企业**的通讯录才能改部门；别的企业只能在空着时补上。

    同一个人可能在两个企业分属两个部门（实测：陈荣标 A=财务部、B=产品部），
    而 profile.department 只有一个值。不设这道闸，`--all` 每天跑一遍就成了
    「谁排在后面谁的通讯录赢」：B 会用自己 60 人的部门盖掉 23 个重叠同事在 A 的
    部门 —— 而部门直接决定编辑权限（products/models.py 的 can_be_managed_by）。

    允许「填空」是因为新建的 B 员工主身份就是 B，不需要这条豁免；真正需要它的是
    「主身份企业在通讯录里没映射出部门」的人 —— 让他一直挂在「未设置」上，
    不如按另一边通讯录补一个。

    ⚠️ 已知局限：主身份在 B 的人后来加入 A 时，A 的通讯录**改不动**他的部门
    （他不是空的）。这种情况今天一个都没有，真出现了会在 admin 里看出来 ——
    那时该由人来定他去哪个部门，而不是让同步每天替他选。

    返回 (None, None) 表示不用动；返回 (None, code) 表示存在冲突、按主身份不动。
    """
    dept = resolve_shared_department(corp, dept_ids, overrides=overrides)
    if dept is None or user.profile.department_id == dept.id:
        return None, None
    owner = get_primary_identity(user)
    if owner is not None and owner.corp_id != corp.pk:
        if user.profile.department_id is None:
            return dept, None           # 空着 —— 可以补，不算改
        return None, owner.corp.code    # 已有部门 —— 只有主身份企业能改
    return dept, None


def sync_primary_cache(user):
    """把 profile 上的 wechat_userid / wecom_corp 对齐到当前主身份。

    这两个字段现在只是**主身份的缓存**（很多地方在读，不改读取点），
    真相在 WeComIdentity。多企业的人会有两套 userid，一个字段存不下，
    所以这里永远只写主身份那一套。
    """
    primary = get_primary_identity(user)
    if primary is None:
        return
    profile = user.profile
    if (profile.wechat_userid != primary.userid
            or profile.wecom_corp_id != primary.corp_id):
        profile.wechat_userid = primary.userid
        profile.wecom_corp_id = primary.corp_id
        profile.save(update_fields=['wechat_userid', 'wecom_corp'])


def _touch_user(user, chinese_name):
    """把企微那边的姓名/在职状态同步过来（保持改造前的行为）。"""
    changed = []
    if not user.is_active:
        user.is_active = True
        changed.append('is_active')
    if chinese_name and user.first_name != chinese_name:
        user.first_name = chinese_name
        changed.append('first_name')
    if changed:
        user.save(update_fields=changed)


def _unique_username(base, wechat_userid):
    """生成一个没被占用的 username。base 撞了就用 userid 兜底。"""
    if not User.objects.filter(username__iexact=base).exists():
        return base
    candidate = f'{base}_{wechat_userid}'[:150]
    suffix = 1
    while User.objects.filter(username__iexact=candidate).exists():
        suffix += 1
        candidate = f'{base}_{wechat_userid}_{suffix}'[:150]
    return candidate


def get_or_create_user_from_wechat(wechat_userid, chinese_name, dept_ids=None, corp=None):
    """按企微身份获取或创建账号，返回 (user, created)。

    认人顺序 —— **必须先按身份、再按姓名**：

      ① 本企业已有该 userid 的身份 → 就是这个人
      ② 姓名拼音撞上已有账号     → 同一个人跨企业，归并进去
      ③ 都没有                   → 新建

    实测 B 企业 60 人里有 23 人（38%）和 A 企业是同一批人，靠 ①② 会归并到
    **同一个账号**，绝不新建第二个 —— `Task.assignee` / `Product.assignee` 只指向
    其中一个账号，建重了就会出现「同一个人的待办散在两个账号里」，这正是现网
    曾丽萍那个 bug 的翻版。

    注意别把 `profile.wechat_userid` 覆盖成当前企业的 userid：多企业的人在两边的
    userid 可能不同（如 阮仕云 A=Ruanivan / B=ivanRuan），覆盖会让另一边的消息发不出去。
    """
    from accounts.models import WeComIdentity
    from accounts.wecom import default_corp

    if corp is None:
        corp = default_corp()

    # ① 身份表是唯一真相源
    ident = (WeComIdentity.objects.filter(corp=corp, userid=wechat_userid)
             .select_related('user').first())
    if ident is not None:
        user, created = ident.user, False
        _touch_user(user, chinese_name)

    else:
        base = name_to_pinyin(chinese_name) or wechat_userid
        candidate = User.objects.filter(username__iexact=base).first()
        # 这个账号在本企业已经有别的企微身份了 → 是同名的另一个人，不能并
        if candidate is not None and WeComIdentity.objects.filter(
                corp=corp, user=candidate).exists():
            logger.warning('姓名 %s 撞上账号 %s，但该账号在本企业已有别的企微身份，'
                           '按另一个人处理', chinese_name, candidate.username)
            candidate = None

        if candidate is not None:
            user, created = candidate, False
            _touch_user(user, chinese_name)
            logger.info('企微身份按姓名归并: %s → 已有账号 %s', chinese_name, user.username)
        else:
            user = User.objects.create(
                username=_unique_username(base, wechat_userid),
                first_name=chinese_name,
                is_active=True,
            )
            user.set_password(DEFAULT_PASSWORD)
            user.save()
            created = True
            logger.info('企微身份新建账号: %s → %s', chinese_name, user.username)

        ident, made = WeComIdentity.objects.get_or_create(
            corp=corp, userid=wechat_userid,
            defaults={'user': user, 'is_primary': False},
        )
        if not made and ident.user_id != user.id:
            logger.warning('身份 %s@%s 已属于账号 %s，不改动',
                           wechat_userid, corp.code, ident.user_id)

    # 主身份：这个账号还没有主身份时，把当前这条设为主
    if not WeComIdentity.objects.filter(user=user, is_primary=True).exists():
        WeComIdentity.objects.filter(corp=corp, userid=wechat_userid).update(is_primary=True)
    sync_primary_cache(user)

    dept, conflict = decide_department(user, corp, dept_ids)
    if dept is not None:
        user.profile.department = dept
        user.profile.save(update_fields=['department'])
    elif conflict:
        current = user.profile.department.name if user.profile.department else '未设置'
        logger.info('部门冲突：%s 主身份在 %s（现为 %s），%s 通讯录说别处 —— '
                    '以主身份为准，要改请到 admin 手工指定',
                    user.first_name or user.username, conflict, current, corp.code)

    return user, created
