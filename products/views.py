from urllib.parse import quote

from django.db import models
from django.contrib.auth.models import User
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse, FileResponse, Http404, HttpResponse
from django.views.decorators.http import require_POST
from django.utils import timezone
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from accounts.decorators import admin_required, can_create_product
from .models import (
    Product, ProductStage, Task, TaskAttachment, TaskChecklistItem,
    TaskChecklistLog, is_visible_to, compute_task_color, compute_task_status,
)
from templates_app.models import StageTemplate
from activity_log.utils import log_action
from reminders.wechat import send_wechat_message
from django.conf import settings

from .progress_export import render_product_progress_png


def _month_start(value):
    return value.replace(day=1)


def _next_month(value):
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _milestone_label_width(name):
    """估算单行里程碑胶囊宽度，兼顾中文、英文和图标留白。"""
    visual_units = sum(1 if ord(char) > 127 else .62 for char in str(name or ''))
    return max(78, min(236, round(visual_units * 13 + 42)))


def _compute_progress_stage_status(stage, tasks, effective_statuses, today):
    """根据任务真实进度推导总览中的阶段状态，不修改数据库状态。"""
    if not tasks:
        if stage.status in ('completed', 'in_progress'):
            return stage.status
        return 'pending'

    if all(status == 'completed' for status in effective_statuses):
        return 'completed'
    if any(status == 'overdue' for status in effective_statuses):
        return 'overdue'
    if any(status == 'in_progress' for status in effective_statuses):
        return 'in_progress'
    if stage.status == 'in_progress':
        return 'in_progress'
    if any(status == 'completed' for status in effective_statuses):
        return 'in_progress'
    if any(
        task.started_at and task.started_at.date() <= today
        for task in tasks
    ):
        return 'in_progress'
    return 'pending'


def _build_progress_timeline(product, stage_items, today):
    """根据项目、阶段和里程碑真实日期构建生命周期路线图定位数据。"""
    project_start = (
        product.started_at.date()
        if product.started_at else product.created_at.date()
    )
    project_end = product.expected_end_date or (project_start + timedelta(days=150))

    date_points = [project_start, project_end]
    for stage in stage_items:
        date_points.extend(
            value for value in (
                stage.get('started_date'),
                stage.get('expected_end_date'),
                stage.get('completed_date'),
            ) if value
        )
        for task in stage['tasks']:
            date_points.extend(
                value for value in (
                    task.get('started_date'),
                    task.get('expected_end_date'),
                    task.get('actual_end_date'),
                ) if value
            )

    timeline_start = _month_start(min(date_points))
    timeline_last_month = _month_start(max(date_points))

    # 参考图使用约 6 个月的观察窗口；短周期项目向右补足，避免时间轴过空。
    month_count = 1
    probe_month = timeline_start
    while probe_month < timeline_last_month:
        probe_month = _next_month(probe_month)
        month_count += 1
    while month_count < 6:
        timeline_last_month = _next_month(timeline_last_month)
        month_count += 1

    timeline_end = _next_month(timeline_last_month)
    total_days = max(1, (timeline_end - timeline_start).days)

    def position(value):
        days = (value - timeline_start).days
        return round(max(0, min(100, days / total_days * 100)), 3)

    months = []
    month_cursor = timeline_start
    while month_cursor < timeline_end:
        next_cursor = _next_month(month_cursor)
        months.append({
            'label': f'{month_cursor.month:02d}月',
            'start_pct': position(month_cursor),
            'width_pct': round((next_cursor - month_cursor).days / total_days * 100, 3),
        })
        month_cursor = next_cursor

    project_span = max(1, (project_end - project_start).days)
    stage_count = max(1, len(stage_items))
    for index, stage in enumerate(stage_items):
        milestone_starts = [task['started_date'] for task in stage['tasks'] if task.get('started_date')]
        milestone_ends = [
            task.get('actual_end_date') or task.get('expected_end_date')
            for task in stage['tasks']
            if task.get('actual_end_date') or task.get('expected_end_date')
        ]
        start_candidates = milestone_starts + ([stage['started_date']] if stage.get('started_date') else [])
        end_candidates = milestone_ends + [
            value for value in (
                stage.get('completed_date'),
                stage.get('expected_end_date'),
            ) if value
        ]

        fallback_start = project_start + timedelta(days=round(project_span * index / stage_count))
        fallback_end = project_start + timedelta(days=round(project_span * (index + 1) / stage_count))
        stage_start = min(start_candidates) if start_candidates else fallback_start
        stage_end = max(end_candidates) if end_candidates else fallback_end
        if stage_end <= stage_start:
            stage_end = stage_start + timedelta(days=max(7, round(project_span / stage_count)))

        left_pct = position(stage_start)
        right_pct = position(stage_end + timedelta(days=1))
        stage['timeline_start_date'] = stage_start
        stage['timeline_end_date'] = stage_end
        stage['timeline_left_pct'] = left_pct
        stage_width_pct = round(max(4, right_pct - left_pct), 3)
        stage['timeline_width_pct'] = stage_width_pct

        milestone_widths = [task['display_width_px'] for task in stage['tasks']]
        stage['timeline_min_width_px'] = (
            sum(milestone_widths) + max(0, len(milestone_widths) - 1) * 6 + 16
            if milestone_widths else 88
        )
        estimated_min_width_pct = stage['timeline_min_width_px'] / 13.2
        reserved_right_pct = 7 if stage['status_key'] == 'overdue' else 1
        display_width_pct = max(stage_width_pct, estimated_min_width_pct)
        stage['timeline_align_right'] = (
            right_pct > 100 - reserved_right_pct
            or left_pct + display_width_pct > 100 - reserved_right_pct
        )
        stage['timeline_right_pct'] = max(
            reserved_right_pct,
            round(100 - right_pct, 3),
        )

    return {
        'months': months,
        'month_count': len(months),
        'start_date': timeline_start,
        'end_date': timeline_end - timedelta(days=1),
        'today_visible': timeline_start <= today < timeline_end,
        'today_pct': position(today),
    }


def _notify_new_assignee(entity, old_assignee, new_assignee, changed_by, context):
    """assignee 变更时向新负责人发送企微通知。
    不管谁改的（管理员/品负责人/阶段负责人），只要设定了新负责人且不是本人操作，就发。"""
    if not new_assignee:
        return          # 设为空，不发
    if old_assignee == new_assignee:
        return          # 没变，不发
    if new_assignee == changed_by:
        return          # 自己设自己，不发

    wechat_id = getattr(getattr(new_assignee, 'profile', None), 'wechat_userid', '')
    if not wechat_id:
        return

    recipient_name = new_assignee.first_name or new_assignee.username
    changer_name = changed_by.first_name or changed_by.username
    content = (
        f'{recipient_name}你好，我是项目管理智能机器人。\n'
        f'{changer_name} 已将你设为 {context} 的负责人，请及时关注。\n'
        f'点击查看：{settings.SITE_URL}'
    )

    try:
        send_wechat_message(wechat_id, content)
        log_action(changed_by, f'通知新负责人（{context}）', 'user', new_assignee.id,
                   new_assignee.first_name or new_assignee.username, '已发送企微通知')
    except Exception:
        pass  # 通知是非关键路径，失败不影响主流程


def _parse_dt(value):
    """兼容 date 输入（'Y-m-d'）和 datetime-local 输入（'Y-m-dTH:M'）。
    项目 USE_TZ=True，返回带当前时区的 aware datetime。"""
    if not value:
        return None
    if 'T' in value:
        naive = datetime.strptime(value, '%Y-%m-%dT%H:%M')
    else:
        naive = datetime.strptime(value, '%Y-%m-%d')
    return timezone.make_aware(naive)


# 产品资料字段（选填，创建时可留空，创建后由负责人逐步补充）
PRODUCT_PROFILE_TEXT_FIELDS = [
    'product_name', 'brand', 'platforms', 'category', 'positioning',
    'dosage_form', 'specification', 'main_ingredients', 'efficacy',
    'target_audience', 'usage_scenario', 'selling_points',
    'material_advantage', 'project_rationale',
]
PRODUCT_PROFILE_DATE_FIELDS = []
PRODUCT_PROFILE_DECIMAL_FIELDS = [
    'suggested_retail_price', 'suggested_cost_price', 'expected_gross_margin',
]


def _apply_product_profile_fields(product, post_data):
    """从 POST 数据里读取产品资料字段并赋值到 product 实例上（不保存）"""
    for field in PRODUCT_PROFILE_TEXT_FIELDS:
        setattr(product, field, post_data.get(field, '').strip())
    for field in PRODUCT_PROFILE_DATE_FIELDS:
        value = post_data.get(field, '').strip()
        if not value:
            setattr(product, field, None)
            continue
        try:
            setattr(product, field, datetime.strptime(value, '%Y-%m-%d').date())
        except ValueError:
            setattr(product, field, None)
    for field in PRODUCT_PROFILE_DECIMAL_FIELDS:
        value = post_data.get(field, '').strip()
        if not value:
            setattr(product, field, None)
            continue
        try:
            setattr(product, field, Decimal(value))
        except InvalidOperation:
            setattr(product, field, None)


@login_required
def product_create(request):
    if not can_create_product(request.user):
        raise PermissionDenied
    has_templates = StageTemplate.objects.exists()

    all_users = User.objects.select_related('profile').order_by('first_name', 'username')

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        assignee_id = request.POST.get('assignee', '').strip()
        started_at_raw = request.POST.get('started_at', '').strip()
        expected_end_raw = request.POST.get('expected_end_date', '').strip()

        error_msg = None
        if not name:
            error_msg = '品名不能为空。'
        elif not assignee_id:
            error_msg = '总负责人不能为空。'
        elif not started_at_raw:
            error_msg = '项目开始时间不能为空。'
        elif not expected_end_raw:
            error_msg = '项目预计结束时间不能为空。'

        started_at = None
        expected_end = None
        if not error_msg:
            try:
                started_at = _parse_dt(started_at_raw)
            except ValueError:
                error_msg = '项目开始时间格式不正确。'
        if not error_msg:
            try:
                expected_end = datetime.strptime(expected_end_raw, '%Y-%m-%d').date()
            except ValueError:
                error_msg = '项目预计结束时间格式不正确。'
        if not error_msg and started_at.date() > expected_end:
            error_msg = '项目开始时间不能大于预计结束时间。'

        if error_msg:
            messages.error(request, error_msg)
            return render(request, 'products/product_create.html', {
                'has_templates': has_templates,
                'all_users': all_users,
                'form_name': name,
                'form_assignee_id': assignee_id,
                'form_started_at': started_at_raw,
                'form_expected_end_date': expected_end_raw,
            })
        if not has_templates:
            messages.error(request, '尚未配置阶段模板，请先配置。')
            return redirect('stage_template_list')

        assignee = None
        try:
            assignee = User.objects.get(pk=int(assignee_id))
        except (User.DoesNotExist, ValueError):
            messages.error(request, '总负责人无效。')
            return render(request, 'products/product_create.html', {
                'has_templates': has_templates, 'all_users': all_users,
            })

        product = Product(
            name=name, creator=request.user, assignee=assignee, status='draft',
            started_at=started_at, expected_end_date=expected_end,
        )
        _apply_product_profile_fields(product, request.POST)
        product.save()
        product.create_stages_from_templates()
        product.sync_auto_todo()
        for stage in product.stages.all():
            stage.sync_auto_todo()
        log_action(request.user, '创建新品', 'product', product.id, product.name,
                   f'草稿，共 {product.stages.count()} 个阶段')
        messages.success(request, f'新品 "{name}" 已创建（草稿），编辑完成后由负责人发布。')
        return redirect('product_detail', pk=product.pk)

    return render(request, 'products/product_create.html', {
        'has_templates': has_templates,
        'all_users': all_users,
    })


def _annotate_stage_permissions(product, stages, user):
    """给阶段/任务标注当前用户的编辑权限，并刷新任务状态。返回 (is_admin, can_manage)"""
    user_dept = user.profile.department
    is_admin = user.profile.is_admin
    is_product_owner = (product.assignee == user)
    is_draft = (product.status == 'draft')
    for stage in stages:
        # 草稿状态：品负责人和管理员可以编辑所有阶段
        if is_draft and (is_admin or is_product_owner):
            stage.can_edit = True
        else:
            stage.can_edit = (
                is_admin
                or is_product_owner
                or stage.assignee == user  # 阶段负责人：不受阶段状态限制
                or (
                    stage.status == 'in_progress'
                    and stage.department == user_dept  # 部门成员：仅进行中的阶段
                )
            )
        # 每个子任务：任务负责人可操作完成/附件；管理员/品负责/阶段负责可编辑时间
        for task in stage.tasks.all():
            task.can_edit = (
                task.assignee == user
                or stage.can_edit
            )
            task.can_manage_time = (
                is_admin or is_product_owner
                or stage.assignee == user
            )
            items = list(task.checklist_items.all())
            task.checklist_total = len(items)
            task.checklist_done = sum(1 for i in items if i.is_done)
            task.update_status()
            task.color = compute_task_color(task)

    return is_admin, (is_admin or is_product_owner)


@login_required
def product_detail(request, pk):
    """品详情页：展示所有阶段和子任务"""
    product = get_object_or_404(Product, pk=pk)
    if not is_visible_to(product, request.user):
        raise Http404('项目不存在或无查看权限')
    stages = product.stages.all().prefetch_related(
        'tasks__attachments',
        'tasks__checklist_items__logs__user',
    )

    is_admin, can_manage = _annotate_stage_permissions(product, stages, request.user)

    # 所有用户列表（供负责人选择）
    all_users = User.objects.select_related('profile').order_by('first_name', 'username')

    return render(request, 'dashboard/product_detail.html', {
        'product': product,
        'stages': stages,
        'is_admin': is_admin,
        'can_manage': can_manage,
        'all_users': all_users,
    })


def _build_product_progress_overview(product, stages):
    """构建项目名称弹窗使用的整体进度总览数据，不修改任务数据库状态。"""
    stage_items = []
    overdue_milestones = []
    total_tasks = 0
    completed_tasks = 0
    overdue_tasks = 0
    today = timezone.localdate()

    status_meta = {
        'completed': ('已完成', 'bi-check-lg'),
        'in_progress': ('进行中', 'bi-play-fill'),
        'overdue': ('有超期', 'bi-exclamation-lg'),
        'pending': ('待开始', 'bi-clock'),
    }
    task_status_meta = {
        **status_meta,
        'overdue': ('已超期', 'bi-exclamation-lg'),
    }

    for stage in stages:
        tasks = list(stage.tasks.all())
        effective_statuses = [compute_task_status(task) for task in tasks]
        task_total = len(tasks)
        task_completed = sum(status == 'completed' for status in effective_statuses)
        task_overdue = sum(status == 'overdue' for status in effective_statuses)
        task_items = []
        for task, effective_status in zip(tasks, effective_statuses):
            if not task.is_milestone:
                continue
            task_status_label, task_status_icon = task_status_meta[effective_status]
            task_items.append({
                'id': task.pk,
                'name': task.name,
                'order': task.order,
                'status_key': effective_status,
                'status_label': task_status_label,
                'status_icon': task_status_icon,
                'started_date': task.started_at.date() if task.started_at else None,
                'expected_end_date': task.expected_end_date,
                'actual_end_date': task.actual_end_date.date() if task.actual_end_date else None,
                'display_width_px': _milestone_label_width(task.name),
            })
            if effective_status == 'overdue':
                risk_assignee = task.assignee or stage.assignee
                overdue_milestones.append({
                    'id': task.pk,
                    'name': task.name,
                    'stage_name': stage.name,
                    'stage_order': stage.order,
                    'task_order': task.order,
                    'department_name': stage.department.name if stage.department else '未分配部门',
                    'assignee_name': (
                        risk_assignee.first_name or risk_assignee.username
                        if risk_assignee else '未指定'
                    ),
                    'expected_end_date': task.expected_end_date,
                    'overdue_days': (today - task.expected_end_date).days,
                })

        total_tasks += task_total
        completed_tasks += task_completed
        overdue_tasks += task_overdue

        status_key = _compute_progress_stage_status(
            stage,
            tasks,
            effective_statuses,
            today,
        )

        status_label, status_icon = status_meta[status_key]
        stage_items.append({
            'id': stage.pk,
            'name': stage.name,
            'order': stage.order,
            'department_name': stage.department.name if stage.department else '未分配部门',
            'assignee_name': (
                stage.assignee.first_name or stage.assignee.username
                if stage.assignee else '未指定'
            ),
            'status_key': status_key,
            'status_label': status_label,
            'status_icon': status_icon,
            'started_date': stage.started_at.date() if stage.started_at else None,
            'expected_end_date': stage.expected_end_date,
            'completed_date': stage.completed_at.date() if stage.completed_at else None,
            'tasks': task_items,
        })

    total_stages = len(stage_items)
    completed_stages = sum(
        stage['status_key'] == 'completed' for stage in stage_items
    )
    active_stage_names = [
        stage['name'] for stage in stage_items
        if stage['status_key'] in ('in_progress', 'overdue')
    ]
    progress_pct = int(completed_tasks / total_tasks * 100) if total_tasks else (
        100 if product.status == 'completed' else 0
    )

    overdue_milestones.sort(
        key=lambda item: (-item['overdue_days'], item['stage_order'], item['task_order'])
    )
    project_overdue = bool(
        product.status == 'active'
        and product.expected_end_date
        and product.expected_end_date < today
    )
    if product.status == 'completed':
        status_key, status_label = 'completed', '项目已完成'
    elif product.status == 'cancelled':
        status_key, status_label = 'cancelled', '项目已取消'
    elif product.status == 'draft':
        status_key, status_label = 'draft', '项目待发布'
    elif overdue_tasks or project_overdue:
        status_key, status_label = 'overdue', '项目有超期'
    elif active_stage_names:
        status_key, status_label = 'in_progress', '项目进行中'
    else:
        status_key, status_label = 'waiting', '待启动阶段'

    if product.status == 'completed':
        remaining_text = '已完成'
    elif product.status == 'cancelled':
        remaining_text = '已取消'
    elif not product.expected_end_date:
        remaining_text = '未设置截止日期'
    else:
        remaining_days = (product.expected_end_date - today).days
        remaining_text = (
            f'剩余 {remaining_days} 天'
            if remaining_days >= 0 else f'已超期 {-remaining_days} 天'
        )

    timeline = _build_progress_timeline(product, stage_items, today)

    return {
        'stages': stage_items,
        'stage_count': total_stages,
        'completed_stages': completed_stages,
        'total_tasks': total_tasks,
        'completed_tasks': completed_tasks,
        'overdue_tasks': overdue_tasks,
        'overdue_milestones': overdue_milestones,
        'overdue_milestone_count': len(overdue_milestones),
        'progress_pct': progress_pct,
        'status_key': status_key,
        'status_label': status_label,
        'active_stage_text': '、'.join(active_stage_names) if active_stage_names else '暂无进行中阶段',
        'remaining_text': remaining_text,
        'timeline': timeline,
    }


@login_required
def product_progress_modal(request, pk):
    """返回按月份展开、仅展示里程碑节点的项目进度总览。"""
    product = get_object_or_404(Product.objects.select_related('assignee'), pk=pk)
    if not is_visible_to(product, request.user):
        raise Http404('项目不存在或无查看权限')
    stages = list(
        product.stages.select_related('department', 'assignee').prefetch_related(
            models.Prefetch(
                'tasks',
                queryset=Task.objects.select_related('assignee').order_by('order'),
            )
        ).order_by('order')
    )

    return render(request, 'dashboard/_product_progress_overview.html', {
        'product': product,
        'progress_overview': _build_product_progress_overview(product, stages),
    })


@login_required
def product_progress_export_png(request, pk):
    """在服务端生成项目进度总览 PNG，避免浏览器 Canvas 跨域污染。"""
    product = get_object_or_404(Product.objects.select_related('assignee'), pk=pk)
    if not is_visible_to(product, request.user):
        raise Http404('项目不存在或无查看权限')

    stages = list(
        product.stages.select_related('department', 'assignee').prefetch_related(
            models.Prefetch(
                'tasks',
                queryset=Task.objects.select_related('assignee').order_by('order'),
            )
        ).order_by('order')
    )
    overview = _build_product_progress_overview(product, stages)
    png_data = render_product_progress_png(product, overview)

    filename = f'{product.name}_项目进度总览_{timezone.localdate()}.png'
    response = HttpResponse(png_data, content_type='image/png')
    response['Content-Disposition'] = (
        f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    )
    response['Cache-Control'] = 'no-store'
    return response


@login_required
def product_info_modal(request, pk):
    """返回独立的项目基本信息和产品资料弹窗。"""
    product = get_object_or_404(Product, pk=pk)
    if not is_visible_to(product, request.user):
        raise Http404('项目不存在或无查看权限')

    return render(request, 'dashboard/_product_info.html', {
        'product': product,
        'can_manage': product.can_be_managed_by(request.user),
        'hide_datalist': True,
        'datalist_id': 'modal-users-datalist',
    })


@login_required
def stage_detail_modal(request, stage_id):
    """返回单个阶段的详情片段（供看板弹窗使用）"""
    stage = get_object_or_404(ProductStage, pk=stage_id)
    product = stage.product
    if not is_visible_to(product, request.user):
        raise Http404('项目不存在或无查看权限')
    stages = product.stages.filter(pk=stage.pk).prefetch_related(
        'tasks__attachments',
        'tasks__checklist_items__logs__user',
    )

    is_admin, can_manage = _annotate_stage_permissions(product, stages, request.user)
    stage = stages.first()

    all_users = User.objects.select_related('profile').order_by('first_name', 'username')

    return render(request, 'dashboard/_stage_detail.html', {
        'stage': stage,
        'product': product,
        'is_admin': is_admin,
        'can_manage': can_manage,
        'all_users': all_users,
    })


def _check_task_permission(user, task):
    """检查用户是否对某个 Task 有编辑权限"""
    stage = task.product_stage
    product = stage.product
    # 任务负责人始终可编辑自己的任务
    if task.assignee == user:
        return True
    # 草稿状态：品负责人和管理员可编辑
    if product.status == 'draft' and (user.profile.is_admin or product.assignee == user):
        return True
    # 管理员/品负责人：始终可编辑
    # 阶段负责人：始终可编辑（不受阶段状态限制，方便前期设置执行人）
    # 阶段所属部门成员：仅进行中的阶段可编辑
    return (
        user.profile.is_admin
        or product.assignee == user
        or stage.assignee == user
        or (
            stage.status == 'in_progress'
            and stage.department == user.profile.department
        )
    )


@login_required
@require_POST
def task_complete(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    if not _check_task_permission(request.user, task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)
    if task.status == 'completed':
        # 撤销完成：回到待开始，清除完成时间和实际结束时间
        task.status = 'pending'
        task.completed_at = None
        task.actual_end_date = None
        task.save()
        task.update_status()  # 根据时间字段重算
        task.sync_auto_todo()
        log_action(request.user, '撤销任务完成', 'task', task.id,
                   f'{task.product_stage.product.name} / {task.name}',
                   f'任务状态: 已完成 → {task.get_status_display()}')
        return JsonResponse({'success': True, 'status': task.status, 'reverted': True})
    old_status = task.get_status_display()
    task.mark_completed()
    task.sync_auto_todo()
    log_action(request.user, '标记任务完成', 'task', task.id,
               f'{task.product_stage.product.name} / {task.name}',
               f'任务状态: {old_status} → 已完成')
    return JsonResponse({'success': True, 'status': 'completed', 'reverted': False})


@login_required
@require_POST
def task_update_deadline(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    if not _check_task_permission(request.user, task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)
    try:
        date_str = request.POST['deadline']
        new_deadline = datetime.strptime(date_str, '%Y-%m-%d').date()
        old_deadline = task.deadline.strftime('%Y-%m-%d') if task.deadline else '无'
        task.deadline = new_deadline
        task.save(update_fields=['deadline'])
        log_action(request.user, '修改任务', 'task', task.id,
                   f'{task.product_stage.product.name} / {task.name}',
                   f'截止日期: {old_deadline} → {date_str}')
        return JsonResponse({'success': True, 'deadline': date_str})
    except (KeyError, ValueError):
        return JsonResponse({'error': '日期格式错误，需要 YYYY-MM-DD'}, status=400)


@login_required
@require_POST
def task_upload_attachment(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    if not _check_task_permission(request.user, task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)
    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'error': '请选择文件'}, status=400)

    # 校验扩展名
    ext = uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else ''
    if ext not in TaskAttachment.ALLOWED_EXTENSIONS:
        return JsonResponse({'error': f'不支持的文件类型: .{ext}'}, status=400)

    # 校验大小
    max_bytes = TaskAttachment.MAX_SIZE_MB * 1024 * 1024
    if uploaded.size > max_bytes:
        return JsonResponse({
            'error': f'文件超过 {TaskAttachment.MAX_SIZE_MB}MB 限制'
        }, status=400)

    attachment = TaskAttachment.objects.create(
        task=task, file=uploaded, uploaded_by=request.user
    )
    log_action(request.user, '上传附件', 'attachment', attachment.id,
               uploaded.name,
               f'任务: {task.product_stage.product.name} / {task.name}')
    return JsonResponse({
        'success': True,
        'id': attachment.id,
        'filename': uploaded.name,
        'url': attachment.get_download_url(),
    })


@login_required
def attachment_download(request, attachment_id):
    """附件下载：需登录才能访问，不再直接暴露 media 静态目录"""
    attachment = get_object_or_404(TaskAttachment, pk=attachment_id)
    if not attachment.file:
        raise Http404
    return FileResponse(
        attachment.file.open('rb'),
        as_attachment=True,
        filename=attachment.file.name.rsplit('/', 1)[-1],
    )


@login_required
@require_POST
def checklist_item_add(request, task_id):
    """在任务下新增一条最小事项"""
    task = get_object_or_404(Task, pk=task_id)
    if not _check_task_permission(request.user, task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)
    name = request.POST.get('name', '').strip()
    if not name:
        return JsonResponse({'error': '事项名称不能为空'}, status=400)
    max_order = task.checklist_items.aggregate(m=models.Max('order'))['m'] or 0
    item = TaskChecklistItem.objects.create(task=task, name=name, order=max_order + 1)
    log_action(request.user, '添加清单事项', 'checklist_item', item.id,
               f'{task.product_stage.product.name} / {task.name} / {item.name}')
    return JsonResponse({'success': True, 'id': item.id, 'name': item.name})


@login_required
@require_POST
def checklist_item_toggle(request, item_id):
    """勾选/取消勾选一条最小事项"""
    item = get_object_or_404(TaskChecklistItem, pk=item_id)
    if not _check_task_permission(request.user, item.task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)
    item.mark_done(not item.is_done)
    return JsonResponse({
        'success': True,
        'is_done': item.is_done,
        'completed_at': item.completed_at.strftime('%Y-%m-%d %H:%M') if item.completed_at else None,
    })


@login_required
def task_checklist_modal(request, task_id):
    """返回单个任务的清单弹窗片段"""
    task = get_object_or_404(Task, pk=task_id)
    if not is_visible_to(task.product_stage.product, request.user):
        raise Http404('任务不存在或无查看权限')
    task.can_edit = _check_task_permission(request.user, task)
    return render(request, 'products/_task_checklist_modal.html', {
        'task': task,
    })


@login_required
@require_POST
def checklist_item_delete(request, item_id):
    """删除一条最小事项"""
    item = get_object_or_404(TaskChecklistItem, pk=item_id)
    if not _check_task_permission(request.user, item.task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)
    task = item.task
    item_name = item.name
    item.delete()
    log_action(request.user, '删除清单事项', 'checklist_item', item_id,
               f'{task.product_stage.product.name} / {task.name} / {item_name}')
    return JsonResponse({'success': True})


@login_required
@require_POST
def checklist_log_add(request, item_id):
    """在最小事项下追加一条文字日志"""
    item = get_object_or_404(TaskChecklistItem, pk=item_id)
    if not _check_task_permission(request.user, item.task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)
    content = request.POST.get('content', '').strip()
    if not content:
        return JsonResponse({'error': '日志内容不能为空'}, status=400)
    log = TaskChecklistLog.objects.create(item=item, user=request.user, content=content)
    log_action(request.user, '记录清单事项', 'checklist_item', item.id,
               f'{item.task.product_stage.product.name} / {item.task.name} / {item.name}',
               content[:100])
    return JsonResponse({
        'success': True,
        'id': log.id,
        'content': log.content,
        'user_name': request.user.first_name or request.user.username,
        'created_at': log.created_at.strftime('%Y-%m-%d %H:%M'),
    })


@login_required
@require_POST
def checklist_item_save_notes(request, item_id):
    """直接保存最小事项的填写内容（不追加日志，直接覆盖）"""
    item = get_object_or_404(TaskChecklistItem, pk=item_id)
    if not _check_task_permission(request.user, item.task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)
    item.notes = request.POST.get('notes', '').strip()
    item.is_done = bool(item.notes)
    item.save(update_fields=['notes', 'is_done'])
    log_action(request.user, '修改清单事项', 'checklist_item', item.id,
               f'{item.task.product_stage.product.name} / {item.task.name} / {item.name}',
               f'备注: {item.notes[:60]}')
    return JsonResponse({'success': True})


@login_required
@require_POST
def task_save_all_notes(request, task_id):
    """一次保存任务下所有最小事项的填写内容"""
    task = get_object_or_404(Task, pk=task_id)
    if not _check_task_permission(request.user, task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)
    updated_count = 0
    for item in task.checklist_items.all():
        key = f'notes_{item.pk}'
        if key in request.POST:
            item.notes = request.POST.get(key, '').strip()
            item.is_done = bool(item.notes)
            item.save(update_fields=['notes', 'is_done'])
            updated_count += 1
    if updated_count > 0:
        log_action(request.user, '修改清单事项', 'task', task.id,
                   f'{task.product_stage.product.name} / {task.name}',
                   f'批量保存 {updated_count} 条清单备注')
    return JsonResponse({'success': True})


@login_required
@require_POST
def stage_add_task(request, stage_id):
    """管理员/品负责人在阶段下新增临时任务"""
    stage = get_object_or_404(ProductStage, pk=stage_id)
    product = stage.product
    if not (request.user.profile.is_admin or product.assignee == request.user):
        return JsonResponse({'error': '无权限操作'}, status=403)
    name = request.POST.get('name', '').strip()
    if not name:
        return JsonResponse({'error': '任务名不能为空'}, status=400)
    # 计算排序号
    max_order = stage.tasks.aggregate(m=models.Max('order'))['m'] or 0
    task = Task.objects.create(
        product_stage=stage, name=name, order=max_order + 1,
        status='pending',
    )
    log_action(request.user, '添加任务', 'task', task.id,
               f'{stage.product.name} / {task.name}',
               f'在「{stage.name}」阶段新增子任务')
    task.sync_auto_todo()
    return JsonResponse({'success': True, 'task_id': task.id, 'name': task.name})


@login_required
@require_POST
def task_move(request, task_id, direction):
    """上下移动任务调整排序"""
    task = get_object_or_404(Task, pk=task_id)
    if not _check_task_permission(request.user, task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)

    stage = task.product_stage
    tasks = list(stage.tasks.order_by('order'))

    idx = next((i for i, t in enumerate(tasks) if t.pk == task_id), None)
    if idx is None:
        return JsonResponse({'error': '任务不存在'}, status=400)

    if direction == 'up' and idx > 0:
        task.order, tasks[idx - 1].order = tasks[idx - 1].order, task.order
        task.save(update_fields=['order'])
        tasks[idx - 1].save(update_fields=['order'])
    elif direction == 'down' and idx < len(tasks) - 1:
        task.order, tasks[idx + 1].order = tasks[idx + 1].order, task.order
        task.save(update_fields=['order'])
        tasks[idx + 1].save(update_fields=['order'])
    else:
        return JsonResponse({'success': True})  # 边界情况，不做移动

    log_action(request.user, '调整任务顺序', 'task', task.id,
               f'{stage.product.name} / {task.name}',
               f'顺序: {direction}')

    return JsonResponse({'success': True})


@login_required
@require_POST
def task_delete(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    product = task.product_stage.product
    if not (request.user.profile.is_admin or product.assignee == request.user):
        return JsonResponse({'error': '无权限操作'}, status=403)
    task_name = f'{task.product_stage.product.name} / {task.name}'
    task.delete()
    log_action(request.user, '删除任务', 'task', task_id, task_name,
               f'已删除子任务: {task_name}')
    return JsonResponse({'success': True})


@login_required
@require_POST
def stage_complete(request, stage_id):
    """部门负责人标记阶段完成，需校验所有Task已完成"""
    stage = get_object_or_404(ProductStage, pk=stage_id)
    product = stage.product
    # 权限校验：管理员、品负责人、或阶段所属部门成员
    can_operate = (
        request.user.profile.is_admin
        or product.assignee == request.user
        or stage.department == request.user.profile.department
    )
    if not can_operate:
        return JsonResponse({'error': '无权限操作此阶段'}, status=403)
    if not request.user.profile.is_admin and product.assignee != request.user:
        if stage.status != 'in_progress':
            return JsonResponse({'error': '只有进行中的阶段可以标记完成'}, status=400)
    if not stage.all_tasks_completed():
        return JsonResponse({'error': '该阶段还有未完成的任务'}, status=400)
    stage.complete()
    stage.sync_auto_todo()
    # stage.complete() 可能激活下一阶段（in_progress）或完结整个项目
    stage.product.refresh_from_db()
    for s in stage.product.stages.all():
        s.sync_auto_todo()
    stage.product.sync_auto_todo()
    log_action(request.user, '完成阶段', 'stage', stage.id,
               f'{stage.product.name} - {stage.name}',
               f'「{stage.name}」阶段所有任务已完成，标记为完成')
    return JsonResponse({'success': True, 'product_completed': stage.product.status == 'completed'})


@login_required
@require_POST
def stage_start(request, stage_id):
    """手动开始一个并行阶段"""
    stage = get_object_or_404(ProductStage, pk=stage_id)
    if not stage.allow_parallel:
        return JsonResponse({'error': '该阶段不支持手动开始'}, status=400)
    if stage.status != 'pending':
        return JsonResponse({'error': '该阶段不是待开始状态'}, status=400)
    if not stage.can_start():
        return JsonResponse({'error': '前置阶段尚未完成'}, status=400)

    # 权限校验：管理员、品负责人、或阶段所属部门成员
    product = stage.product
    can_operate = (
        request.user.profile.is_admin
        or product.assignee == request.user
        or stage.department == request.user.profile.department
    )
    if not can_operate:
        return JsonResponse({'error': '无权限操作此阶段'}, status=403)

    stage.status = 'in_progress'
    stage.started_at = timezone.now()
    stage.save()
    log_action(request.user, '开始阶段', 'stage', stage.id,
               f'{stage.product.name} - {stage.name}',
               f'手动开始并行阶段「{stage.name}」')
    return JsonResponse({'success': True})


@login_required
@require_POST
def stage_update_field(request, stage_id):
    """更新阶段字段（负责人、开始时间、预计结束）"""
    stage = get_object_or_404(ProductStage, pk=stage_id)
    product = stage.product
    # 管理员 / 品负责人 / 阶段负责人 都可编辑阶段字段
    if not (
        request.user.profile.is_admin
        or product.assignee == request.user
        or stage.assignee == request.user
    ):
        return JsonResponse({'error': '无权限操作'}, status=403)

    field = request.POST.get('field', '')
    value = request.POST.get('value', '')

    field_label_map = {
        'assignee': '负责人',
        'started_at': '开始时间',
        'expected_end_date': '预计结束日期',
    }
    old_val = None
    new_val = None
    old_assignee = stage.assignee  # 通知用

    if field == 'assignee':
        old_val = stage.assignee.username if stage.assignee else '无'
        if value:
            try:
                stage.assignee = User.objects.get(pk=int(value))
            except (User.DoesNotExist, ValueError):
                stage.assignee = None
        else:
            stage.assignee = None
        new_val = stage.assignee.username if stage.assignee else '无'
    elif field == 'started_at':
        old_val = stage.started_at.strftime('%Y-%m-%d') if stage.started_at else '无'
        new_started = _parse_dt(value)
        if new_started and stage.expected_end_date and new_started.date() > stage.expected_end_date:
            return JsonResponse({'error': '开始时间不能大于预计结束日期'}, status=400)
        # 校验：阶段开始时间必须在项目时间范围内
        if new_started and product.started_at and new_started < product.started_at:
            return JsonResponse({'error': '阶段开始时间不能早于项目开始时间'}, status=400)
        if new_started and product.expected_end_date and new_started.date() > product.expected_end_date:
            return JsonResponse({'error': '阶段开始时间不能晚于项目预计结束'}, status=400)
        stage.started_at = new_started
        new_val = stage.started_at.strftime('%Y-%m-%d') if stage.started_at else '无'
    elif field == 'expected_end_date':
        old_val = stage.expected_end_date.strftime('%Y-%m-%d') if stage.expected_end_date else '无'
        new_end = datetime.strptime(value, '%Y-%m-%d').date() if value else None
        if new_end and stage.started_at and new_end < stage.started_at.date():
            return JsonResponse({'error': '预计结束日期不能小于开始时间'}, status=400)
        # 校验：阶段结束不能超出项目时间范围
        if new_end and product.expected_end_date and new_end > product.expected_end_date:
            return JsonResponse({'error': '阶段预计结束不能晚于项目预计结束'}, status=400)
        if new_end and product.started_at and new_end < product.started_at.date():
            return JsonResponse({'error': '阶段预计结束不能早于项目开始时间'}, status=400)
        stage.expected_end_date = new_end
        new_val = stage.expected_end_date.strftime('%Y-%m-%d') if stage.expected_end_date else '无'
    else:
        return JsonResponse({'error': f'不支持的字段: {field}'}, status=400)

    stage.save()
    if field == 'assignee':
        _notify_new_assignee(stage, old_assignee, stage.assignee, request.user,
                             f'「{product.name} · {stage.name}」阶段')
    stage.sync_auto_todo()
    field_label = field_label_map.get(field, field)
    log_action(request.user, '修改阶段', 'stage', stage.id,
               f'{product.name} / {stage.name}',
               f'{field_label}: {old_val} → {new_val}')
    return JsonResponse({'success': True})


@login_required
@require_POST
def product_publish(request, pk):
    """负责人发布草稿，项目正式开始"""
    product = get_object_or_404(Product, pk=pk)
    if product.status != 'draft':
        return JsonResponse({'error': '只有草稿状态可以发布'}, status=400)
    if not (request.user.profile.is_admin or product.assignee == request.user):
        return JsonResponse({'error': '无权限操作'}, status=403)
    product.publish()
    product.sync_auto_todo()
    for stage in product.stages.all():
        stage.sync_auto_todo()
    log_action(request.user, '发布新品', 'product', product.id, product.name,
               '从草稿发布，项目正式开始')
    return JsonResponse({'success': True})


@login_required
@require_POST
def product_cancel(request, pk):
    """管理员/品负责人取消一个品"""
    product = get_object_or_404(Product, pk=pk)
    if not (request.user.profile.is_admin or product.assignee == request.user):
        return JsonResponse({'error': '无权限操作'}, status=403)
    if product.status != 'active':
        return JsonResponse({'error': '只能取消进行中的品'}, status=400)
    product.status = 'cancelled'
    product.save()
    product.sync_auto_todo()
    log_action(request.user, '取消品', 'product', product.id, product.name,
               f'状态: {product.get_status_display()} → 已取消')
    messages.success(request, f'"{product.name}" 已取消。')
    return redirect('kanban')


@login_required
@require_POST
def product_delete(request, pk):
    """管理员/品负责人彻底删除一个品（仅限草稿/已完成/已取消，级联删除全部阶段/任务/附件/日志）"""
    product = get_object_or_404(Product, pk=pk)
    if not (request.user.profile.is_admin or product.assignee == request.user):
        return JsonResponse({'error': '无权限操作'}, status=403)
    if product.status not in ('draft', 'completed', 'cancelled'):
        return JsonResponse({'error': '进行中的品不能删除，请先取消'}, status=400)
    name = product.name
    log_action(request.user, '删除品', 'product', product.id, name,
               f'已彻底删除（原状态: {product.get_status_display()}）')
    product.delete()
    return JsonResponse({'success': True})


@login_required
@require_POST
def task_update_field(request, task_id):
    """更新任务字段（开始时间、预计结束日期、负责人、截止日期）"""
    task = get_object_or_404(Task, pk=task_id)
    if not _check_task_permission(request.user, task):
        return JsonResponse({'error': '无权限操作此任务'}, status=403)

    # 开始时间和预计结束时间只有管理员/品负责人/阶段负责人能改
    time_fields = {'started_at', 'expected_end_date', 'deadline'}
    field = request.POST.get('field', '')
    if field in time_fields:
        product = task.product_stage.product
        stage = task.product_stage
        can_manage_time = (
            request.user.profile.is_admin
            or product.assignee == request.user
            or stage.assignee == request.user
        )
        if not can_manage_time:
            return JsonResponse({'error': '无权限修改时间字段'}, status=403)

    field = request.POST.get('field', '')
    value = request.POST.get('value', '')

    field_label_map = {
        'started_at': '开始时间',
        'expected_end_date': '预计结束日期',
        'deadline': '截止日期',
        'assignee': '负责人',
    }

    # 记录修改前的值
    old_val = None
    old_assignee = task.assignee  # 通知用
    stage = task.product_stage
    if field == 'started_at':
        old_val = task.started_at.strftime('%Y-%m-%d') if task.started_at else '无'
        new_started = _parse_dt(value)
        if new_started and task.expected_end_date and new_started.date() > task.expected_end_date:
            return JsonResponse({'error': '开始时间不能大于预计结束日期'}, status=400)
        # 校验：任务开始时间必须在阶段时间范围内
        if new_started and stage.started_at and new_started < stage.started_at:
            return JsonResponse({'error': '任务开始时间不能早于阶段开始时间'}, status=400)
        if new_started and stage.expected_end_date and new_started.date() > stage.expected_end_date:
            return JsonResponse({'error': '任务开始时间不能晚于阶段预计结束'}, status=400)
        task.started_at = new_started
        new_val = task.started_at.strftime('%Y-%m-%d') if task.started_at else '无'
    elif field == 'expected_end_date':
        old_val = task.expected_end_date.strftime('%Y-%m-%d') if task.expected_end_date else '无'
        new_end = datetime.strptime(value, '%Y-%m-%d').date() if value else None
        if new_end and task.started_at and new_end < task.started_at.date():
            return JsonResponse({'error': '预计结束日期不能小于开始时间'}, status=400)
        # 校验：任务结束不能超出阶段时间范围
        if new_end and stage.expected_end_date and new_end > stage.expected_end_date:
            return JsonResponse({'error': '任务预计结束不能晚于阶段预计结束'}, status=400)
        if new_end and stage.started_at and new_end < stage.started_at.date():
            return JsonResponse({'error': '任务预计结束不能早于阶段开始时间'}, status=400)
        task.expected_end_date = new_end
        new_val = task.expected_end_date.strftime('%Y-%m-%d') if task.expected_end_date else '无'
    elif field == 'deadline':
        old_val = task.deadline.strftime('%Y-%m-%d') if task.deadline else '无'
        task.deadline = datetime.strptime(value, '%Y-%m-%d').date() if value else None
        new_val = task.deadline.strftime('%Y-%m-%d') if task.deadline else '无'
    elif field == 'assignee':
        old_val = task.assignee.username if task.assignee else '无'
        if value:
            try:
                task.assignee = User.objects.get(pk=int(value))
            except (User.DoesNotExist, ValueError):
                task.assignee = None
        else:
            task.assignee = None
        new_val = task.assignee.username if task.assignee else '无'
    else:
        return JsonResponse({'error': f'不支持的字段: {field}'}, status=400)

    field_label = field_label_map.get(field, field)
    detail = f'{field_label}: {old_val} → {new_val}'

    task.save()
    if field == 'assignee':
        product = task.product_stage.product
        stage = task.product_stage
        _notify_new_assignee(task, old_assignee, task.assignee, request.user,
                             f'「{product.name} · {stage.name} · {task.name}」任务')
    # 时间字段变更后，状态可能随之改变（如设了已过期的预计结束日期），立即重算并一并返回
    task.update_status()
    task.sync_auto_todo()
    log_action(request.user, '修改任务', 'task', task.id,
               f'{task.product_stage.product.name} / {task.name}', detail)
    return JsonResponse({
        'success': True,
        'status': task.status,
        'status_display': task.get_status_display(),
    })


@login_required
@require_POST
def product_update_field(request, pk):
    """更新品字段（负责人、开始时间、预计结束日期）"""
    product = get_object_or_404(Product, pk=pk)
    if not (request.user.profile.is_admin or product.assignee == request.user):
        return JsonResponse({'error': '无权限操作'}, status=403)
    field = request.POST.get('field', '')
    value = request.POST.get('value', '')

    field_label_map = {
        'assignee': '负责人',
        'started_at': '开始时间',
        'expected_end_date': '预计结束日期',
    }

    old_val = None
    old_assignee = product.assignee  # 通知用
    if field == 'assignee':
        old_val = product.assignee.username if product.assignee else '无'
        if value:
            try:
                product.assignee = User.objects.get(pk=int(value))
            except (User.DoesNotExist, ValueError):
                product.assignee = None
        else:
            product.assignee = None
        new_val = product.assignee.username if product.assignee else '无'
    elif field == 'started_at':
        old_val = product.started_at.strftime('%Y-%m-%d') if product.started_at else '无'
        new_started = _parse_dt(value)
        if new_started and product.expected_end_date and new_started.date() > product.expected_end_date:
            return JsonResponse({'error': '开始时间不能大于预计结束日期'}, status=400)
        # 反向校验：项目开始时间收紧后，不能晚于任何一个阶段的开始时间
        if new_started:
            for st in product.stages.all():
                if st.started_at and st.started_at < new_started:
                    return JsonResponse({'error': f'项目开始时间不能晚于「{st.name}」阶段的开始时间'}, status=400)
        product.started_at = new_started
        new_val = product.started_at.strftime('%Y-%m-%d') if product.started_at else '无'
    elif field == 'expected_end_date':
        old_val = product.expected_end_date.strftime('%Y-%m-%d') if product.expected_end_date else '无'
        new_end = datetime.strptime(value, '%Y-%m-%d').date() if value else None
        if new_end and product.started_at and new_end < product.started_at.date():
            return JsonResponse({'error': '预计结束日期不能小于开始时间'}, status=400)
        # 反向校验：项目预计结束提前，不能早于任何一个阶段的预计结束时间
        if new_end:
            for st in product.stages.all():
                if st.expected_end_date and st.expected_end_date > new_end:
                    return JsonResponse({'error': f'项目预计结束不能早于「{st.name}」阶段的预计结束'}, status=400)
        product.expected_end_date = new_end
        new_val = product.expected_end_date.strftime('%Y-%m-%d') if product.expected_end_date else '无'
    else:
        return JsonResponse({'error': f'不支持的字段: {field}'}, status=400)

    field_label = field_label_map.get(field, field)
    detail = f'{field_label}: {old_val} → {new_val}'

    product.save()
    if field == 'assignee':
        _notify_new_assignee(product, old_assignee, product.assignee, request.user,
                             f'「{product.name}」项目总负责人')
    product.sync_auto_todo()
    log_action(request.user, '修改品', 'product', product.id, product.name, detail)
    return JsonResponse({'success': True})


@login_required
@require_POST
def product_update_profile(request, pk):
    """批量保存产品资料字段（管理员或品负责人）"""
    product = get_object_or_404(Product, pk=pk)
    if not product.can_be_managed_by(request.user):
        return JsonResponse({'error': '无权限操作'}, status=403)
    _apply_product_profile_fields(product, request.POST)
    product.save()
    log_action(request.user, '修改品', 'product', product.id, product.name, '更新产品资料')
    return JsonResponse({'success': True})
