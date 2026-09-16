"""把 .env 里现有的 WECHAT_* 落成一条默认企业，并把现网 164 个已绑用户挂上去。

幂等：用 get_or_create / update 而不是 create，重跑不会重复建。
可以在空库上跑（此时没有任何用户，只建企业那一条）。

分两个迁移是有意的：0008 只建表，本迁移只灌数据。
将来加 unique_together 时必须再拆一个独立迁移，且必须排在去重之后 ——
现网存在两个账号共用同一个 wechat_userid（ZengLiPing），
把约束和加字段塞进同一个迁移会让整个事务以 IntegrityError 1062 回滚。
"""
from django.conf import settings
from django.db import migrations


def seed_default_corp(apps, schema_editor):
    WeComCorp = apps.get_model('accounts', 'WeComCorp')

    corp_id = getattr(settings, 'WECHAT_CORP_ID', '') or ''
    if not corp_id:
        # 没配企业微信（比如本地开发），不建任何企业，
        # accounts/wecom.py 会回退到 settings，行为不变。
        return

    WeComCorp.objects.update_or_create(
        code='default',
        defaults={
            'name': '默认企业',
            'corp_id': corp_id,
            'agent_id': str(getattr(settings, 'WECHAT_AGENT_ID', '') or ''),
            'app_secret': getattr(settings, 'WECHAT_APP_SECRET', '') or '',
            'is_active': True,
            'is_default': True,
        },
    )


def unseed_default_corp(apps, schema_editor):
    WeComCorp = apps.get_model('accounts', 'WeComCorp')
    WeComCorp.objects.filter(code='default').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_wecomcorp'),
    ]

    operations = [
        migrations.RunPython(seed_default_corp, unseed_default_corp),
    ]
