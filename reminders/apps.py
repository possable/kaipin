from django.apps import AppConfig


class RemindersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'reminders'

    # 定时任务不在这里启动。
    #
    # 原实现在 ready() 里判断 RUN_MAIN/WERKZEUG_RUN_MAIN 后启动进程内 APScheduler，
    # 但这两个变量只有 runserver 会设，gunicorn 不设 —— 生产环境（systemd + gunicorn）
    # 下守卫永远为假，每日组织同步和提醒扫描从未执行过。
    #
    # 现在改为 systemd timer 触发 management command：
    #   每日 07:50  manage.py sync_wechat_org
    #   每日 09:00  manage.py scan_reminders
    # 见 deploy/kaipin-sync.timer、deploy/kaipin-remind.timer。
    #
    # 不要改回进程内调度器：gunicorn 配置了 --workers 3，三个 worker 是独立进程，
    # 每个都会启动一份调度器，同一时刻重复执行三次。
