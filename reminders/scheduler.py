import logging
from datetime import timedelta
from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from products.models import Task, ProductStage, Product
from activity_log.utils import log_action
from .models import ReminderLog, UpwardNotifyLog
from .upward_notify import notify_upward
from .wechat import send_wechat_message

logger = logging.getLogger(__name__)


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
    upcoming_threshold = today + timedelta(days=3)

    # 只查进行中品下的任务
    tasks = Task.objects.filter(
        product_stage__product__status='active',
        product_stage__status='in_progress',
    ).exclude(
        status='completed'
    ).select_related(
        'product_stage__product', 'assignee__profile'
    )

    sent_count = 0
    for task in tasks:
        # 先根据时间自动更新状态
        task.update_status()

        # 确定用于提醒的截止日期（优先用预计结束日期）
        end_date = task.expected_end_date or task.deadline
        if not end_date:
            continue
        if not task.assignee:
            continue

        wechat_id = task.assignee.profile.wechat_userid
        if not wechat_id:
            continue

        reminder_type = None
        if end_date < today:
            reminder_type = 'overdue'
        elif end_date <= upcoming_threshold:
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

        product_name = task.product_stage.product.name
        stage_name = task.product_stage.name
        end_date_str = end_date.strftime('%Y-%m-%d')

        assignee_name = task.assignee.first_name or task.assignee.username
        greeting = f'{assignee_name}你好，我是项目管理智能机器人。'
        situation = f'你负责的「{product_name}」项目，在「{stage_name}」阶段的「{task.name}」事项，'

        if reminder_type == 'overdue':
            content = (
                f'{greeting}\n{situation}'
                f'预计 {end_date_str} 完成，但已超期 {(today - end_date).days} 天，请尽快处理！\n'
                f'点击查看：{settings.SITE_URL}'
            )
        else:
            content = (
                f'{greeting}\n{situation}'
                f'预计 {end_date_str} 完成，距离截止还有 {(end_date - today).days} 天，请及时处理。\n'
                f'点击查看：{settings.SITE_URL}'
            )

        success = send_wechat_message(wechat_id, content)
        if success:
            ReminderLog.objects.create(task=task, reminder_type=reminder_type)
            if reminder_type == 'overdue':
                task.mark_overdue()
            sent_count += 1
            receiver_name = task.assignee.first_name or task.assignee.username
            log_action(None, '系统提醒', 'task', task.id,
                       f'致 {receiver_name} · {task.name}',
                       f'{"超期提醒" if reminder_type == "overdue" else "临近提醒"}')
            logger.info(f'提醒已发送: {task.name} -> {task.assignee.username}')

    logger.info(f'定时提醒扫描完成，共发送 {sent_count} 条提醒')

    # ---- 阶段/品超期聚合检测：通知上一级负责人，按天去重 ----

    # 阶段超期：未完成阶段下存在超期任务 → 通知品总负责人
    for stage in ProductStage.objects.exclude(status='completed').select_related('product'):
        has_overdue_task = stage.tasks.filter(status='overdue').exists()
        if not has_overdue_task:
            continue
        already_sent = UpwardNotifyLog.objects.filter(
            content_type_label='stage', object_id=stage.pk,
            event_type='overdue', sent_date=today,
        ).exists()
        if already_sent:
            continue
        notify_upward(stage, 'overdue')
        try:
            UpwardNotifyLog.objects.create(
                content_type_label='stage', object_id=stage.pk,
                event_type='overdue', sent_date=today,
            )
        except IntegrityError:
            pass  # 并发下已有兄弟进程写入，忽略

    # 品超期：未完成/未取消的品下存在超期阶段（未完成阶段含超期任务） → 通知所有管理员
    for product in Product.objects.exclude(status__in=['completed', 'cancelled']):
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
        notify_upward(product, 'overdue')
        try:
            UpwardNotifyLog.objects.create(
                content_type_label='product', object_id=product.pk,
                event_type='overdue', sent_date=today,
            )
        except IntegrityError:
            pass  # 并发下已有兄弟进程写入，忽略

    return sent_count


from django.core.management import call_command
from apscheduler.schedulers.background import BackgroundScheduler

_scheduler = None


def start_scheduler():
    """在 Django 应用就绪时调用，启动后台调度器"""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        lambda: call_command('sync_wechat_org'),
        'cron',
        hour=8,
        minute=0,
        id='daily_sync_wechat',
        replace_existing=True,
    )
    _scheduler.add_job(
        scan_and_remind,
        'cron',
        hour=9,
        minute=0,
        id='daily_reminder',
        replace_existing=True,
    )
    _scheduler.start()
    logger.info('APScheduler 已启动，每日 08:00 同步组织架构，09:00 执行提醒扫描')
