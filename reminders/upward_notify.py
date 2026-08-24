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
from .wechat import send_wechat_message

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

        wechat_userid = getattr(getattr(recipient, 'profile', None), 'wechat_userid', '')
        if not wechat_userid:
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
            success = send_wechat_message(wechat_userid, content)
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
