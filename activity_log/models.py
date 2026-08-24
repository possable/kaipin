from django.db import models
from django.contrib.auth.models import User


class ActivityLog(models.Model):
    """操作日志"""
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='操作人',
        related_name='activity_logs'
    )
    action = models.CharField(max_length=100, verbose_name='操作')
    target_type = models.CharField(max_length=50, verbose_name='目标类型')
    target_id = models.IntegerField(verbose_name='目标ID')
    target_name = models.CharField(max_length=300, verbose_name='目标名称')
    detail = models.TextField(blank=True, default='', verbose_name='详情')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='操作时间')

    class Meta:
        verbose_name = '操作日志'
        verbose_name_plural = '操作日志'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username} {self.action} {self.target_name}'
