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
