import csv
from datetime import date, datetime, timedelta
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.utils import timezone as tz
from accounts.models import Announcement
from products.models import Product, visible_products_for, compute_task_status, compute_task_color
from products.views import _check_task_permission
from templates_app.models import StageTemplate


PROJECTS_PER_PAGE = 20


def _stage_icon(name):
    """按阶段名关键词返回 Bootstrap 图标名，无匹配用 bi-circle 兜底"""
    keyword_icons = [
        ('立项', 'bi-collection'),
        ('研发', 'bi-droplet'),
        ('配方', 'bi-droplet'),
        ('策划', 'bi-megaphone'),
        ('文案', 'bi-megaphone'),
        ('营销', 'bi-megaphone'),
        ('物料', 'bi-megaphone'),
        ('包装', 'bi-box-seam'),
        ('设计', 'bi-box-seam'),
        ('生产', 'bi-gear'),
        ('工厂', 'bi-gear'),
        ('报关', 'bi-truck'),
        ('入仓', 'bi-truck'),
        ('上架', 'bi-shop'),
        ('归档', 'bi-shop'),
    ]
    for keyword, icon in keyword_icons:
        if keyword in name:
            return icon
    return 'bi-circle'


@login_required
def kanban(request):
    """统一看板：通过状态筛选按钮和下拉筛选查看所有品"""
    # 获取筛选参数
    q = request.GET.get('q', '').strip()
    assignee_id = request.GET.get('assignee', '').strip()
    status_filter = request.GET.get('status', 'all')
    category_filter = request.GET.get('category', '').strip()
    time_type = request.GET.get('time_type', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    stage_filter = request.GET.get('stage', '').strip()

    # 基础查询集：先按可见权限过滤（管理员看全部，普通成员只看自己相关的）
    visible = visible_products_for(request.user)
    if status_filter == 'overdue':
        products = visible.filter(status='active')
    elif status_filter == 'all':
        products = visible
    elif status_filter in ('active', 'completed', 'cancelled', 'draft'):
        products = visible.filter(status=status_filter)
    else:
        status_filter = 'all'
        products = visible

    products = products.prefetch_related('stages__tasks')

    # 关键词模糊搜索：品名、产品名称、品牌、上架平台
    if q:
        products = products.filter(
            Q(name__icontains=q)
            | Q(product_name__icontains=q)
            | Q(brand__icontains=q)
            | Q(platforms__icontains=q)
        )

    # 按所属类目筛选
    if category_filter:
        products = products.filter(category=category_filter)

    # 按选定时间类型范围筛选
    if time_type == 'created' and (date_from or date_to):
        if date_from:
            try:
                d = datetime.strptime(date_from, '%Y-%m-%d').date()
                aware = tz.make_aware(datetime.combine(d, datetime.min.time()))
                products = products.filter(created_at__gte=aware)
            except ValueError:
                date_from = ''
        if date_to:
            try:
                d = datetime.strptime(date_to, '%Y-%m-%d').date()
                next_day = d + timedelta(days=1)
                aware = tz.make_aware(datetime.combine(next_day, datetime.min.time()))
                products = products.filter(created_at__lt=aware)
            except ValueError:
                date_to = ''

    # 按负责人筛选
    assignee_name = ''
    if assignee_id:
        try:
            assignee_id_int = int(assignee_id)
            products = products.filter(assignee_id=assignee_id_int)
            assignee_user = User.objects.filter(pk=assignee_id_int).first()
            if assignee_user:
                assignee_name = assignee_user.first_name or assignee_user.username
        except (ValueError, TypeError):
            assignee_id = ''

    # 获取阶段模板用于排序
    stage_templates = StageTemplate.objects.all()

    # 获取所有用户供负责人下拉列表
    all_users = User.objects.select_related('profile').order_by(
        '-is_active', 'first_name', 'username'
    )

    # 判断是否有筛选条件激活
    has_filter = bool(q or assignee_id or category_filter or date_from or date_to)

    # 构建统一的产品列表
    products_flat = []
    active_products = products.select_related(
        'assignee__profile__department'
    ).prefetch_related(
        'stages__department', 'stages__assignee__profile__department',
        'stages__tasks__assignee__profile',
    )

    for product in active_products:
        item = _build_product_item(request, product)
        if item:
            products_flat.append(item)

    # 异常筛选：只保留有超期任务的
    if status_filter == 'overdue':
        products_flat = [p for p in products_flat if p['has_overdue']]

    # 正常筛选：排除有超期任务的（异常在单独的筛选项里）
    if status_filter == 'active':
        products_flat = [p for p in products_flat if not p['has_overdue']]

    # KPI 阶段筛选：点击 KPI 卡片跳转过来时只显示当前处于该阶段的活跃项目
    if stage_filter:
        def _matches_stage(p):
            if p.get('product_status') != 'active':
                return False
            for s in p.get('all_stages', []):
                if s.get('name') == stage_filter and s.get('status') == 'in_progress':
                    return True
            return False
        products_flat = [p for p in products_flat if _matches_stage(p)]

    # 按阶段顺序排序
    stage_order = {st.order: i for i, st in enumerate(stage_templates)}
    products_flat.sort(key=lambda p: stage_order.get(p.get('_stage_order', 0), 999))

    # KPI 统计：按阶段模板动态生成卡片，统计当前处于该阶段的活跃项目数
    kpi_total = visible_products_for(request.user).count()
    stage_templates_ordered = list(StageTemplate.objects.order_by('order'))
    stage_counts = {st.name: 0 for st in stage_templates_ordered}
    for p in products_flat:
        if p.get('product_status') == 'active':
            for s in p.get('all_stages', []):
                if s.get('status') in ('in_progress', 'overdue') and s['name'] in stage_counts:
                    stage_counts[s['name']] += 1
    stage_infos = [
        {
            'name': st.name,
            'count': stage_counts.get(st.name, 0),
            'icon': _stage_icon(st.name),
        }
        for st in stage_templates_ordered
    ]

    # 分页：每页7条
    page_num = request.GET.get('page', '1')
    paginator = Paginator(products_flat, PROJECTS_PER_PAGE)
    page_obj = paginator.get_page(page_num)

    return render(request, 'dashboard/kanban.html', {
        'products': page_obj,
        'page_obj': page_obj,
        'has_products': bool(products_flat),
        'all_users': all_users,
        'search_q': q,
        'filter_assignee': assignee_id,
        'filter_assignee_name': assignee_name,
        'filter_category': category_filter,
        'filter_time_type': time_type,
        'filter_date_from': date_from,
        'filter_date_to': date_to,
        'has_filter': has_filter,
        'status_filter': status_filter,
        'today': date.today(),
        'kpi_total': kpi_total,
        'stage_counts': stage_counts,
        'stage_infos': stage_infos,
        'user_todos': request.user.todos.filter(is_done=False).order_by('due_at', '-created_at')[:5],
        'announcements': Announcement.objects.filter(is_active=True).select_related('created_by')[:5],
    })


def _build_product_item(request, product):
    """构建统一的看板行数据，适用于所有状态的产品"""
    can_message_product = (
        product.assignee_id and product.assignee_id != request.user.id
        and product.can_be_managed_by(request.user)
    )

    current = product.get_current_stage()
    can_message_stage = False
    if current:
        can_message_stage = (
            current.assignee_id and current.assignee_id != request.user.id
            and current.can_be_managed_by(request.user)
        )

    all_tasks_total = 0
    all_tasks_done = 0
    all_overdue_tasks = []
    all_stages = []
    today_val = date.today()

    def _task_effective_status(t):
        """任务有效状态：复用 compute_task_status，与 Task.update_status() 共用同一判定口径，
        避免看板和详情页显示的状态不一致。"""
        return compute_task_status(t, today=today_val)

    def _stage_effective_status(s, task_statuses):
        """阶段状态由子任务聚合"""
        if not task_statuses:
            return s.status  # 无子任务时用数据库存的状态
        if any(st == 'overdue' for st in task_statuses):
            return 'overdue'
        if all(st == 'completed' for st in task_statuses):
            return 'completed'
        if any(st == 'in_progress' for st in task_statuses):
            return 'in_progress'
        return 'pending'

    def _stage_effective_color(s_tasks, task_colors):
        """阶段五色规则：与 compute_task_color 共用同一套颜色，按聚合优先级判定：
        有超期(red) > 有进行中(white) > 全部完成且有超期完成(yellow) > 全部完成(green) > 未开始(gray)"""
        if not task_colors:
            return 'gray'
        if 'red' in task_colors:
            return 'red'
        if 'white' in task_colors:
            return 'white'
        if all(c in ('green', 'yellow') for c in task_colors):
            return 'yellow' if 'yellow' in task_colors else 'green'
        return 'gray'

    for s in product.stages.all():
        s_tasks = list(s.tasks.all())
        task_effective = [_task_effective_status(t) for t in s_tasks]
        task_colors = [compute_task_color(t) for t in s_tasks]
        all_tasks_total += len(s_tasks)
        all_tasks_done += sum(1 for st in task_effective if st == 'completed')
        stage_overdue_tasks = [t for t, st in zip(s_tasks, task_effective) if st == 'overdue']
        all_overdue_tasks.extend(stage_overdue_tasks)
        stage_status = _stage_effective_status(s, task_effective)
        stage_color = _stage_effective_color(s_tasks, task_colors)
        stage_done = sum(1 for st in task_effective if st == 'completed')
        all_stages.append({
            'id': s.id,
            'name': s.name,
            'order': s.order,
            'status': stage_status,
            'color': stage_color,
            'is_current': current and s.id == current.id,
            'allow_parallel': s.allow_parallel,
            'has_overdue': stage_status == 'overdue',
            'tasks_done': stage_done,
            'tasks_total': len(s_tasks),
        })

    progress_pct = int(all_tasks_done / all_tasks_total * 100) if all_tasks_total > 0 else 0

    # 卡点：全部超期任务
    overdue_tasks = [
        {
            'name': t.name,
            'assignee': (
                t.assignee.first_name or t.assignee.username
                if t.assignee else '未指定'
            ),
            'task_id': t.id,
            'can_message': (
                bool(t.assignee_id) and t.assignee_id != request.user.id
                and _check_task_permission(request.user, t)
            ),
            'stage_name': t.product_stage.name,
        }
        for t in all_overdue_tasks
    ]

    # 时间计算
    started = product.started_at
    if started:
        started = tz.localtime(started)  # 先转本地时区再取日期，与模板 date 过滤器保持一致
    days_remain = ''
    project_overdue = False
    if product.expected_end_date and product.status == 'active':
        remain = (product.expected_end_date - date.today()).days
        days_remain = f'剩{remain}天' if remain >= 0 else f'超{-remain}天'
        if remain < 0:
            project_overdue = True

    # 状态判定（仅 active 项目有异常/待推进/正常判定）
    has_overdue = len(all_overdue_tasks) > 0 or (product.status == 'active' and project_overdue)
    if product.status == 'active':
        if has_overdue:
            status_label = '已超期'
            status_class = 'overdue'
            block_reason = ''
            block_person = ''
        elif all_tasks_done == all_tasks_total and all_tasks_total > 0:
            status_label = '待推进'
            status_class = 'in_progress'
            block_reason = '全部任务完成，等待标记阶段'
            block_person = current.assignee.first_name or current.assignee.username if current and current.assignee else '未指定'
        else:
            status_label = '进行中'
            status_class = 'completed'
            block_reason = ''
            block_person = ''
    elif product.status == 'draft':
        status_label = '待开始'
        status_class = 'pending'
        block_reason = ''
        block_person = ''
    elif product.status == 'completed':
        status_label = '已完成'
        status_class = 'completed'
        block_reason = ''
        block_person = ''
    else:
        status_label = '已取消'
        status_class = 'pending'
        block_reason = ''
        block_person = ''

    # 优先级：根据剩余天数判定
    if product.status == 'active' and product.expected_end_date:
        remain_days = (product.expected_end_date - date.today()).days
        if remain_days < 10:
            priority = 'high'
        elif remain_days < 31:
            priority = 'mid'
        else:
            priority = 'low'
    else:
        priority = 'low'

    return {
        'id': product.id,
        'name': product.name,
        'product_assignee': (
            product.assignee.first_name
            or product.assignee.username
            if product.assignee else ''
        ),
        'product_assignee_inactive': bool(product.assignee and not product.assignee.is_active),
        'can_message_product': bool(can_message_product),
        'stage_id': current.id if current else 0,
        'can_message_stage': bool(can_message_stage),
        'progress_pct': progress_pct,
        'all_tasks_done': all_tasks_done,
        'all_tasks_total': all_tasks_total,
        'all_stages': all_stages,
        'overdue_tasks': overdue_tasks,
        'start_date': started.date() if started else None,
        'days_remain': days_remain,
        'expected_end_date': product.expected_end_date,
        'priority': priority,
        'status_label': status_label,
        'status_class': status_class,
        'has_overdue': has_overdue,
        'block_reason': block_reason,
        'block_person': block_person,
        'can_delete': product.status != 'active' and product.can_be_managed_by(request.user),
        'product_status': product.status,
        '_stage_order': current.order if current else 0,
    }


@login_required
def export_csv(request):
    """导出项目为 CSV：可按状态导出，或按单个 product 导出。
    先按可见性过滤（管理员看全部，普通成员只看自己相关的），再按其他条件收窄。"""
    product_id = request.GET.get('product', '').strip()
    status_filter = request.GET.get('status', 'completed')

    # 先按可见性过滤——防止旁路
    visible = visible_products_for(request.user)

    if product_id:
        # 单项目导出：不限状态
        products = visible.filter(pk=product_id).select_related(
            'creator', 'assignee'
        ).prefetch_related('stages__tasks__assignee')
    else:
        products = visible.filter(
            status__in=['completed', 'cancelled']
        ).select_related('creator', 'assignee').prefetch_related(
            'stages__tasks__assignee'
        ).order_by('-created_at')

        if status_filter in ('completed', 'cancelled'):
            products = products.filter(status=status_filter)

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = f'attachment; filename="开品项目导出_{date.today()}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        '品名', '状态', '品负责人', '创建人', '创建时间', '开始时间', '预计结束', '实际结束',
        '阶段名', '阶段状态', '阶段负责人', '阶段开始', '阶段预计结束', '阶段实际结束',
        '子任务名', '任务负责人', '任务状态', '任务开始', '任务预计结束', '任务实际结束', '截止日期',
    ])

    for product in products:
        for stage in product.stages.all():
            for task in stage.tasks.all():
                writer.writerow([
                    product.name,
                    product.get_status_display(),
                    product.assignee.first_name or product.assignee.username if product.assignee else '',
                    product.creator.first_name or product.creator.username if product.creator else '',
                    product.created_at.strftime('%Y-%m-%d %H:%M'),
                    product.started_at.strftime('%Y-%m-%d %H:%M') if product.started_at else '',
                    product.expected_end_date.strftime('%Y-%m-%d') if product.expected_end_date else '',
                    product.actual_end_date.strftime('%Y-%m-%d %H:%M') if product.actual_end_date else '',
                    stage.name,
                    stage.get_status_display(),
                    stage.assignee.first_name or stage.assignee.username if stage.assignee else '',
                    stage.started_at.strftime('%Y-%m-%d %H:%M') if stage.started_at else '',
                    stage.expected_end_date.strftime('%Y-%m-%d') if stage.expected_end_date else '',
                    stage.completed_at.strftime('%Y-%m-%d %H:%M') if stage.completed_at else '',
                    task.name,
                    task.assignee.first_name or task.assignee.username if task.assignee else '',
                    task.get_status_display(),
                    task.started_at.strftime('%Y-%m-%d %H:%M') if task.started_at else '',
                    task.expected_end_date.strftime('%Y-%m-%d') if task.expected_end_date else '',
                    task.actual_end_date.strftime('%Y-%m-%d %H:%M') if task.actual_end_date else '',
                    task.deadline.strftime('%Y-%m-%d') if task.deadline else '',
                ])

    return response
