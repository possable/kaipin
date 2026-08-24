from django.apps import AppConfig


class ActivityLogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'activity_log'
    verbose_name = '操作日志'
