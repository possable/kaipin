import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_department_wechat_dept_id'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TodoItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('content', models.CharField(max_length=200, verbose_name='内容')),
                ('due_at', models.DateTimeField(blank=True, null=True, verbose_name='截止时间')),
                ('is_done', models.BooleanField(default=False, verbose_name='已完成')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('completed_at', models.DateTimeField(blank=True, null=True, verbose_name='完成时间')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='todos', to=settings.AUTH_USER_MODEL, verbose_name='用户')),
            ],
            options={
                'verbose_name': '待办事项',
                'verbose_name_plural': '待办事项',
                'ordering': ['is_done', 'due_at', '-created_at'],
            },
        ),
    ]
