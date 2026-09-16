"""状态变更时通知上一级负责人的核心逻辑。

层级关系：Task -> ProductStage.assignee（阶段负责人）
          ProductStage -> Product.assignee（品总负责人）
          Product -> 所有 is_admin=True 的用户

失败静默：找不到上级负责人、上级未绑定企微、发送异常，都不影响调用方的主流程。
"""
import logging

from django.conf import settings
from django.contrib.auth.models import User

from activity_log.utils import log_action
from .wechat import send_message_to_user

logger = logging.getLogger(__name__)

_EVENT_LABEL = {'completed': '完成', 'overdue': '超期'}

# activity_log 里对各实体统一使用的 target_type 取值（见 products/views.py 里的 log_action 调用）
_TARGET_TYPE_MAP = {'Task': 'task', 'ProductStage': 'stage', 'Product': 'product'}


def _resolve_recipients_and_context(entity):
    """返回 (收件人列表, 品名, 阶段名或None, 任务名或None)"""
    from products.models import Task, ProductStage, Product

    if isinstance(entity, Task):
        stage = entity.product_stage
        product = stage.product
        recipients = [stage.assignee] if stage.assignee else []
        return recipients, product.name, stage.name, entity.name

    if isinstance(entity, ProductStage):
        product = entity.product
        recipients = [product.assignee] if product.assignee else []
        return recipients, product.name, entity.name, None

    if isinstance(entity, Product):
        admins = [u for u in User.objects.select_related('profile') if u.profile.is_admin]
        return admins, entity.name, None, None

    return [], '', None, None


def notify_upward(entity, event_type, actor=None):
    """entity: Task / ProductStage / Product 实例。
    event_type: 'completed' 或 'overdue'。
    actor: 触发本次状态变更的操作人（User 或 None）；若上级负责人恰好是 actor 本人则跳过通知。
    """
    try:
        recipients, product_name, stage_name, task_name = _resolve_recipients_and_context(entity)
    except Exception:
        logger.exception('notify_upward 解析收件人失败: entity=%r', entity)
        return

    if not recipients:
        return

    event_label = _EVENT_LABEL.get(event_type, event_type)

    for recipient in recipients:
        if recipient is None:
            continue
        if actor is not None and recipient.id == actor.id:
            continue

        if not getattr(getattr(recipient, 'profile', None), 'wechat_userid', ''):
            continue

        recipient_name = recipient.first_name or recipient.username

        if task_name:
            situation = f'你负责的「{product_name}」项目，「{stage_name}」阶段的「{task_name}」已{event_label}，请关注。'
        elif stage_name:
            situation = f'你负责的「{product_name}」项目，「{stage_name}」阶段已{event_label}，请关注。'
        else:
            situation = f'你负责的「{product_name}」项目已{event_label}，请关注。'

        content = (
            f'{recipient_name}你好，我是项目管理智能机器人。\n'
            f'{situation}\n'
            f'点击查看：{settings.SITE_URL}'
        )

        try:
            success = send_message_to_user(recipient, content)
        except Exception:
            logger.exception('notify_upward 发送企微消息异常: recipient=%s', recipient.username)
            continue

        if not success:
            continue

        try:
            entity_class_name = entity.__class__.__name__
            target_type = _TARGET_TYPE_MAP.get(entity_class_name, entity_class_name.lower())
            log_action(
                recipient, f'系统通知（{event_label}）',
                target_type, entity.pk,
                product_name, situation,
            )
        except Exception:
            logger.exception('notify_upward 写操作日志失败')


# ---------------------------------------------------------------------------
# 聚合通知
#
# 扫描器原先对每个超期实体各调一次 notify_upward()，于是「6 个品超期 + 7 个阶段
# 超期」会让一个管理员一天收到 13 条几乎一样的消息，而且每天重复到超期被处理为止。
# 下面这组函数把同一天新超期的实体按收件人聚合成**一条**消息。
#
# 注意：只用于 scheduler 的每日扫描。products/models.py 里状态变更时的同步通知
# 仍走 notify_upward()，那是单实体事件，不需要聚合。
# ---------------------------------------------------------------------------


def _overdue_task_count(product):
    """该品下「未完成阶段」里的超期任务数，用于聚合消息里给出量级。"""
    from products.models import Task

    return Task.objects.filter(
        product_stage__in=product.stages.exclude(status='completed'),
        status='overdue',
    ).count()


def _admin_recipients():
    """所有管理员，按账号去重。

    按账号去重而不是按姓名：姓名不是可靠的去重键 —— 现网那组同名账号
    （曾丽萍，两个账号绑的是同一个 userid）本来就是同一个人被建重了，
    但姓名相同也确实可能是两个不同的人，两个方向都会错。
    同一个人的多企业身份由 WeComIdentity 承载（一个账号挂多条身份），
    账号 id 本身就是去重键，不需要姓名启发式。
    """
    seen = set()
    result = []
    for user in User.objects.select_related('profile'):
        if user.id in seen or not user.profile.is_admin:
            continue
        seen.add(user.id)
        result.append(user)
    return result


def _group_by_recipient(items, recipient_of):
    """按收件人分组，保持首次出现的顺序。recipient_of(item) -> User 或 None。"""
    groups = {}
    for item in items:
        recipient = recipient_of(item)
        if recipient is None:
            continue
        groups.setdefault(recipient.id, (recipient, []))[1].append(item)
    return list(groups.values())


def _send_aggregate(recipient, situation, target_type, target_id, target_name):
    """给单个收件人发一条聚合消息，成功返回 True。

    发不出去（没绑企微 / 企微报错 / 网络异常）都只影响这一个人，不向外抛。
    """
    if not getattr(getattr(recipient, 'profile', None), 'wechat_userid', ''):
        return False

    recipient_name = recipient.first_name or recipient.username
    content = (
        f'{recipient_name}你好，我是项目管理智能机器人。\n'
        f'{situation}\n'
        f'点击查看：{settings.SITE_URL}'
    )

    try:
        success = send_message_to_user(recipient, content)
    except Exception:
        logger.exception('聚合通知发送异常: recipient=%s', recipient.username)
        return False
    if not success:
        return False

    try:
        log_action(recipient, '系统通知（超期）', target_type, target_id, target_name, situation)
    except Exception:
        logger.exception('聚合通知写操作日志失败')
    return True


def notify_products_overdue_upward(products):
    """多个品同时超期时，聚合成**一条**消息发给每个管理员。

    返回成功发送的收件人数。
    """
    products = list(products)
    if not products:
        return 0

    try:
        recipients = _admin_recipients()
    except Exception:
        logger.exception('notify_products_overdue_upward 解析管理员失败')
        return 0

    lines = []
    for idx, product in enumerate(products, 1):
        try:
            count = _overdue_task_count(product)
        except Exception:
            logger.exception('统计超期任务数失败: product=%s', product.pk)
            count = 0
        suffix = f'（{count} 个超期任务）' if count else ''
        lines.append(f'{idx}. 「{product.name}」{suffix}')

    situation = (
        f'你有 {len(products)} 个品存在超期任务，请关注：\n'
        + '\n'.join(lines)
    )
    summary = f'{len(products)} 个品超期'

    sent = 0
    for recipient in recipients:
        if _send_aggregate(recipient, situation, 'product', products[0].pk, summary):
            sent += 1
    return sent


def notify_stages_overdue_upward(stages):
    """多个阶段超期时，按品负责人聚合成**一条**消息。

    收件人是各阶段的品总负责人（ProductStage 的上级），一个人可能同时负责
    好几个品、每个品又有好几个阶段超期 —— 这正是要聚合的场景。

    返回成功发送的收件人数。
    """
    stages = list(stages)
    if not stages:
        return 0

    groups = _group_by_recipient(stages, lambda s: s.product.assignee)

    sent = 0
    for recipient, own_stages in groups:
        # 同一个品下的多个超期阶段合并成一行，负责人一眼能看出是哪个品出问题
        per_product = {}
        for stage in own_stages:
            per_product.setdefault(stage.product.name, []).append(stage.name)

        lines = [
            f'{idx}. 「{product_name}」：{"、".join(stage_names)}'
            for idx, (product_name, stage_names) in enumerate(per_product.items(), 1)
        ]
        situation = (
            f'你负责的 {len(per_product)} 个品存在超期阶段，请关注：\n'
            + '\n'.join(lines)
        )
        summary = f'{len(per_product)} 个品存在超期阶段'

        if _send_aggregate(recipient, situation, 'stage', own_stages[0].pk, summary):
            sent += 1
    return sent
