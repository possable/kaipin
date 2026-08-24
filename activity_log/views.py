from django.db.models import Q
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from .models import ActivityLog


@login_required
def activity_list(request):
    """操作日志列表：支持按操作人、操作类型筛选，按目标名称/详情关键词搜索"""
    user_id = request.GET.get('user', '').strip()
    action = request.GET.get('action', '').strip()
    q = request.GET.get('q', '').strip()

    logs = ActivityLog.objects.select_related('user').all()

    if user_id:
        try:
            logs = logs.filter(user_id=int(user_id))
        except ValueError:
            user_id = ''

    if action:
        logs = logs.filter(action=action)

    if q:
        logs = logs.filter(Q(target_name__icontains=q) | Q(detail__icontains=q))

    has_filter = bool(user_id or action or q)
    logs = logs[:200]

    all_users = User.objects.select_related('profile').order_by('first_name', 'username')
    action_choices = (
        ActivityLog.objects.order_by('action').values_list('action', flat=True).distinct()
    )

    return render(request, 'activity_log/activity_list.html', {
        'logs': logs,
        'all_users': all_users,
        'action_choices': action_choices,
        'filter_user': user_id,
        'filter_action': action,
        'filter_q': q,
        'has_filter': has_filter,
    })
