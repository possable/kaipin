from django.test import TestCase
from django.contrib.auth.models import User
from accounts.models import Department
from .models import StageTemplate, TaskTemplate, ChecklistItemTemplate


class StageTemplateEditTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='设计部')
        self.admin = User.objects.create_user(username='admin', password='pass')
        self.admin.profile.role = 'admin'
        self.admin.profile.save()
        self.assignee = User.objects.create_user(username='zhangsan', password='pass')
        self.stage = StageTemplate.objects.create(
            name='包装设计', order=1, department=self.dept,
        )
        self.client.login(username='admin', password='pass')

    def test_edit_saves_default_assignee(self):
        """编辑阶段模板时，默认负责人字段必须被保存（回归：曾漏掉此字段导致改了不生效）"""
        resp = self.client.post(
            f'/templates/stages/{self.stage.pk}/edit/',
            {
                'name': self.stage.name,
                'order': self.stage.order,
                'department': self.dept.id,
                'default_assignee': self.assignee.id,
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.default_assignee_id, self.assignee.id)

    def test_edit_can_clear_default_assignee(self):
        self.stage.default_assignee = self.assignee
        self.stage.save()

        resp = self.client.post(
            f'/templates/stages/{self.stage.pk}/edit/',
            {
                'name': self.stage.name,
                'order': self.stage.order,
                'department': self.dept.id,
                'default_assignee': '',
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.stage.refresh_from_db()
        self.assertIsNone(self.stage.default_assignee_id)

    def test_default_assignee_picker_only_lists_active_users(self):
        User.objects.create_user(
            username='template-active', password='pass', first_name='在职员工',
        )
        User.objects.create_user(
            username='template-left', password='pass', first_name='离职员工',
            is_active=False,
        )

        resp = self.client.get(f'/templates/stages/{self.stage.pk}/edit/')

        self.assertContains(resp, '在职员工')
        self.assertNotContains(resp, '离职员工')


class StageTemplateDeleteTest(TestCase):
    """回归：删除确认页曾复用编辑表单但未传 departments，导致必填下拉框为空、
    浏览器端校验拦截提交，删除按钮点击无效果。"""

    def setUp(self):
        self.dept = Department.objects.create(name='设计部')
        self.admin = User.objects.create_user(username='admin', password='pass')
        self.admin.profile.role = 'admin'
        self.admin.profile.save()
        self.stage = StageTemplate.objects.create(
            name='包装设计', order=1, department=self.dept,
        )
        self.client.login(username='admin', password='pass')

    def test_confirm_page_renders_without_department_select(self):
        """删除确认页不应依赖 departments 数据才能提交表单"""
        resp = self.client.get(f'/templates/stages/{self.stage.pk}/delete/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="department"')

    def test_post_deletes_stage(self):
        resp = self.client.post(f'/templates/stages/{self.stage.pk}/delete/')
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(StageTemplate.objects.filter(pk=self.stage.pk).exists())


class TaskTemplateDeleteTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='设计部')
        self.admin = User.objects.create_user(username='admin', password='pass')
        self.admin.profile.role = 'admin'
        self.admin.profile.save()
        self.stage = StageTemplate.objects.create(
            name='包装设计', order=1, department=self.dept,
        )
        self.task = TaskTemplate.objects.create(stage_template=self.stage, name='内部评审')
        self.client.login(username='admin', password='pass')

    def test_confirm_page_renders(self):
        resp = self.client.get(f'/templates/tasks/{self.task.pk}/delete/')
        self.assertEqual(resp.status_code, 200)

    def test_post_deletes_task(self):
        resp = self.client.post(f'/templates/tasks/{self.task.pk}/delete/')
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(TaskTemplate.objects.filter(pk=self.task.pk).exists())


class ChecklistTemplateDeleteTest(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name='设计部')
        self.admin = User.objects.create_user(username='admin', password='pass')
        self.admin.profile.role = 'admin'
        self.admin.profile.save()
        self.stage = StageTemplate.objects.create(
            name='包装设计', order=1, department=self.dept,
        )
        self.task = TaskTemplate.objects.create(stage_template=self.stage, name='内部评审')
        self.item = ChecklistItemTemplate.objects.create(task_template=self.task, name='确认包材供应商')
        self.client.login(username='admin', password='pass')

    def test_confirm_page_renders(self):
        resp = self.client.get(f'/templates/checklist/{self.item.pk}/delete/')
        self.assertEqual(resp.status_code, 200)

    def test_post_deletes_item(self):
        resp = self.client.post(f'/templates/checklist/{self.item.pk}/delete/')
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ChecklistItemTemplate.objects.filter(pk=self.item.pk).exists())


class TaskTemplatePredecessorTest(TestCase):
    """流程模板里可以给子任务设置前置项，留空即无前置。"""

    def setUp(self):
        self.dept = Department.objects.create(name='流程模板部')
        self.admin = User.objects.create_user(username='tpl-admin', password='pass')
        self.admin.profile.role = 'admin'
        self.admin.profile.save()
        self.stage1 = StageTemplate.objects.create(
            name='设计阶段', order=1, department=self.dept,
        )
        self.stage2 = StageTemplate.objects.create(
            name='包装阶段', order=2, department=self.dept,
        )
        self.task_a = TaskTemplate.objects.create(
            stage_template=self.stage1, name='完成设计稿', order=1,
        )
        self.task_b = TaskTemplate.objects.create(
            stage_template=self.stage1, name='内部评审', order=2,
        )
        self.task_c = TaskTemplate.objects.create(
            stage_template=self.stage2, name='开始打样', order=1,
        )
        self.client.login(username='tpl-admin', password='pass')

    def test_create_task_with_cross_stage_predecessor(self):
        response = self.client.post(
            f'/templates/stages/{self.stage1.pk}/tasks/create/',
            {
                'name': '确认包装结构',
                'order': '3',
                'depends_on': self.task_c.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        task = TaskTemplate.objects.get(name='确认包装结构')
        self.assertEqual(
            list(task.depends_on.values_list('pk', flat=True)),
            [self.task_c.pk],
        )

    def test_create_task_with_multiple_predecessors(self):
        response = self.client.post(
            f'/templates/stages/{self.stage1.pk}/tasks/create/',
            {
                'name': '确认包装结构',
                'order': '3',
                'depends_on': [self.task_a.pk, self.task_c.pk],
            },
        )

        self.assertEqual(response.status_code, 302)
        task = TaskTemplate.objects.get(name='确认包装结构')
        self.assertEqual(
            set(task.depends_on.values_list('pk', flat=True)),
            {self.task_a.pk, self.task_c.pk},
        )

    def test_edit_without_predecessor_clears_it(self):
        self.task_b.depends_on.set([self.task_a])

        response = self.client.post(
            f'/templates/tasks/{self.task_b.pk}/edit/',
            {'name': self.task_b.name, 'order': '2', 'depends_on': ''},
        )

        self.assertEqual(response.status_code, 302)
        self.task_b.refresh_from_db()
        self.assertFalse(self.task_b.depends_on.exists())

    def test_edit_rejects_self_predecessor(self):
        response = self.client.post(
            f'/templates/tasks/{self.task_b.pk}/edit/',
            {
                'name': self.task_b.name,
                'order': '2',
                'depends_on': self.task_b.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.task_b.refresh_from_db()
        self.assertFalse(self.task_b.depends_on.exists())
        self.assertContains(response, '前置任务不能是任务自身')

    def test_edit_rejects_cycle(self):
        self.task_b.depends_on.set([self.task_a])

        response = self.client.post(
            f'/templates/tasks/{self.task_a.pk}/edit/',
            {
                'name': self.task_a.name,
                'order': '1',
                'depends_on': self.task_b.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.task_a.refresh_from_db()
        self.assertFalse(self.task_a.depends_on.exists())
        self.assertContains(response, '不能设为前置任务')

    def test_form_lists_predecessors_grouped_by_stage(self):
        response = self.client.get(
            f'/templates/tasks/{self.task_b.pk}/edit/',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<optgroup label="设计阶段">')
        self.assertContains(response, '<optgroup label="包装阶段">')
        self.assertContains(response, '开始打样')

    def test_list_shows_predecessor(self):
        self.task_c.depends_on.set([self.task_a])

        response = self.client.get(
            f'/templates/stages/{self.stage2.pk}/tasks/',
        )

        self.assertContains(response, '设计阶段 · 完成设计稿')
