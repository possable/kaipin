"""给 (企业, 企微userid) 加唯一约束。

**故意排在 0012 去重之后。** makemigrations 本能地会把它和 AddField 塞进同一个
文件，那样整条链会在 1062 上失败。这里手工拆开，就是为了让重复数据先被清掉。

MySQL 的唯一索引允许多个 NULL 共存，所以未绑定企微的账号
（wecom_corp=NULL，wechat_userid=''）可以有多条，不受影响。
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0012_dedupe_userid'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='userprofile',
            unique_together={('wecom_corp', 'wechat_userid')},
        ),
    ]
