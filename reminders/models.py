from django.db import models
from products.models import Task


class ReminderLog(models.Model):
    """提醒记录，用于去重：同一个 Task 同类型提醒每天只发一次"""
    REMINDER_TYPES = [
        ('upcoming', '临近'),
        ('overdue', '超期'),
    ]
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, verbose_name='任务'
    )
    reminder_type = models.CharField(
        max_length=10, choices=REMINDER_TYPES, verbose_name='提醒类型'
    )
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name='发送时间')

    class Meta:
        verbose_name = '提醒记录'
        verbose_name_plural = '提醒记录'

    def __str__(self):
        return f'{self.task.name} - {self.get_reminder_type_display()} - {self.sent_at.date()}'


class UpwardNotifyLog(models.Model):
    """阶段/品的超期通知去重记录：同一天同一对象同一事件类型只发一次"""
    content_type_label = models.CharField(max_length=20)  # 'stage' 或 'product'
    object_id = models.PositiveIntegerField()
    event_type = models.CharField(max_length=20, default='overdue')
    sent_date = models.DateField()

    class Meta:
        unique_together = ('content_type_label', 'object_id', 'event_type', 'sent_date')

    def __str__(self):
        return f'{self.content_type_label}#{self.object_id} {self.event_type} @ {self.sent_date}'
