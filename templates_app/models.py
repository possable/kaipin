from django.db import models
from django.contrib.auth.models import User
from accounts.models import Department


class StageTemplate(models.Model):
    """公司当前标准开品流程的阶段定义（管理员维护）"""
    name = models.CharField(max_length=100, verbose_name='阶段名称')
    order = models.PositiveIntegerField(verbose_name='顺序号')
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, verbose_name='负责部门'
    )
    allow_parallel = models.BooleanField(
        default=False,
        verbose_name='允许并行（勾选后，该阶段无需等待上一阶段完成即可手动开始）'
    )
    default_assignee = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='default_stage_templates', verbose_name='默认负责人'
    )

    class Meta:
        verbose_name = '阶段模板'
        verbose_name_plural = '阶段模板'
        ordering = ['order']
        unique_together = ['order']

    def __str__(self):
        return f'{self.order}. {self.name}'


class TaskTemplate(models.Model):
    """某阶段下的默认子任务清单（管理员维护）"""
    stage_template = models.ForeignKey(
        StageTemplate, on_delete=models.CASCADE,
        related_name='task_templates', verbose_name='所属阶段模板'
    )
    name = models.CharField(max_length=200, verbose_name='任务名称')
    order = models.PositiveIntegerField(default=0, verbose_name='排序')
    is_milestone = models.BooleanField(default=False, verbose_name='是否里程碑')

    class Meta:
        verbose_name = '子任务模板'
        verbose_name_plural = '子任务模板'
        ordering = ['order']

    def __str__(self):
        return self.name


class ChecklistItemTemplate(models.Model):
    """某子任务下的默认最小事项清单（管理员维护）"""
    task_template = models.ForeignKey(
        TaskTemplate, on_delete=models.CASCADE,
        related_name='checklist_item_templates', verbose_name='所属子任务模板'
    )
    name = models.CharField(max_length=200, verbose_name='事项名称')
    order = models.PositiveIntegerField(default=0, verbose_name='排序')
    is_milestone = models.BooleanField(default=False, verbose_name='是否里程碑')

    class Meta:
        verbose_name = '最小事项模板'
        verbose_name_plural = '最小事项模板'
        ordering = ['order']

    def __str__(self):
        return self.name
