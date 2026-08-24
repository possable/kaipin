from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.models import User
from accounts.decorators import admin_required
from .models import StageTemplate, TaskTemplate, ChecklistItemTemplate
from accounts.models import Department


def _get_stage_post_data(request):
    """从 POST 中提取并验证阶段表单数据"""
    name = request.POST.get('name', '').strip()
    order_str = request.POST.get('order', '').strip()
    dept_id_str = request.POST.get('department', '').strip()
    assignee_id_str = request.POST.get('default_assignee', '').strip()

    if not name:
        return None, '阶段名称不能为空'
    if not order_str or not order_str.isdigit():
        return None, '请输入有效的顺序号'
    if not dept_id_str or not dept_id_str.isdigit():
        return None, '请选择负责部门'

    try:
        department = Department.objects.get(id=int(dept_id_str))
    except Department.DoesNotExist:
        return None, '所选部门不存在'

    default_assignee = None
    if assignee_id_str:
        try:
            default_assignee = User.objects.get(pk=int(assignee_id_str))
        except (User.DoesNotExist, ValueError):
            pass

    return {
        'name': name,
        'order': int(order_str),
        'department': department,
        'allow_parallel': request.POST.get('allow_parallel') == 'on',
        'default_assignee': default_assignee,
    }, None


def _get_task_post_data(request):
    """从 POST 中提取并验证任务表单数据（含 is_milestone）"""
    name = request.POST.get('name', '').strip()
    order_str = request.POST.get('order', '0').strip()
    is_milestone = 'is_milestone' in request.POST

    if not name:
        return None, '任务名称不能为空'
    order = int(order_str) if order_str.isdigit() else 0
    return {'name': name, 'order': order, 'is_milestone': is_milestone}, None


def _get_checklist_post_data(request):
    """从 POST 中提取并验证最小事项模板表单数据（含 is_milestone）"""
    name = request.POST.get('name', '').strip()
    order_str = request.POST.get('order', '0').strip()
    is_milestone = 'is_milestone' in request.POST

    if not name:
        return None, '事项名称不能为空'
    order = int(order_str) if order_str.isdigit() else 0
    return {'name': name, 'order': order, 'is_milestone': is_milestone}, None


@admin_required
def stage_template_list(request):
    stages = StageTemplate.objects.all()
    return render(request, 'templates_app/stage_template_list.html', {'stages': stages})


@admin_required
def stage_template_create(request):
    if request.method == 'POST':
        data, error = _get_stage_post_data(request)
        if error:
            messages.error(request, error)
        else:
            StageTemplate.objects.create(**data)
            messages.success(request, f'阶段 "{data["name"]}" 已添加。')
            return redirect('stage_template_list')
    departments = Department.objects.all()
    all_users = User.objects.select_related('profile').order_by('first_name', 'username')
    return render(request, 'templates_app/stage_template_form.html', {
        'departments': departments,
        'all_users': all_users,
        'action': '添加',
    })


@admin_required
def stage_template_edit(request, pk):
    stage = get_object_or_404(StageTemplate, pk=pk)
    if request.method == 'POST':
        data, error = _get_stage_post_data(request)
        if error:
            messages.error(request, error)
        else:
            stage.name = data['name']
            stage.order = data['order']
            stage.department = data['department']
            stage.allow_parallel = data['allow_parallel']
            stage.default_assignee = data['default_assignee']
            stage.save()
            messages.success(request, f'阶段 "{stage.name}" 已更新。')
            return redirect('stage_template_list')
    departments = Department.objects.all()
    all_users = User.objects.select_related('profile').order_by('first_name', 'username')
    return render(request, 'templates_app/stage_template_form.html', {
        'stage': stage,
        'departments': departments,
        'all_users': all_users,
        'action': '编辑',
    })


@admin_required
def stage_template_delete(request, pk):
    stage = get_object_or_404(StageTemplate, pk=pk)
    if request.method == 'POST':
        name = stage.name
        stage.delete()
        messages.success(request, f'阶段 "{name}" 已删除。')
        return redirect('stage_template_list')
    return render(request, 'templates_app/stage_template_form.html', {
        'stage': stage,
        'action': '删除确认',
    })


@admin_required
def task_template_list(request, stage_pk):
    stage = get_object_or_404(StageTemplate, pk=stage_pk)
    tasks = stage.task_templates.all()
    return render(request, 'templates_app/task_template_list.html', {
        'stage': stage,
        'tasks': tasks,
    })


@admin_required
def task_template_create(request, stage_pk):
    stage = get_object_or_404(StageTemplate, pk=stage_pk)
    if request.method == 'POST':
        data, error = _get_task_post_data(request)
        if error:
            messages.error(request, error)
        else:
            TaskTemplate.objects.create(stage_template=stage, **data)
            messages.success(request, f'子任务 "{data["name"]}" 已添加。')
            return redirect('task_template_list', stage_pk=stage.pk)
    return render(request, 'templates_app/task_template_form.html', {
        'stage': stage,
        'action': '添加',
    })


@admin_required
def task_template_edit(request, pk):
    task = get_object_or_404(TaskTemplate, pk=pk)
    if request.method == 'POST':
        data, error = _get_task_post_data(request)
        if error:
            messages.error(request, error)
        else:
            task.name = data['name']
            task.order = data['order']
            task.is_milestone = data['is_milestone']
            task.save()
            messages.success(request, f'子任务 "{task.name}" 已更新。')
            return redirect('task_template_list', stage_pk=task.stage_template.pk)
    return render(request, 'templates_app/task_template_form.html', {
        'task': task,
        'stage': task.stage_template,
        'action': '编辑',
    })


@admin_required
def task_template_delete(request, pk):
    task = get_object_or_404(TaskTemplate, pk=pk)
    stage_pk = task.stage_template.pk
    if request.method == 'POST':
        name = task.name
        task.delete()
        messages.success(request, f'子任务 "{name}" 已删除。')
        return redirect('task_template_list', stage_pk=stage_pk)
    return render(request, 'templates_app/task_template_form.html', {
        'task': task,
        'stage': task.stage_template,
        'action': '删除确认',
    })


@admin_required
def checklist_template_list(request, task_pk):
    task = get_object_or_404(TaskTemplate, pk=task_pk)
    items = task.checklist_item_templates.all()
    return render(request, 'templates_app/checklist_template_list.html', {
        'task': task,
        'stage': task.stage_template,
        'items': items,
    })


@admin_required
def checklist_template_create(request, task_pk):
    task = get_object_or_404(TaskTemplate, pk=task_pk)
    if request.method == 'POST':
        data, error = _get_checklist_post_data(request)
        if error:
            messages.error(request, error)
        else:
            ChecklistItemTemplate.objects.create(task_template=task, **data)
            messages.success(request, f'最小事项 "{data["name"]}" 已添加。')
            return redirect('checklist_template_list', task_pk=task.pk)
    return render(request, 'templates_app/checklist_template_form.html', {
        'task': task,
        'stage': task.stage_template,
        'action': '添加',
    })


@admin_required
def checklist_template_edit(request, pk):
    item = get_object_or_404(ChecklistItemTemplate, pk=pk)
    if request.method == 'POST':
        data, error = _get_checklist_post_data(request)
        if error:
            messages.error(request, error)
        else:
            item.name = data['name']
            item.order = data['order']
            item.is_milestone = data['is_milestone']
            item.save()
            messages.success(request, f'最小事项 "{item.name}" 已更新。')
            return redirect('checklist_template_list', task_pk=item.task_template.pk)
    return render(request, 'templates_app/checklist_template_form.html', {
        'item': item,
        'task': item.task_template,
        'stage': item.task_template.stage_template,
        'action': '编辑',
    })


@admin_required
def checklist_template_delete(request, pk):
    item = get_object_or_404(ChecklistItemTemplate, pk=pk)
    task_pk = item.task_template.pk
    if request.method == 'POST':
        name = item.name
        item.delete()
        messages.success(request, f'最小事项 "{name}" 已删除。')
        return redirect('checklist_template_list', task_pk=task_pk)
    return render(request, 'templates_app/checklist_template_form.html', {
        'item': item,
        'task': item.task_template,
        'stage': item.task_template.stage_template,
        'action': '删除确认',
    })
