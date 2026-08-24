from django.apps import AppConfig


class RemindersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'reminders'

    def ready(self):
        # 确保只在主进程中启动调度器（避免 runserver reload 时重复启动）
        import os
        if os.environ.get('RUN_MAIN') == 'true' or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
            from .scheduler import start_scheduler
            start_scheduler()
