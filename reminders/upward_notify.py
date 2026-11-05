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


def _entity_product(entity):
    """拿到实体所属的 Product（Task → 阶段 → 品）"""
    from products.models import Task, ProductStage, Product

    if isinstance(entity, Task):
        return entity.product_stage.product
    if isinstance(entity, ProductStage):
        return entity.product
    if isinstance(entity, Product):
        return entity
    return None


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
        product = _entity_product(entity)
        # 暂停的项目不发任何通知：阶段/任务照常能改，但不再打扰任何人
        if product is not None and product.status == 'paused':
            return
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


def _overdue_task_names_by_stage(stage_ids):
    """{阶段 id: [超期任务名, ...]}，按阶段顺序、任务顺序排列。

    聚合消息除了「几个超期任务」，还要让收件人直接看到是哪几件事，
    不然还得点进系统逐个阶段找。
    """
    from products.models import Task

    stage_ids = list(stage_ids)
    if not stage_ids:
        return {}

    rows = Task.objects.filter(
        product_stage_id__in=stage_ids, status='overdue',
    ).order_by('product_stage__order', 'order').values_list(
        'product_stage_id', 'name',
    )

    result = {}
    for stage_id, task_name in rows:
        result.setdefault(stage_id, []).append(task_name)
    return result


def _task_names_text(task_names):
    return '、'.join(f'「{name}」' for name in task_names)


def _overdue_task_summary(product):
    """该品下「未完成阶段」里的超期情况。

    返回 (超期任务总数, [阶段明细, ...])，阶段明细按阶段顺序排列，每项是
    {'name': 阶段名, 'owner': 负责人姓名（没设则为「未指定」）, 'count': 该阶段超期任务数,
     'tasks': [超期任务名, ...]}。
    管理员聚合消息要能看出「哪些阶段超期了、该找谁、具体是哪几件事」。
    """
    from products.models import Task

    tasks = Task.objects.filter(
        product_stage__in=product.stages.exclude(status='completed'),
        status='overdue',
    ).select_related('product_stage__assignee').order_by(
        'product_stage__order', 'order', 'id',
    )

    stage_rows = []
    row_by_stage_id = {}
    for task in tasks:
        stage = task.product_stage
        row = row_by_stage_id.get(stage.id)
        if row is None:
            owner = stage.assignee
            row = {
                'name': stage.name,
                'owner': (owner.first_name or owner.username) if owner else '未指定',
                'count': 0,
                'tasks': [],
            }
            row_by_stage_id[stage.id] = row
            stage_rows.append(row)
        row['count'] += 1
        row['tasks'].append(task.name)

    return sum(row['count'] for row in stage_rows), stage_rows


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


def send_notice(recipient, situation, target_type, target_id, target_name,
                event_label='超期'):
    """给单个收件人发一条系统通知，成功返回 True。

    发不出去（没绑企微 / 企微报错 / 网络异常）都只影响这一个人，不向外抛。
    是否绑定企微交给 send_message_to_user 按主身份判断，这里不再看 profile
    字段 —— 多企业用户的主身份可能在 WeComIdentity 里。
    """
    recipient_name = recipient.first_name or recipient.username
    content = (
        f'{recipient_name}你好，我是项目管理智能机器人。\n'
        f'{situation}\n'
        f'点击查看：{settings.SITE_URL}'
    )

    try:
        success = send_message_to_user(recipient, content)
    except Exception:
        logger.exception('系统通知发送异常: recipient=%s', recipient.username)
        return False
    if not success:
        return False

    try:
        log_action(
            recipient, f'系统通知（{event_label}）',
            target_type, target_id, target_name, situation,
        )
    except Exception:
        logger.exception('系统通知写操作日志失败')
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
            count, stage_rows = _overdue_task_summary(product)
        except Exception:
            logger.exception('统计超期任务数失败: product=%s', product.pk)
            count, stage_rows = 0, []
        suffix = f'（{count} 个超期任务）' if count else ''
        lines.append(f'{idx}. 「{product.name}」{suffix}')
        if stage_rows:
            # 每个超期阶段带负责人和该阶段的超期任务数，缩进一行列出，
            # 管理员不用再点进去查
            stage_text = '、'.join(
                f'{row["name"]}（{row["owner"]}，{row["count"]} 个超期任务）'
                for row in stage_rows
            )
            lines.append(f'   超期阶段：{stage_text}')
            task_text = '；'.join(
                f'{row["name"]}：{_task_names_text(row["tasks"])}'
                for row in stage_rows if row['tasks']
            )
            if task_text:
                lines.append(f'   超期任务：{task_text}')

    situation = (
        f'你有 {len(products)} 个品存在超期任务，请关注：\n'
        + '\n'.join(lines)
    )
    summary = f'{len(products)} 个品超期'

    sent = 0
    for recipient in recipients:
        if send_notice(recipient, situation, 'product', products[0].pk, summary):
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
    tasks_by_stage = _overdue_task_names_by_stage(
        stage.pk for _, own_stages in groups for stage in own_stages
    )

    sent = 0
    for recipient, own_stages in groups:
        # 同一个品下的多个超期阶段合并成一行，负责人一眼能看出是哪个品出问题
        per_product = {}
        for stage in own_stages:
            per_product.setdefault(stage.product.name, []).append(stage)

        lines = [
            f'{idx}. 「{product_name}」：'
            + '、'.join(stage.name for stage in stage_list)
            for idx, (product_name, stage_list) in enumerate(per_product.items(), 1)
        ]
        situation = (
            f'你负责的 {len(per_product)} 个品存在超期阶段，请关注：\n'
            + '\n'.join(lines)
        )
        # 具体到超期任务名，收件人不用再点进去挨个阶段翻
        task_lines = []
        for stage in own_stages:
            task_names = tasks_by_stage.get(stage.pk, [])
            if task_names:
                task_lines.append(f'{stage.name}：{_task_names_text(task_names)}')
        if task_lines:
            situation += '\n超期任务：' + '；'.join(task_lines)
        summary = f'{len(per_product)} 个品存在超期阶段'

        if send_notice(recipient, situation, 'stage', own_stages[0].pk, summary):
            sent += 1
    return sent


def notify_stage_owners_overdue_upward(stages):
    """阶段负责人：自己负责的阶段有超期任务时，每天一条聚合提醒。

    收件人是 ProductStage.assignee（阶段负责人）。原先阶段超期只通知品总负责人，
    只负责某个阶段的人收不到任何提醒。同一个人既负责整个品又负责该阶段时，
    品级消息已经列出了这些阶段，这里跳过，避免一天收到两条重复消息。

    返回成功发送的收件人数。
    """
    stages = list(stages)
    if not stages:
        return 0

    eligible = [
        stage for stage in stages
        if stage.assignee_id and stage.assignee_id != stage.product.assignee_id
    ]
    groups = _group_by_recipient(eligible, lambda s: s.assignee)
    tasks_by_stage = _overdue_task_names_by_stage(
        stage.pk for _, own_stages in groups for stage in own_stages
    )

    sent = 0
    for recipient, own_stages in groups:
        lines = []
        for idx, stage in enumerate(own_stages, 1):
            count = stage.tasks.filter(status='overdue').count()
            task_names = tasks_by_stage.get(stage.pk, [])
            task_suffix = f'：{_task_names_text(task_names)}' if task_names else ''
            lines.append(
                f'{idx}. 「{stage.product.name}」·「{stage.name}」'
                f'（{count} 个超期任务）{task_suffix}'
            )

        situation = (
            f'你负责的 {len(own_stages)} 个阶段存在超期任务，请关注：\n'
            + '\n'.join(lines)
        )
        summary = f'{len(own_stages)} 个阶段存在超期任务'

        if send_notice(recipient, situation, 'stage', own_stages[0].pk, summary):
            sent += 1
    return sent


def _dedupe_recipients(candidates):
    """[(user, role)] 按账号去重保序；同一人有多重身份时取先出现的角色。"""
    seen = set()
    result = []
    for user, role in candidates:
        if user is None or user.id in seen:
            continue
        seen.add(user.id)
        result.append((user, role))
    return result


def notify_stages_due_upward(stages, today):
    """阶段到期前提醒：阶段负责人 + 上一级品负责人 + 管理员（向上两级）。

    只处理 expected_end_date 已落在 D-3 ~ D-1 的未完成阶段；
    已完成阶段在扫描阶段就被排除了，不会继续提醒。
    返回成功发送的消息条数。
    """
    stages = list(stages)
    if not stages:
        return 0

    try:
        admins = _admin_recipients()
    except Exception:
        logger.exception('notify_stages_due_upward 解析管理员失败')
        admins = []

    sent = 0
    for stage in stages:
        if not stage.expected_end_date:
            continue
        days_left = (stage.expected_end_date - today).days
        end_date = stage.expected_end_date.strftime('%Y-%m-%d')
        recipients = _dedupe_recipients(
            [(stage.assignee, 'stage'), (stage.product.assignee, 'product')]
            + [(admin, 'admin') for admin in admins]
        )

        for recipient, role in recipients:
            if role == 'stage':
                situation = (
                    f'你负责的「{stage.product.name}」项目「{stage.name}」阶段'
                    f'预计 {end_date} 结束，距离截止还有 {days_left} 天，请及时推进。'
                )
            elif role == 'product':
                situation = (
                    f'你负责的「{stage.product.name}」项目，「{stage.name}」阶段'
                    f'预计 {end_date} 结束，距离截止还有 {days_left} 天，请关注。'
                )
            else:
                situation = (
                    f'「{stage.product.name}」项目的「{stage.name}」阶段'
                    f'预计 {end_date} 结束，距离截止还有 {days_left} 天，请关注。'
                )
            if send_notice(recipient, situation, 'stage', stage.pk, stage.name, '临近'):
                sent += 1
    return sent


def notify_products_due_upward(products, today):
    """项目到期前提醒：品负责人 + 管理员（再往上一级）。

    只处理 expected_end_date 已落在 D-3 ~ D-1 的进行中项目。
    返回成功发送的消息条数。
    """
    products = list(products)
    if not products:
        return 0

    try:
        admins = _admin_recipients()
    except Exception:
        logger.exception('notify_products_due_upward 解析管理员失败')
        admins = []

    sent = 0
    for product in products:
        if not product.expected_end_date:
            continue
        days_left = (product.expected_end_date - today).days
        end_date = product.expected_end_date.strftime('%Y-%m-%d')
        recipients = _dedupe_recipients(
            [(product.assignee, 'product')]
            + [(admin, 'admin') for admin in admins]
        )
        owner_name = (
            (product.assignee.first_name or product.assignee.username)
            if product.assignee else '未指定'
        )

        for recipient, role in recipients:
            if role == 'product':
                situation = (
                    f'你负责的项目「{product.name}」预计 {end_date} 结束，'
                    f'距离截止还有 {days_left} 天，请及时安排收尾。'
                )
            else:
                situation = (
                    f'项目「{product.name}」（负责人：{owner_name}）预计 {end_date} 结束，'
                    f'距离截止还有 {days_left} 天，请关注。'
                )
            if send_notice(recipient, situation, 'product', product.pk, product.name, '临近'):
                sent += 1
    return sent


def notify_incomplete_tasks_upward(stage_tasks):
    """子任务必填信息缺失：每天提醒阶段负责人，直到执行人/开始/结束时间补齐。

    stage_tasks: [(stage, [task, ...]), ...]，只包含当天还没提醒过的阶段。
    同一阶段负责人负责多个阶段时聚合成一条，避免一天收好几条。
    返回成功发送的收件人数。
    """
    groups = {}
    for stage, tasks in stage_tasks:
        owner = stage.assignee
        if owner is None:
            continue
        groups.setdefault(owner.id, (owner, []))[1].append((stage, tasks))

    sent = 0
    for owner, items in groups.values():
        lines = []
        for stage, tasks in items:
            for task in tasks:
                missing = []
                if not task.assignee_id:
                    missing.append('执行人')
                if not task.started_at:
                    missing.append('开始时间')
                if not task.expected_end_date:
                    missing.append('结束时间')
                if not missing:
                    continue
                lines.append(
                    f'{len(lines) + 1}. 「{stage.product.name}」·「{stage.name}」'
                    f'·「{task.name}」：缺{"、".join(missing)}'
                )

        if not lines:
            continue

        situation = (
            f'你负责的阶段有 {len(lines)} 个子任务必填信息未填写，请补全：\n'
            + '\n'.join(lines)
        )
        first_stage = items[0][0]
        if send_notice(
            owner, situation, 'stage', first_stage.pk,
            f'{first_stage.product.name} · {first_stage.name}', '信息待补全',
        ):
            sent += 1
    return sent
