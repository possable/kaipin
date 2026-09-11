from datetime import datetime
import secrets
import logging
import requests
from django.shortcuts import redirect, render, get_object_or_404
from django.contrib.auth import SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from reminders.wechat import build_oauth_url, get_userid_by_code, get_user_detail
from accounts.models import Department, TodoItem, Announcement
from accounts.decorators import admin_required
from accounts.utils import get_or_create_user_from_wechat
from activity_log.utils import log_action

logger = logging.getLogger(__name__)


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
            {'error': '不能重置自己的密码'},
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


OAUTH_STATE_SESSION_KEY = 'wecom_oauth_state'
OAUTH_NEXT_SESSION_KEY = 'wecom_oauth_next'


def _wecom_callback_url(request):
    """企微 OAuth 回调地址，固定指向 auto_login 入口（SITE_URL 覆盖保证 https）。"""
    callback_url = request.build_absolute_uri(reverse('auto_login'))
    from django.conf import settings
    if settings.SITE_URL and settings.SITE_URL.startswith('https'):
        callback_url = settings.SITE_URL.rstrip('/') + reverse('auto_login')
    return callback_url


def _get_safe_next(request):
    """从 GET 参数读取 next，只允许站内相对路径，防止开放重定向。"""
    next_url = request.GET.get('next', '').strip()
    if next_url and not next_url.startswith('//') \
            and url_has_allowed_host_and_scheme(next_url, None):
        return next_url
    return None


def _start_wecom_oauth(request, next_url=None):
    """生成 state 并保存 next 到 session，返回企微 OAuth 授权链接。"""
    state = secrets.token_urlsafe(32)
    request.session[OAUTH_STATE_SESSION_KEY] = state
    if next_url:
        request.session[OAUTH_NEXT_SESSION_KEY] = next_url
    return build_oauth_url(_wecom_callback_url(request), state=state)


def auto_login(request):
    """工作台免登录入口 + OAuth 回调——企业微信工作台主页直接配这个 URL。
    流程：有 session → 直接进看板；URL 带 code → 校验 state 后换身份登录；
    都没有 → 静默 OAuth 跳转（snsapi_base，用户无感知）。"""
    # 已登录，直接进
    if request.user.is_authenticated:
        next_url = request.session.pop(OAUTH_NEXT_SESSION_KEY, None)
        return redirect(next_url or 'kanban')

    # URL 带 code（OAuth 回调），先校验 state 再交换身份
    code = request.GET.get('code')
    if code:
        expected_state = request.session.pop(OAUTH_STATE_SESSION_KEY, None)
        if not expected_state or request.GET.get('state') != expected_state:
            logger.warning(
                '企微 OAuth state 校验失败: got_state=%s, expected=%s, cookies=%s, ua=%s, ip=%s',
                request.GET.get('state'), bool(expected_state),
                sorted(request.COOKIES.keys()),
                (request.META.get('HTTP_USER_AGENT') or '')[:100],
                request.META.get('REMOTE_ADDR'))
            return render(request, 'registration/wecom_error.html', {
                'error_message': '登录状态校验失败，请重新发起登录。',
            })
        user = _wechat_code_login(request, code)
        if user is None:
            return render(request, 'registration/wecom_error.html', {
                'error_message': '企业微信登录失败，请稍后重试；如持续失败请联系管理员。',
            })
        next_url = request.session.pop(OAUTH_NEXT_SESSION_KEY, None)
        return redirect(next_url or 'kanban')

    # 无身份无 code，发起静默 OAuth（保留 session 里的 next，双标签页场景兜底）
    next_url = _get_safe_next(request)
    if not next_url:
        next_url = request.session.get(OAUTH_NEXT_SESSION_KEY)
    return redirect(_start_wecom_oauth(request, next_url))


def _wechat_code_login(request, code):
    """用 OAuth code 换取企微身份并登录。成功返回 User，失败返回 None。"""
    try:
        userid = get_userid_by_code(code)
    except (requests.RequestException, ValueError) as e:
        logger.error(f'企微获取 userid 异常: {e}')
        return None
    if not userid:
        logger.warning('企微 code 换 userid 失败（code 无效/过期或非企业成员）')
        return None

    try:
        detail = get_user_detail(userid)
    except (requests.RequestException, ValueError) as e:
        logger.error(f'企微获取用户详情异常: {e}')
        return None
    chinese_name = (detail.get('name', '') if detail else '') or userid
    dept_ids = (detail.get('department', []) if detail else [])

    user, created = get_or_create_user_from_wechat(userid, chinese_name, dept_ids)
    if created:
        logger.info(f'新用户通过企微工作台登录: {chinese_name} ({user.username})')

    # 企微客户端内置浏览器在 302 跳转链路上不更新 cookie，标准 login() 会
    # cycle_key 换新 sessionid，导致登录态无法持久（表现为登录成功后立刻
    # 又弹回 OAuth 死循环）。这里手动写入认证信息、保持原 session 键不变。
    # backend 必须用 settings.AUTHENTICATION_BACKENDS 里注册的，否则
    # get_user() 会丢弃登录态（写 ModelBackend 会静默失效）。
    from django.conf import settings
    request.session[SESSION_KEY] = user._meta.pk.value_to_string(user)
    request.session[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
    request.session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    request.session.modified = True

    messages.success(request, f'欢迎, {user.first_name or user.username}!')
    return user


def wechat_login(request):
    """发起企业微信 OAuth 登录（/accounts/login/ 默认入口，wechat-login/ 保留旧入口）。"""
    if request.user.is_authenticated:
        next_url = request.session.pop(OAUTH_NEXT_SESSION_KEY, None)
        return redirect(next_url or 'kanban')

    next_url = _get_safe_next(request)
    if not next_url:
        next_url = request.session.get(OAUTH_NEXT_SESSION_KEY)
    return redirect(_start_wecom_oauth(request, next_url))


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
