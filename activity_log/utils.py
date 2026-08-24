from .models import ActivityLog


def log_action(user, action, target_type, target_id, target_name, detail=''):
    """
    记录一条操作日志。

    参数:
        user: User 实例
        action: 操作描述，如 "标记任务完成"
        target_type: 目标类型，如 "product", "task", "stage", "attachment"
        target_id: 目标对象的主键
        target_name: 目标的人类可读名称
        detail: 可选的额外信息

    返回:
        ActivityLog 实例
    """
    return ActivityLog.objects.create(
        user=user,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        detail=detail,
    )
