import logging
from datetime import timedelta
from django.conf import settings
from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone
from products.models import Task, ProductStage, Product
from activity_log.utils import log_action
from .models import ReminderLog, UpwardNotifyLog
from .upward_notify import (
    collect_product_due_items,
    collect_stage_due_items,
    notify_incomplete_tasks_upward,
    notify_products_overdue_upward,
    notify_stage_owners_overdue_upward,
    notify_stages_overdue_upward,
    send_upcoming_aggregate,
)
from .wechat import send_message_to_user

logger = logging.getLogger(__name__)


def _upcoming_task_recipients(task):
    """子任务到期前的收件人：执行人 + 阶段负责人 + 品负责人，按账号去重。

    收件人从近到远排列；同一个人兼任多个角色时只发一条，并按最近的
    角色措辞（执行人 > 阶段负责人 > 品负责人）。
    """
    stage = task.product_stage
    product = stage.product
    candidates = [
        (task.assignee, 'assignee'),
        (stage.assignee, 'stage'),
        (product.assignee, 'product'),
    ]
    seen = set()
    result = []
    for user, role in candidates:
        if user is None or user.id in seen:
            continue
        seen.add(user.id)
        result.append((user, role))
    return result


def scan_and_remind(mode='all'):
    """
    扫描所有进行中品下的未完成任务：
    - 截止日期在3天内 → 临近提醒
    - 截止日期已过 → 超期提醒 + 标记延期
    每个 Task 每种类型每天只发一次。

    mode 用来把两类提醒拆到不同时间发：
    - 'advance'：只发到期前提醒（子任务、阶段、品的 D-3~D-1），工作日 14:30
    - 'other'：只发超期提醒、信息缺失提醒和超期汇总，每日 09:00
    - 'all'：全部（手动执行 / 测试）
    """
    if mode not in ('all', 'advance', 'other'):
        raise ValueError(f'未知的提醒扫描模式: {mode}')
    do_advance = mode in ('all', 'advance')
    do_other = mode in ('all', 'other')

    today = timezone.localtime(timezone.now()).date()
    # 本地时区的今日起止，Django 会在查询时自动转为 UTC，避免依赖 MySQL CONVERT_TZ
    today_start = timezone.localtime(timezone.now()).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_end = today_start + timedelta(days=1)
    due_window_start = today + timedelta(days=1)
    due_window_end = today + timedelta(days=3)

    # 只查进行中品下的任务
    tasks = Task.objects.filter(
        product_stage__product__status='active',
        product_stage__status='in_progress',
    ).exclude(
        status='completed'
    ).select_related(
        'product_stage__product__assignee',
        'product_stage__assignee',
        'assignee__profile',
    ).prefetch_related('depends_on')

    sent_count = 0
    # 到期前提醒先按「人」收集，扫描末尾同一个人只发一条
    upcoming_items = []
    upcoming_task_recipients = {}
    # 超期提醒也按执行人收集：一个人名下多件超期任务合并成一条，
    # 不再一件一条轰炸；任务级 ReminderLog 仍然逐件写，保住按天的去重
    overdue_groups = {}
    for task in tasks:
        # 先根据时间自动更新状态
        task.update_status()

        # 前置任务没完成的任务还不能开始，不发到期/超期提醒
        if task.is_blocked:
            continue

        # 确定用于提醒的截止日期（优先用预计结束日期）
        end_date = task.expected_end_date or task.deadline
        if not end_date:
            continue

        days_left = (end_date - today).days
        if days_left < 0:
            reminder_type = 'overdue'
        elif 1 <= days_left <= 3:
            reminder_type = 'upcoming'
        else:
            continue

        # 去重：当天（本地时区）是否已发送同类提醒
        already_sent = ReminderLog.objects.filter(
            task=task,
            reminder_type=reminder_type,
            sent_at__gte=today_start,
            sent_at__lt=today_end,
        ).exists()
        if already_sent:
            continue

        stage = task.product_stage
        product_name = stage.product.name
        stage_name = stage.name
        end_date_str = end_date.strftime('%Y-%m-%d')

        if reminder_type == 'overdue':
            if not do_other:
                continue
            # 超期只发执行人；阶段/品负责人由后面的每日超期汇总覆盖。
            # 这里先按执行人收集，循环结束后每人合并成一条。
            if not task.assignee:
                continue
            overdue_groups.setdefault(
                task.assignee_id, (task.assignee, []),
            )[1].append((task, product_name, stage_name, end_date_str, days_left))
            continue

        if not do_advance:
            continue

        # 到期前：执行人 + 阶段负责人 + 品负责人（向上两级）。
        # 这里只收集，末尾统一发送；同一个人当天多条会合并成一条。
        line = (
            f'“{product_name}”·“{stage_name}”·“{task.name}”子任务：'
            f'预计 {end_date_str} 完成，距离截止还有 {days_left} 天。'
        )
        for recipient, _role in _upcoming_task_recipients(task):
            upcoming_items.append({
                'recipient': recipient,
                'line': line,
                'days_left': days_left,
                'target_type': 'task',
                'target_id': task.id,
                'target_name': task.name,
            })
            upcoming_task_recipients.setdefault(task.id, set()).add(recipient.id)

    # ---- 执行人超期提醒：同一人当天多件任务合并成一条 ----
    # 合并只在「本人名下有 2 件及以上」时生效；只有一件时保留原来的单条措辞。
    for assignee, items in overdue_groups.values():
        receiver_name = assignee.first_name or assignee.username
        if len(items) == 1:
            task, product_name, stage_name, end_date_str, days_left = items[0]
            situation = (
                f'你负责的“{product_name}”项目，在“{stage_name}”阶段的'
                f'“{task.name}”事项，预计 {end_date_str} 完成，'
                f'但已超期 {abs(days_left)} 天，请尽快处理！'
            )
        else:
            lines = [
                f'{idx}. “{product_name}”·“{stage_name}”·“{task.name}”：'
                f'预计 {end_date_str} 完成，已超期 {abs(days_left)} 天'
                for idx, (task, product_name, stage_name, end_date_str, days_left)
                in enumerate(items, 1)
            ]
            situation = (
                f'你有 {len(items)} 个事项已超期，请尽快处理：\n'
                + '\n'.join(lines)
            )

        content = (
            '您好，我是项目管理智能机器人。\n'
            f'{situation}\n'
            f'点击查看：{settings.SITE_URL}'
        )
        try:
            success = send_message_to_user(assignee, content)
        except Exception:
            logger.exception('超期提醒发送异常: assignee=%s', assignee.username)
            success = False
        if not success:
            continue

        for task, product_name, stage_name, end_date_str, days_left in items:
            ReminderLog.objects.create(task=task, reminder_type='overdue')
            task.mark_overdue()
            sent_count += 1
            log_action(None, '系统提醒', 'task', task.id,
                       f'致 {receiver_name} · {task.name}', '超期提醒')
        logger.info(
            '超期提醒已发送: %s 件任务 -> %s', len(items), assignee.username,
        )

    # ---- 子任务必填信息缺失：每天提醒阶段负责人，直到补齐 ----
    # 只管进行中阶段里的未完成任务：后续阶段的字段还没到填的时候，
    # 天天催只会变成噪音；完成后也不再催。
    # 阶段还没到开始时间也不催：阶段没开始，执行人/时间本来就不该现在填。
    incomplete_tasks = Task.objects.none()
    if do_other:
        incomplete_tasks = Task.objects.filter(
            product_stage__product__status='active',
            product_stage__status='in_progress',
            product_stage__started_at__isnull=False,
            product_stage__started_at__lte=timezone.now(),
        ).exclude(status='completed').exclude(
            # 立项阶段不再强制要求填执行人/开始/结束时间，也不再发信息缺失提醒
            product_stage__name='立项',
        ).filter(
            Q(assignee__isnull=True)
            | Q(started_at__isnull=True)
            | Q(expected_end_date__isnull=True)
        ).select_related(
            'product_stage__product', 'product_stage__assignee',
        ).prefetch_related('depends_on').order_by('product_stage__order', 'order', 'id')

    stage_tasks = {}
    stage_map = {}
    for task in incomplete_tasks:
        # 前置任务没完成时，开始时间本来就不该填，催阶段负责人没有意义
        if task.is_blocked:
            continue
        stage = task.product_stage
        stage_map[stage.pk] = stage
        stage_tasks.setdefault(stage.pk, []).append(task)

    stages_to_notify = []
    for stage_pk, tasks in stage_tasks.items():
        stage = stage_map[stage_pk]
        already_sent = UpwardNotifyLog.objects.filter(
            content_type_label='stage', object_id=stage.pk,
            event_type='incomplete', sent_date=today,
        ).exists()
        if already_sent:
            continue
        try:
            UpwardNotifyLog.objects.create(
                content_type_label='stage', object_id=stage.pk,
                event_type='incomplete', sent_date=today,
            )
        except IntegrityError:
            continue
        stages_to_notify.append((stage, tasks))

    if stages_to_notify:
        try:
            notify_incomplete_tasks_upward(stages_to_notify)
        except Exception:
            logger.exception('子任务信息缺失提醒失败')

    # ---- 阶段/品到期前提醒（D-3 ~ D-1）：向上两级，按天去重 ----
    due_stages = []
    stage_due_query = ProductStage.objects.none()
    if do_advance:
        stage_due_query = ProductStage.objects.filter(
            product__status='active',
            expected_end_date__gte=due_window_start,
            expected_end_date__lte=due_window_end,
        ).exclude(status='completed').select_related(
            'product__assignee', 'assignee',
        )
    for stage in stage_due_query:
        already_sent = UpwardNotifyLog.objects.filter(
            content_type_label='stage', object_id=stage.pk,
            event_type='upcoming', sent_date=today,
        ).exists()
        if already_sent:
            continue
        try:
            UpwardNotifyLog.objects.create(
                content_type_label='stage', object_id=stage.pk,
                event_type='upcoming', sent_date=today,
            )
        except IntegrityError:
            continue
        due_stages.append(stage)

    if due_stages:
        try:
            upcoming_items.extend(collect_stage_due_items(due_stages, today))
        except Exception:
            logger.exception('阶段到期前通知收集失败')

    due_products = []
    product_due_query = Product.objects.none()
    if do_advance:
        product_due_query = Product.objects.filter(
            status='active',
            expected_end_date__gte=due_window_start,
            expected_end_date__lte=due_window_end,
        ).select_related('assignee')
    for product in product_due_query:
        already_sent = UpwardNotifyLog.objects.filter(
            content_type_label='product', object_id=product.pk,
            event_type='upcoming', sent_date=today,
        ).exists()
        if already_sent:
            continue
        try:
            UpwardNotifyLog.objects.create(
                content_type_label='product', object_id=product.pk,
                event_type='upcoming', sent_date=today,
            )
        except IntegrityError:
            continue
        due_products.append(product)

    if due_products:
        try:
            upcoming_items.extend(collect_product_due_items(due_products, today))
        except Exception:
            logger.exception('项目到期前通知收集失败')

    # ---- 到期前提醒统一发送：同一个人多条合并成一条 ----
    if do_advance and upcoming_items:
        try:
            sent_recipients = send_upcoming_aggregate(upcoming_items)
        except Exception:
            logger.exception('到期前合并提醒发送失败')
            sent_recipients = set()

        # 任务级去重：只要该任务有任一收件人成功收到，当天就不再发
        for task_id, recipient_ids in upcoming_task_recipients.items():
            if recipient_ids & sent_recipients:
                ReminderLog.objects.create(
                    task_id=task_id, reminder_type='upcoming',
                )
                sent_count += 1

    logger.info(f'定时提醒扫描完成，共发送 {sent_count} 条提醒')

    # ---- 阶段/品超期聚合检测：通知上一级负责人，按天去重 ----

    # 阶段超期：未完成阶段下存在超期任务 → 通知品总负责人 + 该阶段负责人
    #
    # 同样是先收集再按负责人聚合发送。品总负责人往往同时管好几个品、
    # 每个品又可能有好几个阶段超期，逐条发就是一天七八条几乎一样的消息。
    newly_overdue_stages = []
    stage_overdue_query = ProductStage.objects.none()
    if do_other:
        stage_overdue_query = ProductStage.objects.exclude(
            status='completed'
        ).exclude(product__status='paused').select_related(
            'product', 'assignee'
        )
    for stage in stage_overdue_query:
        has_overdue_task = stage.tasks.filter(status='overdue').exists()
        if not has_overdue_task:
            continue
        already_sent = UpwardNotifyLog.objects.filter(
            content_type_label='stage', object_id=stage.pk,
            event_type='overdue', sent_date=today,
        ).exists()
        if already_sent:
            continue
        newly_overdue_stages.append(stage)
        try:
            UpwardNotifyLog.objects.create(
                content_type_label='stage', object_id=stage.pk,
                event_type='overdue', sent_date=today,
            )
        except IntegrityError:
            newly_overdue_stages.pop()
            continue

    if newly_overdue_stages:
        try:
            notify_stages_overdue_upward(newly_overdue_stages)
        except Exception:
            logger.exception('阶段超期聚合通知失败')
        try:
            notify_stage_owners_overdue_upward(newly_overdue_stages)
        except Exception:
            logger.exception('阶段负责人超期聚合通知失败')

    # 品超期：未完成/未取消的品下存在超期阶段（未完成阶段含超期任务） → 通知所有管理员
    #
    # 这里**先收集再一次性发送**：原先是逐品调用 notify_upward(product, ...)，
    # 每个管理员每个超期品各收一条，6 个品超期就是 6 条几乎一样的消息、且每天重复。
    # 改成把当天新超期的品聚合成一条「你有 N 个品超期」。
    #
    # UpwardNotifyLog 仍然**逐品**写：它是按 (label, object_id, event_type, date) 去重的，
    # 逐品写才能保证「今天已经通知过的品」明天不会再被算进来。
    newly_overdue = []
    product_overdue_query = Product.objects.none()
    if do_other:
        product_overdue_query = Product.objects.exclude(
            status__in=['completed', 'cancelled', 'paused']
        )
    for product in product_overdue_query:
        has_overdue_stage = product.stages.exclude(status='completed').filter(
            tasks__status='overdue'
        ).exists()
        if not has_overdue_stage:
            continue
        already_sent = UpwardNotifyLog.objects.filter(
            content_type_label='product', object_id=product.pk,
            event_type='overdue', sent_date=today,
        ).exists()
        if already_sent:
            continue
        newly_overdue.append(product)
        try:
            UpwardNotifyLog.objects.create(
                content_type_label='product', object_id=product.pk,
                event_type='overdue', sent_date=today,
            )
        except IntegrityError:
            # 并发下已有兄弟进程写入 —— 但对方也是同一个计时器触发的，
            # 把它从本次聚合里去掉，避免同一条消息里重复列出同一个品
            newly_overdue.pop()
            continue

    if newly_overdue:
        try:
            notify_products_overdue_upward(newly_overdue)
        except Exception:
            logger.exception('品超期聚合通知失败')

    return sent_count


# 调度器已移出进程：改由 systemd timer 触发 `manage.py scan_reminders`
# 和 `manage.py sync_wechat_org`。原因见 reminders/apps.py 顶部注释。
# 不要再往这里加 APScheduler —— gunicorn --workers 3 会让每个 worker 各跑一份。
