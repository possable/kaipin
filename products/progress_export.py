from io import BytesIO
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont


BACKGROUND = '#06131d'
PANEL = '#0a1b26'
PANEL_SOFT = '#0b202c'
TEXT = '#e8f3ee'
MUTED = '#8297a2'
LINE = '#284454'
STATUS_COLORS = {
    'completed': '#39d98a',
    'in_progress': '#48b9f5',
    'overdue': '#ff6666',
    'pending': '#82939c',
}
REPORT_BACKGROUND = '#06131d'
REPORT_PANEL = '#0a1b26'
REPORT_TEXT = '#e8f3ee'
REPORT_MUTED = '#8297a2'
REPORT_LINE = '#284454'
PROCESS_MAP_BACKGROUND = '#04111a'
PROCESS_STAGE_PALETTES = [
    ('#071923', '#0a1f2a'),
    ('#071923', '#0a1f2a'),
    ('#071923', '#0a1f2a'),
    ('#071923', '#0a1f2a'),
    ('#071923', '#0a1f2a'),
    ('#071923', '#0a1f2a'),
    ('#071923', '#0a1f2a'),
    ('#071923', '#0a1f2a'),
]


def _font_path(bold=False):
    names = (
        ['C:/Windows/Fonts/msyhbd.ttc', 'C:/Windows/Fonts/simhei.ttf']
        if bold else
        ['C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simsun.ttc']
    )
    names += [
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'
        if bold else '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
        if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    return next((path for path in names if Path(path).exists()), None)


def _font(size, bold=False):
    path = _font_path(bold=bold)
    return ImageFont.truetype(path, size) if path else ImageFont.load_default()


def _text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap_text(draw, text, font, max_width, max_lines=2):
    text = str(text or '')
    if not text:
        return ['--']

    lines = []
    current = ''
    for char in text:
        candidate = current + char
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = char
            if len(lines) == max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current)

    consumed = ''.join(lines)
    if len(consumed) < len(text):
        last = lines[-1]
        while last and _text_width(draw, last + '…', font) > max_width:
            last = last[:-1]
        lines[-1] = last + '…'
    return lines or ['--']


def _draw_centered_lines(draw, lines, box, font, fill, line_gap=3):
    x1, y1, x2, y2 = box
    line_heights = []
    for line in lines:
        bounds = draw.textbbox((0, 0), line, font=font)
        line_heights.append(bounds[3] - bounds[1])
    total_height = sum(line_heights) + line_gap * max(0, len(lines) - 1)
    y = y1 + (y2 - y1 - total_height) / 2
    for line, line_height in zip(lines, line_heights):
        width = _text_width(draw, line, font)
        draw.text((x1 + (x2 - x1 - width) / 2, y), line, font=font, fill=fill)
        y += line_height + line_gap


def _draw_node(draw, box, name, status_key, font, task=False):
    x1, y1, x2, y2 = box
    color = STATUS_COLORS.get(status_key, STATUS_COLORS['pending'])
    fill = PANEL_SOFT if task else PANEL
    draw.rounded_rectangle(box, radius=10 if task else 13, fill=fill, outline=color, width=2)
    dot_radius = 5 if task else 7
    dot_x = x1 + 17
    dot_y = (y1 + y2) / 2
    draw.ellipse(
        (dot_x - dot_radius, dot_y - dot_radius, dot_x + dot_radius, dot_y + dot_radius),
        fill=color,
    )
    lines = _wrap_text(draw, name, font, max(24, x2 - x1 - 48), max_lines=2)
    _draw_centered_lines(draw, lines, (x1 + 34, y1 + 4, x2 - 8, y2 - 4), font, color)


def _format_date(value, empty='未设置'):
    if not value:
        return empty
    return timezone_local(value).strftime('%Y-%m-%d')


def timezone_local(value):
    from datetime import datetime

    try:
        from django.utils import timezone

        return timezone.localtime(value) if isinstance(value, datetime) and timezone.is_aware(value) else value
    except (TypeError, ValueError):
        return value


def _render_legacy_product_progress_png(product, overview):
    stages = overview['stages']
    stage_count = max(1, len(stages))
    max_tasks = max((len(stage['tasks']) for stage in stages), default=1)

    margin = 48
    stage_gap = 16
    desired_stage_width = 205
    width = max(1600, margin * 2 + stage_count * desired_stage_width + (stage_count - 1) * stage_gap)
    stage_width = (width - margin * 2 - (stage_count - 1) * stage_gap) / stage_count

    task_height = 52
    task_gap = 10
    task_start_y = 420
    height = max(920, task_start_y + max_tasks * (task_height + task_gap) + 100)

    image = Image.new('RGB', (int(width), int(height)), BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = _font(31, bold=True)
    heading_font = _font(17, bold=True)
    value_font = _font(15, bold=True)
    label_font = _font(12)
    stage_font = _font(16, bold=True)
    task_font = _font(13, bold=True)

    draw.text((margin, 30), f'{product.name} · 项目进度总览', font=title_font, fill=TEXT)
    draw.text(
        (margin, 78),
        f"{overview['status_label']}  ·  当前推进：{overview['active_stage_text']}",
        font=label_font,
        fill=MUTED,
    )

    info_values = [
        ('负责人', (product.assignee.first_name or product.assignee.username) if product.assignee else '未指定'),
        ('开始时间', _format_date(product.started_at)),
        ('预计结束', _format_date(product.expected_end_date)),
        ('实际结束', _format_date(product.actual_end_date, empty='未完成')),
        ('创建时间', _format_date(product.created_at)),
    ]
    info_width = 820
    info_x = width - margin - info_width
    info_y = 24
    info_height = 78
    item_width = info_width / len(info_values)
    draw.rounded_rectangle(
        (info_x, info_y, info_x + info_width, info_y + info_height),
        radius=12,
        fill=PANEL,
        outline=LINE,
        width=1,
    )
    for index, (label, value) in enumerate(info_values):
        x1 = info_x + index * item_width
        if index:
            draw.line((x1, info_y + 12, x1, info_y + info_height - 12), fill=LINE, width=1)
        draw.text((x1 + 12, info_y + 12), label, font=label_font, fill=MUTED)
        value_lines = _wrap_text(draw, value, value_font, item_width - 24, max_lines=1)
        draw.text((x1 + 12, info_y + 40), value_lines[0], font=value_font, fill=TEXT)

    stats = [
        ('整体任务完成率', f"{overview['progress_pct']}%"),
        ('阶段完成', f"{overview['completed_stages']} / {overview['stage_count']}"),
        ('任务完成', f"{overview['completed_tasks']} / {overview['total_tasks']}"),
        ('超期任务', str(overview['overdue_tasks'])),
        ('交付时间', overview['remaining_text']),
    ]
    stats_y = 125
    stats_width = (width - margin * 2) / len(stats)
    draw.rounded_rectangle(
        (margin, stats_y, width - margin, stats_y + 70),
        radius=10,
        fill=PANEL,
        outline=LINE,
        width=1,
    )
    for index, (label, value) in enumerate(stats):
        x1 = margin + index * stats_width
        if index:
            draw.line((x1, stats_y + 12, x1, stats_y + 58), fill=LINE, width=1)
        draw.text((x1 + 14, stats_y + 11), label, font=label_font, fill=MUTED)
        value_color = STATUS_COLORS['overdue'] if label == '超期任务' and overview['overdue_tasks'] else TEXT
        draw.text((x1 + 14, stats_y + 37), value, font=value_font, fill=value_color)

    root_width = 330
    root_height = 58
    root_x = width / 2 - root_width / 2
    root_y = 222
    draw.rounded_rectangle(
        (root_x, root_y, root_x + root_width, root_y + root_height),
        radius=13,
        fill=PANEL,
        outline=STATUS_COLORS['completed'],
        width=2,
    )
    root_lines = _wrap_text(draw, product.name, heading_font, root_width - 30, max_lines=1)
    _draw_centered_lines(
        draw,
        root_lines,
        (root_x + 12, root_y + 5, root_x + root_width - 12, root_y + root_height - 5),
        heading_font,
        TEXT,
    )

    branch_y = 315
    stage_y = 335
    stage_height = 62
    centers = [margin + index * (stage_width + stage_gap) + stage_width / 2 for index in range(stage_count)]
    draw.line((width / 2, root_y + root_height, width / 2, branch_y), fill=LINE, width=3)
    if centers:
        draw.line((centers[0], branch_y, centers[-1], branch_y), fill=LINE, width=3)

    for index, stage in enumerate(stages):
        x1 = margin + index * (stage_width + stage_gap)
        x2 = x1 + stage_width
        center_x = (x1 + x2) / 2
        draw.line((center_x, branch_y, center_x, stage_y), fill=LINE, width=3)
        _draw_node(
            draw,
            (x1, stage_y, x2, stage_y + stage_height),
            stage['name'],
            stage['status_key'],
            stage_font,
        )

        tasks = stage['tasks']
        previous_bottom = stage_y + stage_height
        for task_index, task in enumerate(tasks):
            task_y = task_start_y + task_index * (task_height + task_gap)
            draw.line((center_x, previous_bottom, center_x, task_y), fill=LINE, width=2)
            _draw_node(
                draw,
                (x1 + 4, task_y, x2 - 4, task_y + task_height),
                task['name'],
                task['status_key'],
                task_font,
                task=True,
            )
            previous_bottom = task_y + task_height

        if not tasks:
            empty_y = task_start_y
            draw.line((center_x, previous_bottom, center_x, empty_y), fill=LINE, width=2)
            draw.rounded_rectangle(
                (x1 + 4, empty_y, x2 - 4, empty_y + task_height),
                radius=10,
                fill=PANEL_SOFT,
                outline=LINE,
                width=1,
            )
            _draw_centered_lines(
                draw,
                ['暂无里程碑'],
                (x1 + 8, empty_y, x2 - 8, empty_y + task_height),
                task_font,
                MUTED,
            )

    legend_y = height - 55
    legend = [('已完成', 'completed'), ('进行中', 'in_progress'), ('有超期', 'overdue'), ('待开始', 'pending')]
    legend_x = margin
    for label, key in legend:
        color = STATUS_COLORS[key]
        draw.ellipse((legend_x, legend_y + 4, legend_x + 12, legend_y + 16), fill=color)
        draw.text((legend_x + 20, legend_y), label, font=label_font, fill=MUTED)
        legend_x += 110

    buffer = BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def _render_department_swimlane_png(product, overview):
    """将产品开发进度绘制为阶段横轴、负责部门纵轴的跨职能泳道图。"""
    stages = overview['stages']
    swimlanes = overview['swimlanes']
    stage_count = max(1, len(stages))

    margin = 48
    lane_label_width = 155
    stage_gap = 10
    desired_stage_width = 195
    width = max(
        1600,
        margin * 2 + lane_label_width + stage_count * desired_stage_width
        + (stage_count - 1) * stage_gap,
    )
    stage_width = (
        width - margin * 2 - lane_label_width - (stage_count - 1) * stage_gap
    ) / stage_count

    task_height = 32
    task_gap = 6
    lane_gap = 10
    lane_heights = []
    for lane in swimlanes:
        max_tasks = max(
            (len(stage['tasks']) for stage in lane['cells'] if stage),
            default=0,
        )
        lane_heights.append(
            max(134, 54 + max(1, max_tasks) * (task_height + task_gap) + 8)
        )

    board_header_y = 222
    board_header_height = 78
    lanes_height = sum(lane_heights) + lane_gap * max(0, len(lane_heights) - 1)
    height = max(820, board_header_y + board_header_height + 12 + lanes_height + 86)

    image = Image.new('RGB', (int(width), int(height)), BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = _font(29, bold=True)
    heading_font = _font(15, bold=True)
    value_font = _font(15, bold=True)
    label_font = _font(12)
    stage_font = _font(14, bold=True)
    task_font = _font(11, bold=True)

    draw.text(
        (margin, 30),
        f'{product.name} · 产品开发全流程进度泳道图',
        font=title_font,
        fill=TEXT,
    )
    draw.text(
        (margin, 78),
        f"{overview['status_label']}  ·  当前推进：{overview['active_stage_text']}",
        font=label_font,
        fill=MUTED,
    )

    info_values = [
        ('负责人', (product.assignee.first_name or product.assignee.username) if product.assignee else '未指定'),
        ('开始时间', _format_date(product.started_at)),
        ('预计结束', _format_date(product.expected_end_date)),
        ('实际结束', _format_date(product.actual_end_date, empty='未完成')),
        ('创建时间', _format_date(product.created_at)),
    ]
    info_width = 820
    info_x = width - margin - info_width
    info_y = 24
    info_height = 78
    item_width = info_width / len(info_values)
    draw.rounded_rectangle(
        (info_x, info_y, info_x + info_width, info_y + info_height),
        radius=12,
        fill=PANEL,
        outline=LINE,
        width=1,
    )
    for index, (label, value) in enumerate(info_values):
        x1 = info_x + index * item_width
        if index:
            draw.line((x1, info_y + 12, x1, info_y + info_height - 12), fill=LINE, width=1)
        draw.text((x1 + 12, info_y + 12), label, font=label_font, fill=MUTED)
        value_lines = _wrap_text(draw, value, value_font, item_width - 24, max_lines=1)
        draw.text((x1 + 12, info_y + 40), value_lines[0], font=value_font, fill=TEXT)

    stats = [
        ('整体任务完成率', f"{overview['progress_pct']}%"),
        ('阶段完成', f"{overview['completed_stages']} / {overview['stage_count']}"),
        ('任务完成', f"{overview['completed_tasks']} / {overview['total_tasks']}"),
        ('超期任务', str(overview['overdue_tasks'])),
        ('交付时间', overview['remaining_text']),
    ]
    stats_y = 125
    stats_width = (width - margin * 2) / len(stats)
    draw.rounded_rectangle(
        (margin, stats_y, width - margin, stats_y + 70),
        radius=10,
        fill=PANEL,
        outline=LINE,
        width=1,
    )
    for index, (label, value) in enumerate(stats):
        x1 = margin + index * stats_width
        if index:
            draw.line((x1, stats_y + 12, x1, stats_y + 58), fill=LINE, width=1)
        draw.text((x1 + 14, stats_y + 11), label, font=label_font, fill=MUTED)
        value_color = (
            STATUS_COLORS['overdue']
            if label == '超期任务' and overview['overdue_tasks'] else TEXT
        )
        draw.text((x1 + 14, stats_y + 37), value, font=value_font, fill=value_color)

    board_left = margin
    board_right = width - margin
    board_bottom = board_header_y + board_header_height + 12 + lanes_height
    draw.rounded_rectangle(
        (board_left, board_header_y, board_right, board_bottom),
        radius=12,
        fill='#071923',
        outline=LINE,
        width=1,
    )

    draw.rounded_rectangle(
        (
            board_left,
            board_header_y,
            board_left + lane_label_width,
            board_header_y + board_header_height,
        ),
        radius=10,
        fill=PANEL,
        outline=LINE,
        width=1,
    )
    draw.text(
        (board_left + 14, board_header_y + 15),
        '产品开发全流程',
        font=label_font,
        fill=STATUS_COLORS['completed'],
    )
    draw.text(
        (board_left + 14, board_header_y + 43),
        '负责部门 / 项目阶段',
        font=task_font,
        fill=TEXT,
    )

    for index, stage in enumerate(stages):
        x1 = board_left + lane_label_width + index * (stage_width + stage_gap)
        x2 = x1 + stage_width
        color = STATUS_COLORS.get(stage['status_key'], STATUS_COLORS['pending'])
        draw.rounded_rectangle(
            (x1, board_header_y, x2, board_header_y + board_header_height),
            radius=10,
            fill=PANEL,
            outline=color,
            width=2,
        )
        draw.text(
            (x1 + 12, board_header_y + 9),
            f'PHASE {index + 1:02d}',
            font=label_font,
            fill=MUTED,
        )
        stage_lines = _wrap_text(draw, stage['name'], stage_font, stage_width - 24, max_lines=2)
        _draw_centered_lines(
            draw,
            stage_lines,
            (x1 + 8, board_header_y + 24, x2 - 8, board_header_y + 59),
            stage_font,
            color,
        )
        status_width = _text_width(draw, stage['status_label'], label_font)
        draw.text(
            (x2 - status_width - 10, board_header_y + 59),
            stage['status_label'],
            font=label_font,
            fill=color,
        )

        if index < len(stages) - 1:
            arrow_y = board_header_y + board_header_height / 2
            next_x = x2 + stage_gap
            draw.line((x2 + 2, arrow_y, next_x - 3, arrow_y), fill=LINE, width=2)
            draw.polygon(
                [
                    (next_x - 3, arrow_y),
                    (next_x - 8, arrow_y - 4),
                    (next_x - 8, arrow_y + 4),
                ],
                fill=LINE,
            )

    if not stages:
        _draw_centered_lines(
            draw,
            ['当前项目还没有阶段数据'],
            (
                board_left + lane_label_width,
                board_header_y,
                board_right,
                board_header_y + board_header_height,
            ),
            heading_font,
            MUTED,
        )

    lane_y = board_header_y + board_header_height + 12
    for lane_index, (lane, lane_height) in enumerate(zip(swimlanes, lane_heights)):
        lane_bottom = lane_y + lane_height
        lane_fill = '#081c27' if lane_index % 2 == 0 else '#071923'
        draw.rounded_rectangle(
            (board_left, lane_y, board_right, lane_bottom),
            radius=9,
            fill=lane_fill,
            outline=LINE,
            width=1,
        )

        draw.rounded_rectangle(
            (board_left, lane_y, board_left + lane_label_width, lane_bottom),
            radius=9,
            fill=PANEL,
            outline=LINE,
            width=1,
        )
        lane_name_lines = _wrap_text(
            draw,
            lane['name'],
            heading_font,
            lane_label_width - 24,
            max_lines=2,
        )
        _draw_centered_lines(
            draw,
            lane_name_lines,
            (
                board_left + 10,
                lane_y + 16,
                board_left + lane_label_width - 10,
                lane_bottom - 26,
            ),
            heading_font,
            TEXT,
        )
        lane_count_text = f"负责 {lane['stage_count']} 个阶段"
        lane_count_width = _text_width(draw, lane_count_text, label_font)
        draw.text(
            (board_left + (lane_label_width - lane_count_width) / 2, lane_bottom - 25),
            lane_count_text,
            font=label_font,
            fill=MUTED,
        )

        for stage_index in range(stage_count):
            x1 = board_left + lane_label_width + stage_index * (stage_width + stage_gap)
            x2 = x1 + stage_width
            stage = lane['cells'][stage_index] if stage_index < len(lane['cells']) else None

            if not stage:
                center_y = lane_y + lane_height / 2
                draw.line((x1 + 12, center_y, x2 - 12, center_y), fill=LINE, width=1)
                center_x = (x1 + x2) / 2
                draw.ellipse(
                    (center_x - 3, center_y - 3, center_x + 3, center_y + 3),
                    fill=MUTED,
                )
                continue

            color = STATUS_COLORS.get(stage['status_key'], STATUS_COLORS['pending'])
            draw.rounded_rectangle(
                (x1 + 2, lane_y + 2, x2 - 2, lane_bottom - 2),
                radius=8,
                fill=PANEL_SOFT,
                outline=color,
                width=2,
            )
            draw.text(
                (x1 + 10, lane_y + 10),
                stage['status_label'],
                font=label_font,
                fill=color,
            )
            task_count = f"{len(stage['tasks'])} 个里程碑"
            task_count_width = _text_width(draw, task_count, label_font)
            draw.text(
                (x2 - task_count_width - 10, lane_y + 10),
                task_count,
                font=label_font,
                fill=MUTED,
            )

            task_y = lane_y + 39
            for task in stage['tasks']:
                _draw_node(
                    draw,
                    (x1 + 7, task_y, x2 - 7, task_y + task_height),
                    task['name'],
                    task['status_key'],
                    task_font,
                    task=True,
                )
                task_y += task_height + task_gap

            if not stage['tasks']:
                draw.rounded_rectangle(
                    (x1 + 7, task_y, x2 - 7, task_y + task_height),
                    radius=7,
                    fill=PANEL,
                    outline=LINE,
                    width=1,
                )
                _draw_centered_lines(
                    draw,
                    ['暂无里程碑'],
                    (x1 + 10, task_y, x2 - 10, task_y + task_height),
                    task_font,
                    MUTED,
                )

        lane_y = lane_bottom + lane_gap

    if not swimlanes:
        _draw_centered_lines(
            draw,
            ['当前项目还没有可展示的负责部门'],
            (board_left, board_header_y + board_header_height, board_right, board_bottom),
            heading_font,
            MUTED,
        )

    legend_y = height - 55
    legend = [
        ('已完成', 'completed'),
        ('进行中', 'in_progress'),
        ('有超期', 'overdue'),
        ('待开始', 'pending'),
    ]
    legend_x = margin
    for label, key in legend:
        color = STATUS_COLORS[key]
        draw.ellipse((legend_x, legend_y + 4, legend_x + 12, legend_y + 16), fill=color)
        draw.text((legend_x + 20, legend_y), label, font=label_font, fill=MUTED)
        legend_x += 110

    buffer = BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def _risk_panel_height(risks, columns=2):
    if not risks:
        return 108
    rows = (len(risks) + columns - 1) // columns
    return 76 + rows * 60 + max(0, rows - 1) * 8


def _draw_overdue_milestones_panel(
    draw,
    risks,
    box,
    title_font,
    label_font,
    value_font,
):
    """绘制延期里程碑风险面板，页面和 PNG 使用相同的信息口径。"""
    x1, y1, x2, y2 = box
    has_risks = bool(risks)
    border_color = '#6b353d' if has_risks else REPORT_LINE
    draw.rounded_rectangle(
        box,
        radius=12,
        fill=REPORT_PANEL,
        outline=border_color,
        width=1,
    )
    draw.text(
        (x1 + 16, y1 + 12),
        '风险预警 · 延期里程碑',
        font=title_font,
        fill=REPORT_TEXT,
    )
    draw.text(
        (x1 + 16, y1 + 36),
        '仅汇总当前已延期的里程碑，按逾期天数从高到低排列',
        font=label_font,
        fill=REPORT_MUTED,
    )
    count_text = f'{len(risks)} 项风险'
    count_width = _text_width(draw, count_text, value_font)
    draw.text(
        (x2 - count_width - 16, y1 + 20),
        count_text,
        font=value_font,
        fill=STATUS_COLORS['overdue'] if has_risks else STATUS_COLORS['completed'],
    )
    draw.line((x1 + 1, y1 + 60, x2 - 1, y1 + 60), fill=REPORT_LINE, width=1)

    if not risks:
        empty_text = '暂无延期里程碑 · 当前里程碑进度未发现延期风险'
        empty_width = _text_width(draw, empty_text, value_font)
        draw.text(
            ((x1 + x2 - empty_width) / 2, y1 + 75),
            empty_text,
            font=value_font,
            fill=STATUS_COLORS['completed'],
        )
        return

    columns = 2
    card_gap = 10
    row_gap = 8
    card_height = 60
    content_x1 = x1 + 10
    content_x2 = x2 - 10
    card_width = (content_x2 - content_x1 - card_gap) / columns
    cards_y = y1 + 68

    for index, risk in enumerate(risks):
        row = index // columns
        column = index % columns
        card_x1 = content_x1 + column * (card_width + card_gap)
        card_y1 = cards_y + row * (card_height + row_gap)
        card_x2 = card_x1 + card_width
        card_y2 = card_y1 + card_height
        draw.rounded_rectangle(
            (card_x1, card_y1, card_x2, card_y2),
            radius=8,
            fill='#071621',
            outline='#57333a',
            width=1,
        )
        draw.rounded_rectangle(
            (card_x1, card_y1, card_x1 + 4, card_y2),
            radius=2,
            fill=STATUS_COLORS['overdue'],
        )

        main_width = card_width * .46
        stage_text = _wrap_text(
            draw,
            f"{risk['stage_name']} · {risk['department_name']}",
            label_font,
            main_width - 16,
            max_lines=1,
        )[0]
        name_text = _wrap_text(
            draw,
            risk['name'],
            value_font,
            main_width - 16,
            max_lines=1,
        )[0]
        draw.text((card_x1 + 12, card_y1 + 8), stage_text, font=label_font, fill=REPORT_MUTED)
        draw.text((card_x1 + 12, card_y1 + 32), name_text, font=value_font, fill=REPORT_TEXT)

        meta = [
            ('负责人', risk['assignee_name'], REPORT_TEXT),
            ('预计结束', _format_date(risk['expected_end_date']), REPORT_TEXT),
            ('已延期', f"{risk['overdue_days']} 天", STATUS_COLORS['overdue']),
        ]
        meta_x = card_x1 + main_width
        meta_width = (card_x2 - meta_x - 8) / len(meta)
        for meta_index, (label, value, value_color) in enumerate(meta):
            item_x = meta_x + meta_index * meta_width
            value_text = _wrap_text(
                draw,
                value,
                value_font,
                meta_width - 8,
                max_lines=1,
            )[0]
            draw.text((item_x, card_y1 + 8), label, font=label_font, fill=REPORT_MUTED)
            draw.text((item_x, card_y1 + 32), value_text, font=value_font, fill=value_color)


def _render_stage_columns_png(product, overview):
    """绘制按阶段分列的全流程图，并将负责部门放在阶段名称旁边。"""
    stages = overview['stages']
    stage_count = max(1, len(stages))
    max_tasks = max((len(stage['tasks']) for stage in stages), default=0)

    margin = 48
    stage_gap = 10
    desired_stage_width = 205
    width = max(
        1600,
        margin * 2 + stage_count * desired_stage_width
        + (stage_count - 1) * stage_gap,
    )
    stage_width = (
        width - margin * 2 - (stage_count - 1) * stage_gap
    ) / stage_count

    task_height = 40
    task_gap = 6
    board_header_y = 222
    board_header_height = 90
    board_body_y = board_header_y + board_header_height + 10
    board_body_height = 48 + max(1, max_tasks) * (task_height + task_gap) + 10
    board_bottom = board_body_y + board_body_height
    risks = overview.get('overdue_milestones', [])
    legend_y = board_bottom + 20
    risk_panel_y = legend_y + 38
    risk_panel_height = _risk_panel_height(risks)
    height = max(820, risk_panel_y + risk_panel_height + 25)

    image = Image.new('RGB', (int(width), int(height)), BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = _font(30, bold=True)
    value_font = _font(16, bold=True)
    label_font = _font(13)
    stage_font = _font(15, bold=True)
    department_font = _font(11, bold=True)
    task_font = _font(12, bold=True)

    draw.text(
        (margin, 30),
        f'{product.name} · 产品开发全流程进度泳道图',
        font=title_font,
        fill=TEXT,
    )
    draw.text(
        (margin, 78),
        f"{overview['status_label']}  ·  当前推进：{overview['active_stage_text']}",
        font=label_font,
        fill=MUTED,
    )

    info_values = [
        ('负责人', (product.assignee.first_name or product.assignee.username) if product.assignee else '未指定'),
        ('开始时间', _format_date(product.started_at)),
        ('预计结束', _format_date(product.expected_end_date)),
        ('实际结束', _format_date(product.actual_end_date, empty='未完成')),
        ('创建时间', _format_date(product.created_at)),
    ]
    info_width = 820
    info_x = width - margin - info_width
    info_y = 24
    info_height = 78
    item_width = info_width / len(info_values)
    draw.rounded_rectangle(
        (info_x, info_y, info_x + info_width, info_y + info_height),
        radius=12,
        fill=PANEL,
        outline=LINE,
        width=1,
    )
    for index, (label, value) in enumerate(info_values):
        x1 = info_x + index * item_width
        if index:
            draw.line((x1, info_y + 12, x1, info_y + info_height - 12), fill=LINE, width=1)
        draw.text((x1 + 12, info_y + 12), label, font=label_font, fill=MUTED)
        value_lines = _wrap_text(draw, value, value_font, item_width - 24, max_lines=1)
        draw.text((x1 + 12, info_y + 40), value_lines[0], font=value_font, fill=TEXT)

    stats = [
        ('整体任务完成率', f"{overview['progress_pct']}%"),
        ('阶段完成', f"{overview['completed_stages']} / {overview['stage_count']}"),
        ('任务完成', f"{overview['completed_tasks']} / {overview['total_tasks']}"),
        ('超期任务', str(overview['overdue_tasks'])),
        ('交付时间', overview['remaining_text']),
    ]
    stats_y = 125
    stats_width = (width - margin * 2) / len(stats)
    draw.rounded_rectangle(
        (margin, stats_y, width - margin, stats_y + 70),
        radius=10,
        fill=PANEL,
        outline=LINE,
        width=1,
    )
    for index, (label, value) in enumerate(stats):
        x1 = margin + index * stats_width
        if index:
            draw.line((x1, stats_y + 12, x1, stats_y + 58), fill=LINE, width=1)
        draw.text((x1 + 14, stats_y + 11), label, font=label_font, fill=MUTED)
        value_color = (
            STATUS_COLORS['overdue']
            if label == '超期任务' and overview['overdue_tasks'] else TEXT
        )
        draw.text((x1 + 14, stats_y + 37), value, font=value_font, fill=value_color)

    draw.rounded_rectangle(
        (margin, board_header_y, width - margin, board_bottom),
        radius=12,
        fill='#071923',
        outline=LINE,
        width=1,
    )

    for index, stage in enumerate(stages):
        x1 = margin + index * (stage_width + stage_gap)
        x2 = x1 + stage_width
        color = STATUS_COLORS.get(stage['status_key'], STATUS_COLORS['pending'])

        draw.rounded_rectangle(
            (x1, board_header_y, x2, board_header_y + board_header_height),
            radius=10,
            fill=PANEL,
            outline=color,
            width=2,
        )
        draw.text(
            (x1 + 10, board_header_y + 8),
            f'PHASE {index + 1:02d}',
            font=label_font,
            fill=MUTED,
        )

        department_text = _wrap_text(
            draw,
            stage['department_name'],
            department_font,
            max(40, stage_width * .38),
            max_lines=1,
        )[0]
        department_width = min(
            stage_width * .43,
            _text_width(draw, department_text, department_font) + 16,
        )
        department_x = x2 - department_width - 9
        department_y = board_header_y + 29
        draw.rounded_rectangle(
            (
                department_x,
                department_y,
                department_x + department_width,
                department_y + 22,
            ),
            radius=10,
            fill='#102b37',
            outline=LINE,
            width=1,
        )
        department_text_width = _text_width(draw, department_text, department_font)
        draw.text(
            (
                department_x + (department_width - department_text_width) / 2,
                department_y + 4,
            ),
            department_text,
            font=department_font,
            fill='#9aadb6',
        )

        stage_name_width = max(28, department_x - x1 - 15)
        stage_name = _wrap_text(
            draw,
            stage['name'],
            stage_font,
            stage_name_width,
            max_lines=1,
        )[0]
        draw.text(
            (x1 + 10, board_header_y + 31),
            stage_name,
            font=stage_font,
            fill=color,
        )
        draw.text(
            (x1 + 10, board_header_y + 62),
            stage['status_label'],
            font=label_font,
            fill=color,
        )
        task_count_text = f"{len(stage['tasks'])} 个里程碑"
        task_count_width = _text_width(draw, task_count_text, label_font)
        draw.text(
            (x2 - task_count_width - 10, board_header_y + 62),
            task_count_text,
            font=label_font,
            fill=MUTED,
        )

        if index < len(stages) - 1:
            arrow_y = board_header_y + board_header_height / 2
            next_x = x2 + stage_gap
            draw.line((x2 + 2, arrow_y, next_x - 3, arrow_y), fill=LINE, width=2)
            draw.polygon(
                [
                    (next_x - 3, arrow_y),
                    (next_x - 8, arrow_y - 4),
                    (next_x - 8, arrow_y + 4),
                ],
                fill=LINE,
            )

        draw.rounded_rectangle(
            (x1, board_body_y, x2, board_bottom),
            radius=9,
            fill=PANEL_SOFT,
            outline=color,
            width=2,
        )
        draw.text(
            (x1 + 10, board_body_y + 10),
            '里程碑',
            font=label_font,
            fill=color,
        )

        task_y = board_body_y + 38
        for task in stage['tasks']:
            _draw_node(
                draw,
                (x1 + 6, task_y, x2 - 6, task_y + task_height),
                task['name'],
                task['status_key'],
                task_font,
                task=True,
            )
            task_y += task_height + task_gap

        if not stage['tasks']:
            draw.rounded_rectangle(
                (x1 + 6, task_y, x2 - 6, task_y + task_height),
                radius=7,
                fill=PANEL,
                outline=LINE,
                width=1,
            )
            _draw_centered_lines(
                draw,
                ['暂无里程碑'],
                (x1 + 10, task_y, x2 - 10, task_y + task_height),
                task_font,
                MUTED,
            )

    if not stages:
        _draw_centered_lines(
            draw,
            ['当前项目还没有阶段数据'],
            (margin, board_header_y, width - margin, board_bottom),
            stage_font,
            MUTED,
        )

    _draw_overdue_milestones_panel(
        draw,
        risks,
        (margin, risk_panel_y, width - margin, risk_panel_y + risk_panel_height),
        stage_font,
        label_font,
        value_font,
    )

    legend = [
        ('已完成', 'completed'),
        ('进行中', 'in_progress'),
        ('有超期', 'overdue'),
        ('待开始', 'pending'),
    ]
    legend_x = margin
    for label, key in legend:
        color = STATUS_COLORS[key]
        draw.ellipse((legend_x, legend_y + 4, legend_x + 12, legend_y + 16), fill=color)
        draw.text((legend_x + 20, legend_y), label, font=label_font, fill=MUTED)
        legend_x += 110

    buffer = BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def _process_map_required_height(stage, card_width):
    usable_width = max(60, card_width - 18)
    preferred_cell_width = 78
    columns = max(1, int(usable_width // preferred_cell_width))
    task_count = max(1, len(stage['tasks']))
    rows = (task_count + columns - 1) // columns
    return 76 + rows * 82 + 12


def _draw_process_map_task(draw, task, box, font):
    x1, y1, x2, y2 = box
    color = STATUS_COLORS.get(task['status_key'], STATUS_COLORS['pending'])
    center_x = (x1 + x2) / 2
    node_y = y1 + 18
    radius = 14

    node_fill = REPORT_PANEL if task['status_key'] == 'pending' else color
    icon_color = color if task['status_key'] == 'pending' else '#ffffff'
    draw.ellipse(
        (center_x - radius, node_y - radius, center_x + radius, node_y + radius),
        fill=node_fill,
        outline=color,
        width=2,
    )
    if task['status_key'] == 'completed':
        draw.line(
            (center_x - 7, node_y, center_x - 2, node_y + 5),
            fill=icon_color,
            width=2,
        )
        draw.line(
            (center_x - 2, node_y + 5, center_x + 8, node_y - 7),
            fill=icon_color,
            width=2,
        )
    elif task['status_key'] == 'in_progress':
        draw.ellipse(
            (center_x - 5, node_y - 5, center_x + 5, node_y + 5),
            fill=icon_color,
        )
    elif task['status_key'] == 'overdue':
        draw.line((center_x, node_y - 7, center_x, node_y + 3), fill=icon_color, width=2)
        draw.ellipse((center_x - 1, node_y + 7, center_x + 1, node_y + 9), fill=icon_color)
    else:
        draw.rectangle(
            (center_x - 5, node_y - 5, center_x + 5, node_y + 5),
            outline=icon_color,
            width=2,
        )

    lines = _wrap_text(draw, task['name'], font, max(30, x2 - x1 - 4), max_lines=2)
    _draw_centered_lines(
        draw,
        lines,
        (x1 + 1, y1 + 38, x2 - 1, y2),
        font,
        REPORT_TEXT,
        line_gap=2,
    )


def _draw_process_map_stage(draw, stage, box, fonts, stage_index):
    x1, y1, x2, y2 = box
    color = STATUS_COLORS.get(stage['status_key'], STATUS_COLORS['pending'])
    stage_font = fonts['stage']
    department_font = fonts['department']
    label_font = fonts['label']
    task_font = fonts['task']

    body_fill, header_fill = PROCESS_STAGE_PALETTES[stage_index % len(PROCESS_STAGE_PALETTES)]
    draw.rounded_rectangle(
        box,
        radius=12,
        fill=body_fill,
        outline=REPORT_LINE,
        width=1,
    )
    draw.rounded_rectangle(
        (x1, y1, x2, y1 + 4),
        radius=4,
        fill=color,
    )

    header_height = 68
    draw.rounded_rectangle(
        (x1 + 1, y1 + 1, x2 - 1, y1 + header_height),
        radius=11,
        fill=header_fill,
    )
    draw.rectangle((x1 + 1, y1 + 1, x2 - 1, y1 + 5), fill=color)
    draw.line(
        (x1 + 1, y1 + header_height, x2 - 1, y1 + header_height),
        fill=REPORT_LINE,
        width=1,
    )

    department = _wrap_text(
        draw,
        stage['department_name'],
        department_font,
        max(34, (x2 - x1) * .34),
        max_lines=1,
    )[0]
    department_width = min(
        (x2 - x1) * .39,
        _text_width(draw, department, department_font) + 14,
    )
    department_x = x2 - department_width - 8
    draw.rounded_rectangle(
        (department_x, y1 + 11, department_x + department_width, y1 + 32),
        radius=9,
        fill=REPORT_PANEL,
        outline=REPORT_LINE,
        width=1,
    )
    department_text_width = _text_width(draw, department, department_font)
    draw.text(
        (department_x + (department_width - department_text_width) / 2, y1 + 15),
        department,
        font=department_font,
        fill=REPORT_MUTED,
    )

    index_box = (x1 + 10, y1 + 11, x1 + 31, y1 + 32)
    draw.rounded_rectangle(
        index_box,
        radius=6,
        fill=REPORT_PANEL,
        outline=color,
        width=1,
    )
    index_text = f'{stage_index + 1:02d}'
    index_text_width = _text_width(draw, index_text, department_font)
    draw.text(
        (x1 + 10 + (21 - index_text_width) / 2, y1 + 15),
        index_text,
        font=department_font,
        fill=color,
    )
    title_text = _wrap_text(
        draw,
        stage['name'],
        stage_font,
        max(28, department_x - x1 - 43),
        max_lines=1,
    )[0]
    draw.text((x1 + 39, y1 + 13), title_text, font=stage_font, fill=REPORT_TEXT)

    assignee_text = f"负责人：{stage['assignee_name']}"
    assignee_lines = _wrap_text(
        draw,
        assignee_text,
        label_font,
        max(50, x2 - x1 - 18),
        max_lines=1,
    )
    draw.text((x1 + 10, y1 + 37), assignee_lines[0], font=label_font, fill=REPORT_MUTED)
    summary_text = f"{stage['status_label']} · {len(stage['tasks'])} 个里程碑"
    draw.text(
        (x1 + 10, y1 + 52),
        summary_text,
        font=label_font,
        fill=color,
    )

    tasks = stage['tasks']
    content_x1 = x1 + 7
    content_x2 = x2 - 7
    content_y1 = y1 + header_height + 8
    content_y2 = y2 - 7
    if not tasks:
        _draw_centered_lines(
            draw,
            ['暂无里程碑'],
            (content_x1, content_y1, content_x2, content_y2),
            task_font,
            REPORT_MUTED,
        )
        return

    content_width = content_x2 - content_x1
    columns = max(1, int(content_width // 78))
    rows = (len(tasks) + columns - 1) // columns
    cell_width = content_width / columns
    cell_height = min(82, max(64, (content_y2 - content_y1) / max(1, rows)))

    for task_index, task in enumerate(tasks):
        row = task_index // columns
        column = task_index % columns
        task_x1 = content_x1 + column * cell_width
        task_x2 = task_x1 + cell_width
        task_y1 = content_y1 + row * cell_height
        task_y2 = min(content_y2, task_y1 + cell_height)
        _draw_process_map_task(
            draw,
            task,
            (task_x1, task_y1, task_x2, task_y2),
            task_font,
        )

        next_task_same_row = (
            task_index + 1 < len(tasks)
            and (task_index + 1) // columns == row
        )
        if next_task_same_row:
            arrow_y = task_y1 + 18
            arrow_start = task_x1 + cell_width / 2 + 17
            arrow_end = task_x2 + cell_width / 2 - 17
            if arrow_end > arrow_start:
                draw.line((arrow_start, arrow_y, arrow_end - 5, arrow_y), fill='#87969d', width=1)
                draw.polygon(
                    [
                        (arrow_end, arrow_y),
                        (arrow_end - 5, arrow_y - 3),
                        (arrow_end - 5, arrow_y + 3),
                    ],
                    fill='#87969d',
                )


def _render_process_map_png(product, overview):
    """按参考图绘制拼图式产品开发流程地图。"""
    stages = overview['stages']
    if len(stages) != 8:
        return _render_stage_columns_png(product, overview)

    margin = 42
    gap = 8
    width = 1650
    column_weights = [1.18, 1, 1, 1, 1, 1, 1, .86]
    available_width = width - margin * 2 - 7 * gap
    base_column_width = available_width / sum(column_weights)
    column_widths = [base_column_width * weight for weight in column_weights]
    column_x = []
    current_x = margin
    for column_width in column_widths:
        column_x.append(current_x)
        current_x += column_width + gap

    fonts = {
        'title': _font(31, bold=True),
        'value': _font(16, bold=True),
        'label': _font(12),
        'stage': _font(14, bold=True),
        'department': _font(10, bold=True),
        'task': _font(11, bold=True),
    }

    def card_width(start_column, end_column):
        return sum(column_widths[start_column:end_column]) + (end_column - start_column - 1) * gap

    row_1_height = max(
        164,
        _process_map_required_height(stages[1], card_width(1, 4)),
        _process_map_required_height(stages[3], card_width(4, 7)),
    )
    row_2_height = max(
        170,
        _process_map_required_height(stages[2], card_width(1, 7)),
    )
    row_3_height = max(
        172,
        _process_map_required_height(stages[4], card_width(1, 3)),
        _process_map_required_height(stages[5], card_width(3, 5)),
        _process_map_required_height(stages[6], card_width(5, 7)),
    )

    side_height_required = max(
        _process_map_required_height(stages[0], card_width(0, 1)),
        _process_map_required_height(stages[7], card_width(7, 8)),
    )
    rows_total = row_1_height + row_2_height + row_3_height + gap * 2
    if side_height_required > rows_total:
        row_3_height += side_height_required - rows_total

    board_y = 218
    board_height = row_1_height + row_2_height + row_3_height + gap * 2
    board_bottom = board_y + board_height
    risks = overview.get('overdue_milestones', [])
    legend_y = board_bottom + 20
    risk_panel_y = legend_y + 38
    risk_panel_height = _risk_panel_height(risks)
    height = max(840, risk_panel_y + risk_panel_height + 25)
    image = Image.new('RGB', (int(width), int(height)), REPORT_BACKGROUND)
    draw = ImageDraw.Draw(image)

    draw.text(
        (margin, 30),
        f'{product.name} · 产品开发全流程进度图',
        font=fonts['title'],
        fill=REPORT_TEXT,
    )
    draw.text(
        (margin, 78),
        f"{overview['status_label']}  ·  当前推进：{overview['active_stage_text']}",
        font=fonts['label'],
        fill=REPORT_MUTED,
    )

    info_values = [
        ('负责人', (product.assignee.first_name or product.assignee.username) if product.assignee else '未指定'),
        ('开始时间', _format_date(product.started_at)),
        ('预计结束', _format_date(product.expected_end_date)),
        ('实际结束', _format_date(product.actual_end_date, empty='未完成')),
        ('创建时间', _format_date(product.created_at)),
    ]
    info_width = 820
    info_x = width - margin - info_width
    info_y = 24
    info_height = 78
    item_width = info_width / len(info_values)
    draw.rounded_rectangle(
        (info_x, info_y, info_x + info_width, info_y + info_height),
        radius=12,
        fill=REPORT_PANEL,
        outline=REPORT_LINE,
        width=1,
    )
    for index, (label, value) in enumerate(info_values):
        x1 = info_x + index * item_width
        if index:
            draw.line(
                (x1, info_y + 12, x1, info_y + info_height - 12),
                fill=REPORT_LINE,
                width=1,
            )
        draw.text((x1 + 12, info_y + 12), label, font=fonts['label'], fill=REPORT_MUTED)
        value_lines = _wrap_text(draw, value, fonts['value'], item_width - 24, max_lines=1)
        draw.text(
            (x1 + 12, info_y + 40),
            value_lines[0],
            font=fonts['value'],
            fill=REPORT_TEXT,
        )

    stats = [
        ('整体任务完成率', f"{overview['progress_pct']}%"),
        ('阶段完成', f"{overview['completed_stages']} / {overview['stage_count']}"),
        ('任务完成', f"{overview['completed_tasks']} / {overview['total_tasks']}"),
        ('超期任务', str(overview['overdue_tasks'])),
        ('交付时间', overview['remaining_text']),
    ]
    stats_y = 125
    stats_width = (width - margin * 2) / len(stats)
    draw.rounded_rectangle(
        (margin, stats_y, width - margin, stats_y + 70),
        radius=10,
        fill=REPORT_PANEL,
        outline=REPORT_LINE,
        width=1,
    )
    for index, (label, value) in enumerate(stats):
        x1 = margin + index * stats_width
        if index:
            draw.line(
                (x1, stats_y + 12, x1, stats_y + 58),
                fill=REPORT_LINE,
                width=1,
            )
        draw.text((x1 + 14, stats_y + 11), label, font=fonts['label'], fill=REPORT_MUTED)
        value_color = (
            STATUS_COLORS['overdue']
            if label == '超期任务' and overview['overdue_tasks'] else REPORT_TEXT
        )
        draw.text((x1 + 14, stats_y + 37), value, font=fonts['value'], fill=value_color)

    row_1_y = board_y
    row_2_y = row_1_y + row_1_height + gap
    row_3_y = row_2_y + row_2_height + gap
    board_bottom = row_3_y + row_3_height

    draw.rounded_rectangle(
        (margin - 10, board_y - 10, width - margin + 10, board_bottom + 10),
        radius=16,
        fill=PROCESS_MAP_BACKGROUND,
        outline=REPORT_LINE,
        width=1,
    )

    def box(column_start, column_end, y1, y2):
        x1 = column_x[column_start]
        x2 = column_x[column_end - 1] + column_widths[column_end - 1]
        return (x1, y1, x2, y2)

    stage_boxes = [
        box(0, 1, row_1_y, board_bottom),
        box(1, 4, row_1_y, row_1_y + row_1_height),
        box(1, 7, row_2_y, row_2_y + row_2_height),
        box(4, 7, row_1_y, row_1_y + row_1_height),
        box(1, 3, row_3_y, board_bottom),
        box(3, 5, row_3_y, board_bottom),
        box(5, 7, row_3_y, board_bottom),
        box(7, 8, row_1_y, board_bottom),
    ]
    for stage_index, (stage, stage_box) in enumerate(zip(stages, stage_boxes)):
        _draw_process_map_stage(draw, stage, stage_box, fonts, stage_index)

    _draw_overdue_milestones_panel(
        draw,
        risks,
        (margin, risk_panel_y, width - margin, risk_panel_y + risk_panel_height),
        fonts['stage'],
        fonts['label'],
        fonts['value'],
    )

    legend = [
        ('已完成', 'completed'),
        ('进行中', 'in_progress'),
        ('有超期', 'overdue'),
        ('待开始', 'pending'),
    ]
    legend_x = margin
    for label, key in legend:
        color = STATUS_COLORS[key]
        draw.ellipse((legend_x, legend_y + 4, legend_x + 12, legend_y + 16), fill=color)
        draw.text(
            (legend_x + 20, legend_y),
            label,
            font=fonts['label'],
            fill=REPORT_MUTED,
        )
        legend_x += 110

    buffer = BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


ROADMAP_BACKGROUND = '#F5F7FA'
ROADMAP_CARD = '#FFFFFF'
ROADMAP_TEXT = '#172033'
ROADMAP_MUTED = '#6D788A'
ROADMAP_SUBTLE = '#97A3B3'
ROADMAP_LABEL = '#111827'
ROADMAP_LINE = '#E2E7EE'
ROADMAP_LINE_SOFT = '#EDF1F5'
ROADMAP_STATUS = {
    'completed': {
        'main': '#12A65D',
        'soft': '#EDF9F3',
        'border': '#67CC99',
    },
    'in_progress': {
        'main': '#347FF0',
        'soft': '#EEF5FF',
        'border': '#70A5FA',
    },
    'overdue': {
        'main': '#FF4D4F',
        'soft': '#FFF2F1',
        'border': '#FF9292',
    },
    'pending': {
        'main': '#94A1B1',
        'soft': '#F2F4F7',
        'border': '#C8D0DA',
    },
}
ROADMAP_RISK_PANEL_TOP = '#123E59'
ROADMAP_RISK_PANEL_BOTTOM = '#061F32'
ROADMAP_RISK_PANEL_BORDER = '#2D82A3'
ROADMAP_RISK_CARD_PALETTES = [
    ('#FF8C3C', '#E74235'),
    ('#F36F39', '#CA3038'),
    ('#E94A36', '#A72938'),
    ('#D82F38', '#762138'),
]


def _roadmap_palette(status_key):
    return ROADMAP_STATUS.get(status_key, ROADMAP_STATUS['pending'])


def _draw_roadmap_card(draw, box, radius=12, outline=ROADMAP_LINE):
    """绘制参考图中的白色轻阴影卡片。"""
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(
        (x1 + 2, y1 + 4, x2 + 2, y2 + 4),
        radius=radius,
        fill='#EDEFF3',
    )
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=ROADMAP_CARD,
        outline=outline,
        width=1,
    )


def _draw_rounded_gradient(
    image,
    draw,
    box,
    start_color,
    end_color,
    *,
    radius=10,
    outline=None,
    outline_width=1,
    horizontal=False,
):
    """Draw a clipped linear gradient with an optional rounded outline."""
    x1, y1, x2, y2 = (int(round(value)) for value in box)
    width = max(1, x2 - x1 + 1)
    height = max(1, y2 - y1 + 1)
    start_rgb = ImageColor.getrgb(start_color)
    end_rgb = ImageColor.getrgb(end_color)
    gradient = Image.new('RGB', (width, height), start_rgb)
    gradient_draw = ImageDraw.Draw(gradient)
    steps = width if horizontal else height
    for position in range(steps):
        ratio = position / max(1, steps - 1)
        color = tuple(
            round(start + (end - start) * ratio)
            for start, end in zip(start_rgb, end_rgb)
        )
        if horizontal:
            gradient_draw.line((position, 0, position, height), fill=color)
        else:
            gradient_draw.line((0, position, width, position), fill=color)

    mask = Image.new('L', (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        fill=255,
    )
    image.paste(gradient, (x1, y1), mask)
    if outline:
        draw.rounded_rectangle(
            (x1, y1, x2, y2),
            radius=radius,
            outline=outline,
            width=outline_width,
        )


def _draw_roadmap_status_icon(draw, center, status_key, radius=7):
    x, y = center
    palette = _roadmap_palette(status_key)
    main = palette['main']
    if status_key in ('completed', 'in_progress'):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=main)
        if status_key == 'completed':
            draw.line(
                (x - 3, y, x - 1, y + 3, x + 4, y - 3),
                fill='#FFFFFF',
                width=2,
                joint='curve',
            )
        else:
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill='#FFFFFF')
    else:
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill='#FFFFFF',
            outline=main,
            width=2,
        )


def _roadmap_risk_panel_height(risks):
    if not risks:
        return 128
    rows = (len(risks) + 1) // 2
    return 84 + rows * 72 + max(0, rows - 1) * 10 + 12


def _draw_roadmap_risk_panel(image, draw, risks, box, fonts):
    x1, y1, x2, y2 = box
    has_risks = bool(risks)
    draw.rounded_rectangle(
        (x1 + 3, y1 + 5, x2 + 3, y2 + 5),
        radius=12,
        fill='#D7E0E8',
    )
    _draw_rounded_gradient(
        image,
        draw,
        box,
        ROADMAP_RISK_PANEL_TOP,
        ROADMAP_RISK_PANEL_BOTTOM,
        radius=12,
        outline=ROADMAP_RISK_PANEL_BORDER,
    )
    draw.rounded_rectangle(
        (x1 + 2, y1 + 2, x2 - 2, y1 + 5),
        radius=3,
        fill='#51B4D9' if has_risks else '#3AAE83',
    )

    kicker_color = '#8FD8F1' if has_risks else '#82E6BA'
    heading_center_x = (x1 + x2) / 2
    kicker_text = 'MILESTONE RISK ALERT'
    kicker_width = _text_width(draw, kicker_text, fonts['eyebrow'])
    draw.text(
        (heading_center_x - kicker_width / 2, y1 + 9),
        kicker_text,
        font=fonts['eyebrow'],
        fill=kicker_color,
    )
    risk_title = '风险预警'
    risk_title_width = _text_width(draw, risk_title, fonts['section'])
    draw.text(
        (heading_center_x - risk_title_width / 2, y1 + 27),
        risk_title,
        font=fonts['section'],
        fill='#F4FBFF',
    )
    risk_description = '仅汇总当前已延期的里程碑，按逾期天数从高到低排列'
    description_width = _text_width(draw, risk_description, fonts['small'])
    draw.text(
        (heading_center_x - description_width / 2, y1 + 50),
        risk_description,
        font=fonts['small'],
        fill='#C2DFEB',
    )

    count_text = f'{len(risks)} 项风险'
    count_width = _text_width(draw, count_text, fonts['risk_count']) + 24
    count_box = (x2 - count_width - 18, y1 + 22, x2 - 18, y1 + 52)
    count_start, count_end = ('#FF8F3D', '#E63F35') if has_risks else ('#218D68', '#17664F')
    count_outline = '#FFD0AC' if has_risks else '#A0EFCA'
    _draw_rounded_gradient(
        image,
        draw,
        count_box,
        count_start,
        count_end,
        radius=14,
        outline=count_outline,
        horizontal=True,
    )
    _draw_centered_lines(
        draw,
        [count_text],
        count_box,
        fonts['risk_count'],
        '#FFFAF4' if has_risks else '#EDFFF7',
    )
    draw.line((x1 + 1, y1 + 70, x2 - 1, y1 + 70), fill='#2F708D', width=1)

    if not risks:
        icon_x = (x1 + x2) / 2 - 126
        icon_y = y1 + 98
        _draw_roadmap_status_icon(draw, (icon_x, icon_y), 'completed', radius=9)
        draw.text((icon_x + 18, y1 + 84), '暂无延期里程碑', font=fonts['risk_name'], fill='#ECFFF6')
        draw.text(
            (icon_x + 18, y1 + 104),
            '当前里程碑进度未发现延期风险',
            font=fonts['small'],
            fill='#B8DDCF',
        )
        return

    columns = 2
    gap = 12
    card_height = 72
    content_x1 = x1 + 12
    content_x2 = x2 - 12
    card_width = (content_x2 - content_x1 - gap) / columns
    cards_y = y1 + 82
    for index, risk in enumerate(risks):
        row = index // columns
        column = index % columns
        card_x1 = content_x1 + column * (card_width + gap)
        card_y1 = cards_y + row * (card_height + 10)
        card_x2 = card_x1 + card_width
        card_y2 = card_y1 + card_height
        card_start, card_end = ROADMAP_RISK_CARD_PALETTES[index % len(ROADMAP_RISK_CARD_PALETTES)]
        _draw_rounded_gradient(
            image,
            draw,
            (card_x1, card_y1, card_x2, card_y2),
            card_start,
            card_end,
            radius=9,
            outline='#FFDAC0',
            horizontal=True,
        )
        draw.rounded_rectangle(
            (card_x1 + 10, card_y1 + 18, card_x1 + 44, card_y1 + 52),
            radius=8,
            fill='#A83A2E',
            outline='#FFF0DF',
            width=1,
        )
        draw.text((card_x1 + 21, card_y1 + 24), '!', font=fonts['body_bold'], fill='#FFFAF2')

        main_x = card_x1 + 56
        main_width = card_width * .43
        stage_text = _wrap_text(
            draw,
            f"{risk['stage_name']} · {risk['department_name']}",
            fonts['small_bold'],
            max(80, main_width - 12),
            max_lines=1,
        )[0]
        name_text = _wrap_text(
            draw,
            risk['name'],
            fonts['risk_name'],
            max(80, main_width - 12),
            max_lines=1,
        )[0]
        draw.text((main_x, card_y1 + 12), stage_text, font=fonts['small_bold'], fill='#FFF0E2')
        draw.text((main_x, card_y1 + 37), name_text, font=fonts['risk_name'], fill='#FFFFFF')

        meta_x = card_x1 + main_width + 54
        meta_width = (card_x2 - meta_x - 12) / 3
        meta = [
            ('负责人', risk['assignee_name'], '#FFFDF8'),
            ('预计结束', _format_date(risk['expected_end_date']), '#FFFDF8'),
            ('已延期', f"{risk['overdue_days']} 天", '#FFF3B8'),
        ]
        for meta_index, (label, value, value_color) in enumerate(meta):
            item_x = meta_x + meta_index * meta_width
            value_text = _wrap_text(
                draw,
                value,
                fonts['risk_value'],
                max(36, meta_width - 8),
                max_lines=1,
            )[0]
            draw.text((item_x, card_y1 + 12), label, font=fonts['risk_label'], fill='#FFF0E2')
            draw.text((item_x, card_y1 + 38), value_text, font=fonts['risk_value'], fill=value_color)


def render_product_progress_png(product, overview):
    """绘制与页面一致的浅色项目进度总览。"""
    stages = overview.get('stages', [])
    timeline = overview.get('timeline') or {}
    months = timeline.get('months', [])
    risks = overview.get('overdue_milestones', [])

    width = 1680
    margin = 34
    summary_y = 30
    summary_height = 204
    timeline_y = summary_y + summary_height + 18
    timeline_header_height = 52
    stage_row_height = 66
    stage_rows_height = max(150, len(stages) * stage_row_height)
    legend_height = 58
    timeline_height = timeline_header_height + stage_rows_height + legend_height
    risk_y = timeline_y + timeline_height + 18
    risk_height = _roadmap_risk_panel_height(risks)
    height = max(920, risk_y + risk_height + 30)

    image = Image.new('RGB', (width, int(height)), ROADMAP_BACKGROUND)
    draw = ImageDraw.Draw(image)
    fonts = {
        'page_title': _font(24, bold=True),
        'eyebrow': _font(10, bold=True),
        'hero': _font(27, bold=True),
        'percent': _font(29, bold=True),
        'metric': _font(18, bold=True),
        'section': _font(18, bold=True),
        'stage': _font(15, bold=True),
        'body': _font(13),
        'body_bold': _font(13, bold=True),
        'small': _font(11),
        'small_bold': _font(11, bold=True),
        'tiny': _font(9),
        'risk_name': _font(14, bold=True),
        'risk_label': _font(11, bold=True),
        'risk_value': _font(13, bold=True),
        'risk_count': _font(12, bold=True),
    }

    summary_box = (margin, summary_y, width - margin, summary_y + summary_height)
    _draw_roadmap_card(draw, summary_box)
    summary_x1, summary_y1, summary_x2, summary_y2 = summary_box

    intro_right = summary_x1 + 424
    draw.text((summary_x1 + 34, summary_y1 + 34), 'PRODUCT LIFECYCLE ROADMAP', font=fonts['eyebrow'], fill='#8493A8')
    project_title = _wrap_text(
        draw,
        f'{product.name} · 项目进度总览',
        fonts['hero'],
        intro_right - summary_x1 - 68,
        max_lines=2,
    )
    for title_index, title_line in enumerate(project_title):
        draw.text(
            (summary_x1 + 34, summary_y1 + 62 + title_index * 33),
            title_line,
            font=fonts['hero'],
            fill=ROADMAP_TEXT,
        )
    dot_y = summary_y1 + 133
    draw.ellipse((summary_x1 + 35, dot_y - 5, summary_x1 + 45, dot_y + 5), fill=ROADMAP_STATUS['completed']['main'])
    current_text = _wrap_text(
        draw,
        f"当前阶段：{overview['active_stage_text']}",
        fonts['body'],
        intro_right - summary_x1 - 75,
        max_lines=2,
    )
    for line_index, line in enumerate(current_text):
        draw.text((summary_x1 + 55, dot_y - 8 + line_index * 20), line, font=fonts['body'], fill=ROADMAP_MUTED)
    draw.line((intro_right, summary_y1 + 44, intro_right, summary_y2 - 44), fill=ROADMAP_LINE, width=1)

    ring_center_x = intro_right + 92
    ring_center_y = summary_y1 + summary_height / 2
    ring_radius = 58
    ring_box = (
        ring_center_x - ring_radius,
        ring_center_y - ring_radius,
        ring_center_x + ring_radius,
        ring_center_y + ring_radius,
    )
    draw.arc(ring_box, start=-90, end=269.9, fill='#DFF3E9', width=11)
    progress_end = -90 + 360 * max(0, min(100, overview['progress_pct'])) / 100
    if progress_end > -90:
        draw.arc(ring_box, start=-90, end=progress_end, fill=ROADMAP_STATUS['completed']['main'], width=11)
    percent_text = f"{overview['progress_pct']}%"
    percent_width = _text_width(draw, percent_text, fonts['percent'])
    draw.text((ring_center_x - percent_width / 2, ring_center_y - 27), percent_text, font=fonts['percent'], fill=ROADMAP_TEXT)
    rate_text = '任务完成率'
    rate_width = _text_width(draw, rate_text, fonts['small'])
    draw.text((ring_center_x - rate_width / 2, ring_center_y + 17), rate_text, font=fonts['small'], fill=ROADMAP_MUTED)

    data_x1 = intro_right + 174
    data_x2 = summary_x2 - 28
    info_top = summary_y1 + 24
    info_bottom = summary_y1 + 112
    info_values = [
        ('负责人', (product.assignee.first_name or product.assignee.username) if product.assignee else '未指定'),
        ('开始时间', _format_date(product.started_at)),
        ('预计结束', _format_date(product.expected_end_date)),
        ('实际结束', _format_date(product.actual_end_date, empty='未完成')),
        ('创建时间', _format_date(product.created_at)),
    ]
    info_width = (data_x2 - data_x1) / len(info_values)
    for index, (label, value) in enumerate(info_values):
        item_x = data_x1 + index * info_width
        if index:
            draw.line((item_x, info_top + 13, item_x, info_bottom - 12), fill=ROADMAP_LINE, width=1)
        label_text = _wrap_text(draw, label, fonts['small'], info_width - 28, max_lines=1)[0]
        value_text = _wrap_text(draw, value, fonts['body_bold'], info_width - 28, max_lines=1)[0]
        draw.text((item_x + 14, info_top + 13), label_text, font=fonts['small_bold'], fill=ROADMAP_LABEL)
        draw.text((item_x + 14, info_top + 47), value_text, font=fonts['body_bold'], fill=ROADMAP_TEXT)

    draw.line((data_x1, info_bottom, data_x2, info_bottom), fill=ROADMAP_LINE, width=1)
    metrics = [
        ('阶段完成', f"{overview['completed_stages']} / {overview['stage_count']}", ROADMAP_TEXT),
        ('任务完成', f"{overview['completed_tasks']} / {overview['total_tasks']}", ROADMAP_TEXT),
        ('超期任务', str(overview['overdue_tasks']), ROADMAP_STATUS['overdue']['main'] if overview['overdue_tasks'] else ROADMAP_TEXT),
        ('交付时间', overview['remaining_text'], ROADMAP_TEXT),
    ]
    metric_width = (data_x2 - data_x1) / len(metrics)
    metric_top = info_bottom
    for index, (label, value, value_color) in enumerate(metrics):
        item_x = data_x1 + index * metric_width
        if index:
            draw.line((item_x, metric_top + 15, item_x, summary_y2 - 18), fill=ROADMAP_LINE, width=1)
        label_width = _text_width(draw, label, fonts['small_bold'])
        value_text = _wrap_text(draw, value, fonts['metric'], metric_width - 24, max_lines=1)[0]
        value_width = _text_width(draw, value_text, fonts['metric'])
        draw.text((item_x + (metric_width - label_width) / 2, metric_top + 15), label, font=fonts['small_bold'], fill=ROADMAP_LABEL)
        draw.text((item_x + (metric_width - value_width) / 2, metric_top + 43), value_text, font=fonts['metric'], fill=value_color)

    timeline_box = (margin, timeline_y, width - margin, timeline_y + timeline_height)
    _draw_roadmap_card(draw, timeline_box)
    timeline_x1, timeline_y1, timeline_x2, timeline_y2 = timeline_box
    stage_column_width = 190
    track_x1 = timeline_x1 + stage_column_width
    track_x2 = timeline_x2
    header_bottom = timeline_y1 + timeline_header_height
    rows_bottom = header_bottom + stage_rows_height
    draw.line((timeline_x1, header_bottom, timeline_x2, header_bottom), fill=ROADMAP_LINE, width=1)
    draw.line((track_x1, timeline_y1, track_x1, rows_bottom), fill=ROADMAP_LINE, width=1)
    draw.text((timeline_x1 + 16, timeline_y1 + 18), '阶段 / 里程碑', font=fonts['small_bold'], fill='#586579')

    track_width = track_x2 - track_x1
    for month in months:
        month_left = track_x1 + track_width * month['start_pct'] / 100
        month_width = track_width * month['width_pct'] / 100
        draw.line((month_left, timeline_y1, month_left, rows_bottom), fill=ROADMAP_LINE_SOFT, width=1)
        half_x = month_left + month_width / 2
        dash_y = header_bottom
        while dash_y < rows_bottom:
            draw.line((half_x, dash_y, half_x, min(rows_bottom, dash_y + 4)), fill='#E6EBF1', width=1)
            dash_y += 9
        label_width = _text_width(draw, month['label'], fonts['body_bold'])
        draw.text((month_left + (month_width - label_width) / 2, timeline_y1 + 17), month['label'], font=fonts['body_bold'], fill='#516074')
    draw.line((track_x2 - 1, timeline_y1, track_x2 - 1, rows_bottom), fill=ROADMAP_LINE_SOFT, width=1)

    today_x = None
    if timeline.get('today_visible'):
        today_x = track_x1 + track_width * timeline.get('today_pct', 0) / 100
        today_label_box = (today_x - 22, header_bottom - 11, today_x + 22, header_bottom + 9)
        draw.rounded_rectangle(
            today_label_box,
            radius=5,
            fill=ROADMAP_STATUS['completed']['soft'],
            outline=ROADMAP_STATUS['completed']['border'],
            width=1,
        )
        _draw_centered_lines(
            draw,
            ['今天'],
            today_label_box,
            fonts['tiny'],
            '#087B43',
        )
        dash_y = header_bottom + 9
        while dash_y < rows_bottom:
            draw.line((today_x, dash_y, today_x, min(rows_bottom, dash_y + 5)), fill='#43B77F', width=1)
            dash_y += 8

    if stages:
        for stage_index, stage in enumerate(stages):
            row_y1 = header_bottom + stage_index * stage_row_height
            row_y2 = row_y1 + stage_row_height
            if stage_index:
                draw.line((timeline_x1, row_y1, timeline_x2, row_y1), fill=ROADMAP_LINE_SOFT, width=1)

            palette = _roadmap_palette(stage['status_key'])
            number_box = (timeline_x1 + 14, row_y1 + 19, timeline_x1 + 42, row_y1 + 47)
            draw.rounded_rectangle(
                number_box,
                radius=5,
                fill=palette['soft'],
                outline=palette['border'],
                width=1,
            )
            number_text = f'{stage_index + 1:02d}'
            _draw_centered_lines(
                draw,
                [number_text],
                number_box,
                fonts['small_bold'],
                palette['main'],
            )

            name_x = timeline_x1 + 54
            status_label = stage['status_label']
            status_width = max(48, _text_width(draw, status_label, fonts['tiny']) + 14)
            status_box = (track_x1 - status_width - 10, row_y1 + 22, track_x1 - 10, row_y1 + 44)
            stage_name_width = max(35, status_box[0] - name_x - 8)
            stage_name = _wrap_text(draw, stage['name'], fonts['stage'], stage_name_width, max_lines=1)[0]
            draw.text((name_x, row_y1 + 24), stage_name, font=fonts['stage'], fill=ROADMAP_TEXT)
            draw.rounded_rectangle(status_box, radius=6, fill=palette['soft'])
            _draw_centered_lines(draw, [status_label], status_box, fonts['tiny'], palette['main'])

            band_x1 = track_x1 + track_width * stage.get('timeline_left_pct', 0) / 100
            band_width = track_width * stage.get('timeline_width_pct', 4) / 100
            desired_band_width = max(
                70,
                band_width,
                stage.get('timeline_min_width_px', 88),
            )
            right_pct = stage.get('timeline_right_pct', 1)
            aligned_right_x = track_x2 - track_width * right_pct / 100
            maximum_right_x = track_x2 - (96 if stage['status_key'] == 'overdue' else 10)
            if stage.get('timeline_align_right') or band_x1 + desired_band_width > maximum_right_x:
                band_x2 = min(maximum_right_x, aligned_right_x)
                band_x1 = max(track_x1 + 2, band_x2 - desired_band_width)
            else:
                band_x2 = min(maximum_right_x, band_x1 + desired_band_width)
            band_box = (band_x1, row_y1 + 12, band_x2, row_y2 - 12)
            draw.rounded_rectangle(
                band_box,
                radius=10,
                fill=palette['soft'],
                outline=palette['border'],
                width=1,
            )

            tasks = stage.get('tasks', [])
            if tasks:
                inner_x1 = band_x1 + 8
                inner_x2 = band_x2 - 8
                task_gap = 6
                task_group_width = (
                    sum(task.get('display_width_px', 88) for task in tasks)
                    + max(0, len(tasks) - 1) * task_gap
                )
                task_x = inner_x1 + max(0, (inner_x2 - inner_x1 - task_group_width) / 2)
                for task_index, task in enumerate(tasks):
                    pill_width = task.get('display_width_px', 88)
                    pill_x1 = task_x
                    pill_x2 = pill_x1 + pill_width
                    pill_box = (pill_x1, row_y1 + 19, pill_x2, row_y2 - 19)
                    draw.rounded_rectangle(
                        pill_box,
                        radius=6,
                        fill='#FFFFFF',
                        outline='#E9EDF2',
                        width=1,
                    )
                    icon_x = pill_x1 + 14
                    icon_y = (row_y1 + row_y2) / 2
                    _draw_roadmap_status_icon(draw, (icon_x, icon_y), task['status_key'], radius=6)
                    task_text = _wrap_text(
                        draw,
                        task['name'],
                        fonts['small'],
                        max(14, pill_x2 - icon_x - 16),
                        max_lines=1,
                    )[0]
                    _draw_centered_lines(
                        draw,
                        [task_text],
                        (icon_x + 10, pill_box[1] + 2, pill_x2 - 4, pill_box[3] - 2),
                        fonts['small'],
                        '#273247',
                    )
                    task_x = pill_x2 + task_gap
            else:
                empty_text = '暂无里程碑'
                empty_width = _text_width(draw, empty_text, fonts['small'])
                draw.text(((band_x1 + band_x2 - empty_width) / 2, row_y1 + 26), empty_text, font=fonts['small'], fill=ROADMAP_MUTED)

            if stage['status_key'] == 'overdue':
                flag_text = '已超期  !'
                flag_width = _text_width(draw, flag_text, fonts['small_bold'])
                draw.text((track_x2 - flag_width - 16, row_y1 + 26), flag_text, font=fonts['small_bold'], fill=palette['main'])
    else:
        empty_text = '当前项目还没有阶段数据'
        empty_width = _text_width(draw, empty_text, fonts['body'])
        draw.text(((timeline_x1 + timeline_x2 - empty_width) / 2, header_bottom + 65), empty_text, font=fonts['body'], fill=ROADMAP_MUTED)

    draw.line((timeline_x1, rows_bottom, timeline_x2, rows_bottom), fill=ROADMAP_LINE, width=1)
    legend = [
        ('已完成', 'completed'),
        ('进行中', 'in_progress'),
        ('有超期', 'overdue'),
        ('待开始', 'pending'),
    ]
    legend_width = 112
    legend_start = (width - len(legend) * legend_width) / 2
    legend_y = rows_bottom + legend_height / 2
    for legend_index, (label, key) in enumerate(legend):
        item_x = legend_start + legend_index * legend_width
        color = _roadmap_palette(key)['main']
        draw.ellipse((item_x, legend_y - 6, item_x + 12, legend_y + 6), fill=color)
        draw.text(
            (item_x + 22, legend_y - 9),
            label,
            font=fonts['small_bold'],
            fill=color,
        )

    _draw_roadmap_risk_panel(
        image,
        draw,
        risks,
        (margin, risk_y, width - margin, risk_y + risk_height),
        fonts,
    )

    buffer = BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()
