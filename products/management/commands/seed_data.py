from django.core.management.base import BaseCommand
from accounts.models import Department
from templates_app.models import StageTemplate, TaskTemplate


class Command(BaseCommand):
    help = '初始化种子数据：5个部门、5个阶段模板及默认子任务'

    def handle(self, *args, **options):
        # 部门
        departments = {}
        for name in ['策划部', '设计部', '产品研发部', '供应链部', '产品部']:
            dept, created = Department.objects.get_or_create(name=name)
            departments[name] = dept
            self.stdout.write(f'  部门: {name} {"(新建)" if created else "(已存在)"}')

        # 阶段模板和子任务
        stages_data = [
            ('营销方案与上市准备', 1, '策划部', [
                '市场调研/竞品分析', '目标客群与定位确认', '营销方案撰写',
                '上市时间/渠道排期', '方案内部评审通过',
            ]),
            ('包装设计', 2, '设计部', [
                '包装设计初稿', '内部评审', '确认稿', '打样确认', '印刷文件定稿',
            ]),
            ('研发方案', 3, '产品研发部', [
                '产品配方/结构方案', '内部测试', '样品制作', '方案定稿', '检测/合规报告确认',
            ]),
            ('供应链进度', 4, '供应链部', [
                '供应商筛选/确认', '采购下单', '生产排期', '生产进度跟进', '到货/入库确认',
            ]),
            ('新品上市复盘', 5, '产品部', [
                '上市数据收集', '销售/反馈复盘', '问题总结', '改进建议', '复盘报告归档',
            ]),
        ]

        # 先清空旧模板（不影响已有的 Product）
        StageTemplate.objects.all().delete()

        for name, order, dept_name, task_names in stages_data:
            st = StageTemplate.objects.create(
                name=name, order=order, department=departments[dept_name]
            )
            for i, tname in enumerate(task_names, 1):
                TaskTemplate.objects.create(
                    stage_template=st, name=tname, order=i
                )
            self.stdout.write(f'  阶段模板: {name} ({len(task_names)} 个子任务)')

        self.stdout.write(self.style.SUCCESS('种子数据初始化完成'))
