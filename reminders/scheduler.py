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
    notify_incomplete_tasks_upward,
    notify_products_due_upward,
    notify_products_overdue_upward,
    notify_stage_owners_overdue_upward,
    notify_stages_due_upward,
    notify_stages_overdue_upward,
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


def scan_and_remind():
    """
    扫描所有进行中品下的未完成任务：
    - 截止日期在3天内 → 临近提醒
    - 截止日期已过 → 超期提醒 + 标记延期
    每个 Task 每种类型每天只发一次。
    """
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
    )

    sent_count = 0
    for task in tasks:
        # 先根据时间自动更新状态
        task.update_status()

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
            # 超期单任务只发执行人；阶段/品负责人由后面的每日超期汇总覆盖
            if not task.assignee:
                continue
            receiver_name = task.assignee.first_name or task.assignee.username
            situation = (
                f'你负责的「{product_name}」项目，在「{stage_name}」阶段的'
                f'「{task.name}」事项，预计 {end_date_str} 完成，'
                f'但已超期 {abs(days_left)} 天，请尽快处理！'
            )
            content = (
                f'{receiver_name}你好，我是项目管理智能机器人。\n'
                f'{situation}\n'
                f'点击查看：{settings.SITE_URL}'
            )
            try:
                success = send_message_to_user(task.assignee, content)
            except Exception:
                logger.exception('超期提醒发送异常: task=%s', task.pk)
                success = False
            if success:
                ReminderLog.objects.create(task=task, reminder_type=reminder_type)
                task.mark_overdue()
                sent_count += 1
                log_action(None, '系统提醒', 'task', task.id,
                           f'致 {receiver_name} · {task.name}', '超期提醒')
                logger.info('超期提醒已发送: %s -> %s', task.name, task.assignee.username)
            continue

        # 到期前：执行人 + 阶段负责人 + 品负责人（向上两级）
        sent_any = False
        for recipient, role in _upcoming_task_recipients(task):
            recipient_name = recipient.first_name or recipient.username
            if role == 'assignee':
                situation = (
                    f'你负责的「{product_name}」项目，在「{stage_name}」阶段的'
                    f'「{task.name}」事项，预计 {end_date_str} 完成，'
                    f'距离截止还有 {days_left} 天，请及时处理。'
                )
            elif role == 'stage':
                situation = (
                    f'你负责的「{product_name}」项目「{stage_name}」阶段，'
                    f'其下「{task.name}」事项预计 {end_date_str} 完成，'
                    f'距离截止还有 {days_left} 天，请及时推进。'
                )
            else:
                situation = (
                    f'你负责的「{product_name}」项目，「{stage_name}」阶段的'
                    f'「{task.name}」事项预计 {end_date_str} 完成，'
                    f'距离截止还有 {days_left} 天，请关注。'
                )
            content = (
                f'{recipient_name}你好，我是项目管理智能机器人。\n'
                f'{situation}\n'
                f'点击查看：{settings.SITE_URL}'
            )
            try:
                success = send_message_to_user(recipient, content)
            except Exception:
                logger.exception('临近提醒发送异常: task=%s recipient=%s',
                                 task.pk, recipient.username)
                success = False
            if success:
                sent_any = True
                log_action(None, '系统提醒', 'task', task.id,
                           f'致 {recipient_name} · {task.name}', '临近提醒')
                logger.info('临近提醒已发送: %s -> %s', task.name, recipient.username)

        if sent_any:
            ReminderLog.objects.create(task=task, reminder_type='upcoming')
            sent_count += 1

    logger.info(f'定时提醒扫描完成，共发送 {sent_count} 条提醒')

    # ---- 子任务必填信息缺失：每天提醒阶段负责人，直到补齐 ----
    # 只管进行中阶段里的未完成任务：后续阶段的字段还没到填的时候，
    # 天天催只会变成噪音；完成后也不再催。
    incomplete_tasks = Task.objects.filter(
        product_stage__product__status='active',
        product_stage__status='in_progress',
    ).exclude(status='completed').filter(
        Q(assignee__isnull=True)
        | Q(started_at__isnull=True)
        | Q(expected_end_date__isnull=True)
    ).select_related(
        'product_stage__product', 'product_stage__assignee',
    ).order_by('product_stage__order', 'order', 'id')

    stage_tasks = {}
    stage_map = {}
    for task in incomplete_tasks:
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
    for stage in ProductStage.objects.filter(
        product__status='active',
        expected_end_date__gte=due_window_start,
        expected_end_date__lte=due_window_end,
    ).exclude(status='completed').select_related(
        'product__assignee', 'assignee',
    ):
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
            notify_stages_due_upward(due_stages, today)
        except Exception:
            logger.exception('阶段到期前通知失败')

    due_products = []
    for product in Product.objects.filter(
        status='active',
        expected_end_date__gte=due_window_start,
        expected_end_date__lte=due_window_end,
    ).select_related('assignee'):
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
            notify_products_due_upward(due_products, today)
        except Exception:
            logger.exception('项目到期前通知失败')

    # ---- 阶段/品超期聚合检测：通知上一级负责人，按天去重 ----

    # 阶段超期：未完成阶段下存在超期任务 → 通知品总负责人 + 该阶段负责人
    #
    # 同样是先收集再按负责人聚合发送。品总负责人往往同时管好几个品、
    # 每个品又可能有好几个阶段超期，逐条发就是一天七八条几乎一样的消息。
    newly_overdue_stages = []
    for stage in ProductStage.objects.exclude(
        status='completed'
    ).exclude(product__status='paused').select_related(
        'product', 'assignee'
    ):
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
    for product in Product.objects.exclude(status__in=['completed', 'cancelled', 'paused']):
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
