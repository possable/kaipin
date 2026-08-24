from datetime import datetime
from django.shortcuts import redirect, render, get_object_or_404
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.views import PasswordChangeView
from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_POST
from reminders.wechat import build_oauth_url, get_userid_by_code, get_user_detail
from accounts.models import Department, TodoItem, Announcement
from accounts.decorators import admin_required
from accounts.utils import get_or_create_user_from_wechat
from activity_log.utils import log_action
import logging

logger = logging.getLogger(__name__)


class ChangePasswordView(PasswordChangeView):
    """本地密码用户修改密码。企微扫码用户没有可用密码，Django 自带 form 会拒绝。"""
    template_name = 'registration/change_password.html'
    success_url = reverse_lazy('password_change_done')

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(self.request.user, '修改密码', 'user', self.request.user.id,
                   self.request.user.first_name or self.request.user.username, '')
        return response


@admin_required
def user_list(request):
    """管理员查看所有用户，可搜索、重置他人密码"""
    q = request.GET.get('q', '').strip()
    users = User.objects.select_related('profile__department').order_by(
        '-is_active', 'first_name', 'username'
    )
    if q:
        users = users.filter(
            Q(first_name__icontains=q)
            | Q(username__icontains=q)
            | Q(profile__department__name__icontains=q)
        )
    return render(request, 'accounts/user_list.html', {
        'users': users,
        'search_q': q,
    })


@admin_required
@require_POST
def reset_user_password(request, user_id):
    """管理员重置某个用户的密码为随机 8 位字符（排除易混淆字符）"""
    target = get_object_or_404(User, pk=user_id)
    if target.id == request.user.id:
        return JsonResponse(
            {'error': '不能重置自己的密码，请用侧边栏"修改密码"功能'},
            status=400
        )
    new_password = get_random_string(
        length=8,
        allowed_chars='abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'
    )
    target.set_password(new_password)
    target.save()
    log_action(request.user, '重置密码', 'user', target.id,
               target.first_name or target.username, '')
    return JsonResponse({'success': True, 'new_password': new_password})


@admin_required
@require_POST
def toggle_admin_role(request, user_id):
    """管理员切换某个用户的管理员角色"""
    target = get_object_or_404(User, pk=user_id)
    if target.id == request.user.id:
        return JsonResponse({'error': '不能修改自己的角色'}, status=400)
    profile = target.profile
    if profile.is_admin:
        profile.role = 'member'
        profile.save()
        log_action(request.user, '取消管理员', 'user', target.id,
                   target.first_name or target.username, '角色: 管理员 → 普通成员')
        return JsonResponse({'success': True, 'is_admin': False})
    else:
        profile.role = 'admin'
        profile.save()
        log_action(request.user, '设为管理员', 'user', target.id,
                   target.first_name or target.username, '角色: 普通成员 → 管理员')
        return JsonResponse({'success': True, 'is_admin': True})


def auto_login(request):
    """工作台免登录入口——企业微信工作台主页直接配这个 URL。
    流程：有 session → 直接进看板；URL 带 code → 换 token 登录；
    都没有 → 静默 OAuth 跳转（snsapi_base，用户无感知）。"""
    # 已登录，直接进
    if request.user.is_authenticated:
        return redirect('kanban')

    # URL 带 code（OAuth 回调），交换身份并登录
    code = request.GET.get('code')
    if code:
        return _wechat_code_login(request, code)

    # 无身份无 code，发起静默 OAuth
    callback_url = request.build_absolute_uri(reverse('auto_login'))
    # 如果部署在代理（Cloudflare Tunnel）后面，确保回调地址使用 HTTPS
    from django.conf import settings
    if settings.SITE_URL and settings.SITE_URL.startswith('https'):
        callback_url = settings.SITE_URL.rstrip('/') + reverse('auto_login')
    oauth_url = build_oauth_url(callback_url)
    return redirect(oauth_url)


def _wechat_code_login(request, code):
    """用 OAuth code 换取企微身份并登录"""
    userid = get_userid_by_code(code)
    if not userid:
        messages.error(request, '获取企业微信身份失败，请联系管理员。')
        return redirect('login')

    detail = get_user_detail(userid)
    chinese_name = (detail.get('name', '') if detail else '') or userid
    dept_ids = (detail.get('department', []) if detail else [])

    user, created = get_or_create_user_from_wechat(userid, chinese_name, dept_ids)
    if created:
        logger.info(f'新用户通过企微工作台登录: {chinese_name} ({user.username})')

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    messages.success(request, f'欢迎, {user.first_name or user.username}!')
    return redirect('kanban')


def wechat_login(request):
    """发起企业微信 OAuth 扫码登录（保留给旧入口）"""
    callback_url = request.build_absolute_uri(reverse('auto_login'))
    from django.conf import settings
    if settings.SITE_URL and settings.SITE_URL.startswith('https'):
        callback_url = settings.SITE_URL.rstrip('/') + reverse('auto_login')
    oauth_url = build_oauth_url(callback_url)
    return redirect(oauth_url)


# ========================================
# 个人待办事项（每个账号独立）
# ========================================

@login_required
@require_POST
def todo_add(request):
    """新增当前用户的一条待办事项"""
    content = request.POST.get('content', '').strip()
    due_at_raw = request.POST.get('due_at', '').strip()
    if not content:
        return JsonResponse({'error': '内容不能为空'}, status=400)
    if len(content) > 200:
        return JsonResponse({'error': '内容不能超过 200 字'}, status=400)

    due_at = None
    if due_at_raw:
        try:
            # 前端提交 datetime-local 格式 'YYYY-MM-DDTHH:MM'
            naive = datetime.strptime(due_at_raw, '%Y-%m-%dT%H:%M')
            due_at = timezone.make_aware(naive)
        except ValueError:
            return JsonResponse({'error': '截止时间格式不正确'}, status=400)

    todo = TodoItem.objects.create(user=request.user, content=content, due_at=due_at)
    return JsonResponse({
        'success': True,
        'id': todo.id,
        'content': todo.content,
        'due_at': todo.due_at.strftime('%Y-%m-%d %H:%M') if todo.due_at else '',
        'is_done': todo.is_done,
    })


@login_required
@require_POST
def todo_toggle(request, todo_id):
    """勾选/取消完成。若 todo 关联到某任务（auto_todo），同步标记源任务的完成状态。"""
    todo = get_object_or_404(TodoItem, pk=todo_id)
    if todo.user_id != request.user.id:
        return JsonResponse({'error': '无权限操作该待办'}, status=403)
    todo.is_done = not todo.is_done
    todo.completed_at = timezone.now() if todo.is_done else None
    todo.save(update_fields=['is_done', 'completed_at'])
    # 逆向同步：若是 auto_todo（关联任务），同时更新源任务状态
    task = todo.source_task
    if task:
        if todo.is_done and task.status != 'completed':
            task.status = 'completed'
            task.completed_at = todo.completed_at
            task.actual_end_date = todo.completed_at
            task.save(update_fields=['status', 'completed_at', 'actual_end_date'])
        elif not todo.is_done and task.status == 'completed':
            # 取消勾选：任务恢复为未完成状态，由 update_status 依据时间字段重算
            task.status = 'pending'
            task.completed_at = None
            task.actual_end_date = None
            task.save(update_fields=['status', 'completed_at', 'actual_end_date'])
            task.update_status()
    return JsonResponse({'success': True, 'is_done': todo.is_done})


@login_required
@require_POST
def todo_delete(request, todo_id):
    """删除一条待办事项"""
    todo = get_object_or_404(TodoItem, pk=todo_id)
    if todo.user_id != request.user.id:
        return JsonResponse({'error': '无权限操作该待办'}, status=403)
    todo.delete()
    return JsonResponse({'success': True})


@login_required
def todo_list(request):
    """当前用户的全部待办列表（用于'更多'弹窗，本次暂不接入 UI，保留接口）"""
    todos = list(request.user.todos.all().values(
        'id', 'content', 'is_done', 'due_at', 'created_at'
    ))
    # datetime 序列化
    for t in todos:
        t['due_at'] = t['due_at'].strftime('%Y-%m-%d %H:%M') if t['due_at'] else ''
        t['created_at'] = t['created_at'].strftime('%Y-%m-%d %H:%M')
    return JsonResponse({'todos': todos})


@admin_required
def announcement_list(request):
    """管理员查看/管理全部公告"""
    announcements = Announcement.objects.select_related('created_by')
    return render(request, 'accounts/announcement_list.html', {
        'announcements': announcements,
    })


def _get_announcement_post_data(request):
    title = request.POST.get('title', '').strip()
    content = request.POST.get('content', '').strip()
    if not title:
        return None, '标题不能为空'
    if not content:
        return None, '内容不能为空'
    return {
        'title': title,
        'content': content,
        'is_pinned': request.POST.get('is_pinned') == 'on',
        'is_active': request.POST.get('is_active') == 'on',
    }, None


@admin_required
def announcement_create(request):
    if request.method == 'POST':
        data, error = _get_announcement_post_data(request)
        if error:
            messages.error(request, error)
        else:
            ann = Announcement.objects.create(created_by=request.user, **data)
            log_action(request.user, '发布公告', 'announcement', ann.id, ann.title, '')
            messages.success(request, f'公告 "{ann.title}" 已发布。')
            return redirect('announcement_list')
    return render(request, 'accounts/announcement_form.html', {'action': '发布'})


@admin_required
def announcement_edit(request, pk):
    ann = get_object_or_404(Announcement, pk=pk)
    if request.method == 'POST':
        data, error = _get_announcement_post_data(request)
        if error:
            messages.error(request, error)
        else:
            ann.title = data['title']
            ann.content = data['content']
            ann.is_pinned = data['is_pinned']
            ann.is_active = data['is_active']
            ann.save()
            log_action(request.user, '编辑公告', 'announcement', ann.id, ann.title, '')
            messages.success(request, f'公告 "{ann.title}" 已更新。')
            return redirect('announcement_list')
    return render(request, 'accounts/announcement_form.html', {'announcement': ann, 'action': '编辑'})


@admin_required
@require_POST
def announcement_delete(request, pk):
    ann = get_object_or_404(Announcement, pk=pk)
    title = ann.title
    ann.delete()
    log_action(request.user, '删除公告', 'announcement', pk, title, '')
    messages.success(request, f'公告 "{title}" 已删除。')
    return redirect('announcement_list')
