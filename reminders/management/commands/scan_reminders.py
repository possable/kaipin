"""每日提醒扫描，由 systemd timer 触发（见 deploy/kaipin-remind.timer）。

以前这个扫描是挂在 reminders/apps.py 里用进程内 APScheduler 跑的，
但那个启动守卫（RUN_MAIN/WERKZEUG_RUN_MAIN）只有 runserver 会满足，
gunicorn 不设这两个变量，所以生产环境从来没执行过。
现在改成外部 timer + management command，天然单次执行，
也不会像进程内调度器那样在 3 个 gunicorn worker 里各跑一遍。
"""
from django.core.management.base import BaseCommand

from reminders.scheduler import scan_and_remind


class Command(BaseCommand):
    help = '扫描进行中项目的任务，发送临近/超期提醒并做阶段、品的超期聚合通知'

    def handle(self, *args, **options):
        sent = scan_and_remind()
        self.stdout.write(self.style.SUCCESS(f'提醒扫描完成，发送 {sent} 条任务提醒'))
