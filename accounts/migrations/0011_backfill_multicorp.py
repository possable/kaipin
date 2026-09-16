"""把现网的单企业数据回填成多企业结构。

- 164 个已绑 profile → wecom_corp = 默认企业
- 164 条 WeComIdentity（企业, userid）→ 账号，第一次绑定的那个企业设为主身份
- 37 个 Department(wechat_dept_id 非空) → WeComDept 映射，is_manual=True

全部走 get_or_create，**可以重复执行**。这一点很重要：MySQL 的 DDL 不能回滚，
Django 不会把这类迁移包在事务里，中途失败会留下部分完成的状态。
"""
from django.db import migrations


def backfill(apps, schema_editor):
    WeComCorp = apps.get_model('accounts', 'WeComCorp')
    WeComDept = apps.get_model('accounts', 'WeComDept')
    WeComIdentity = apps.get_model('accounts', 'WeComIdentity')
    UserProfile = apps.get_model('accounts', 'UserProfile')
    Department = apps.get_model('accounts', 'Department')

    corp = (
        WeComCorp.objects.filter(code='default').first()
        or WeComCorp.objects.filter(is_default=True).first()
    )
    if corp is None:
        # 迁移 0009 只在 WECHAT_CORP_ID 非空时才播种；没有默认企业就没什么可回填的
        print('  未找到默认企业（迁移 0009 未播种），跳过回填')
        return

    n_profile = n_identity = 0
    for profile in UserProfile.objects.exclude(wechat_userid='').iterator():
        if profile.wecom_corp_id is None:
            profile.wecom_corp_id = corp.pk
            profile.save(update_fields=['wecom_corp'])
            n_profile += 1
        # 同 (企业, userid) 只建一条：现网 ZengLiPing 有重复，先到先得，
        # 剩下的那个账号由 0012 合并进来
        _, created = WeComIdentity.objects.get_or_create(
            corp=corp, userid=profile.wechat_userid,
            defaults={'user_id': profile.user_id, 'is_primary': True},
        )
        if created:
            n_identity += 1

    n_dept = 0
    for dept in Department.objects.exclude(wechat_dept_id=None).iterator():
        _, created = WeComDept.objects.get_or_create(
            corp=corp, wechat_dept_id=dept.wechat_dept_id,
            defaults={
                'name': dept.name,
                'department_id': dept.pk,
                # 现网这些映射是人工维护的，同步命令不许覆盖
                'is_manual': True,
                # 企业自己那一层（如 Department「莱特维健」）不参与主部门解析
                'is_root': (dept.name == corp.name),
            },
        )
        if created:
            n_dept += 1

    print(f'  回填完成：{n_profile} 个 profile 绑定企业，'
          f'{n_identity} 条身份，{n_dept} 个部门映射')


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_multicorp'),
    ]

    operations = [
        # 反向回滚没有意义（数据已被后续步骤改写），失败时从备份恢复
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
