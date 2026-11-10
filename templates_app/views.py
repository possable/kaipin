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


def _task_template_would_cycle(task, predecessors):
    """把 predecessors 都设为 task 的前置模板，是否会形成环。"""
    seen = set()
    stack = list(predecessors)
    while stack:
        node = stack.pop()
        if node.pk == task.pk:
            return True
        if node.pk in seen:
            continue
        seen.add(node.pk)
        stack.extend(node.depends_on.all())
    return False


def _task_predecessor_groups(task=None):
    """子任务模板的前置候选，按阶段分组。

    前置任务可以跨阶段、可以多选；编辑时排除自身和会形成环的候选。
    新建时还没有自身，所有已有模板都可以选。
    """
    all_tasks = list(
        TaskTemplate.objects.select_related('stage_template')
        .prefetch_related('depends_on')
        .order_by('stage_template__order', 'order', 'id')
    )
    dependency_map = {
        t.pk: [p.pk for p in t.depends_on.all()] for t in all_tasks
    }

    def _would_cycle(candidate):
        if task is None:
            return False
        seen = set()
        stack = [candidate.pk]
        while stack:
            node = stack.pop()
            if node == task.pk:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(dependency_map.get(node, []))
        return False

    groups = {}
    for candidate in all_tasks:
        if task is not None and (
            candidate.pk == task.pk or _would_cycle(candidate)
        ):
            continue
        groups.setdefault(candidate.stage_template_id, []).append(candidate)

    return [
        {'stage': candidates[0].stage_template, 'tasks': candidates}
        for candidates in groups.values()
    ]


def _get_task_post_data(request, task=None):
    """从 POST 中提取并验证任务表单数据（含 is_milestone、前置任务）"""
    name = request.POST.get('name', '').strip()
    order_str = request.POST.get('order', '0').strip()
    is_milestone = 'is_milestone' in request.POST
    # 多选；也兼容只传一个值的旧表单
    depends_on_ids = request.POST.getlist('depends_on')

    if not name:
        return None, '任务名称不能为空'
    order = int(order_str) if order_str.isdigit() else 0

    depends_on = []
    seen_ids = set()
    for depends_on_id in depends_on_ids:
        depends_on_id = depends_on_id.strip()
        if not depends_on_id:
            continue
        try:
            predecessor = TaskTemplate.objects.prefetch_related('depends_on').get(
                pk=int(depends_on_id),
            )
        except (TaskTemplate.DoesNotExist, ValueError):
            return None, '所选前置任务不存在'
        if predecessor.pk in seen_ids:
            continue
        if task is not None:
            if predecessor.pk == task.pk:
                return None, '前置任务不能是任务自身'
            if _task_template_would_cycle(task, [predecessor]):
                return None, (
                    f'「{predecessor.name}」已直接或间接依赖此任务，不能设为前置任务'
                )
        seen_ids.add(predecessor.pk)
        depends_on.append(predecessor)

    return {
        'name': name,
        'order': order,
        'is_milestone': is_milestone,
        'depends_on': depends_on,
    }, None


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
    all_users = User.objects.filter(is_active=True).select_related('profile').order_by(
        'first_name', 'username'
    )
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
    all_users = User.objects.filter(is_active=True).select_related('profile').order_by(
        'first_name', 'username'
    )
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
    tasks = stage.task_templates.prefetch_related(
        'depends_on__stage_template',
    ).all()
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
            depends_on = data.pop('depends_on')
            task = TaskTemplate.objects.create(stage_template=stage, **data)
            task.depends_on.set(depends_on)
            messages.success(request, f'子任务 "{data["name"]}" 已添加。')
            return redirect('task_template_list', stage_pk=stage.pk)
    return render(request, 'templates_app/task_template_form.html', {
        'stage': stage,
        'predecessor_groups': _task_predecessor_groups(),
        'selected_predecessor_ids': set(),
        'action': '添加',
    })


@admin_required
def task_template_edit(request, pk):
    task = get_object_or_404(TaskTemplate, pk=pk)
    if request.method == 'POST':
        data, error = _get_task_post_data(request, task=task)
        if error:
            messages.error(request, error)
        else:
            depends_on = data.pop('depends_on')
            task.name = data['name']
            task.order = data['order']
            task.is_milestone = data['is_milestone']
            task.save()
            task.depends_on.set(depends_on)
            messages.success(request, f'子任务 "{task.name}" 已更新。')
            return redirect('task_template_list', stage_pk=task.stage_template.pk)
    return render(request, 'templates_app/task_template_form.html', {
        'task': task,
        'stage': task.stage_template,
        'predecessor_groups': _task_predecessor_groups(task),
        'selected_predecessor_ids': set(
            task.depends_on.values_list('pk', flat=True)
        ),
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
