"""合并「同一企业内、两个账号绑了同一个企微 userid」的重复账号。

现网实测只有一组：ZengLiPing(pk=124) / CengLiPing(pk=173)，都是「曾丽萍」。
成因：遗留命令 sync_wechat.py 用 userid 当 username 建过一次账号，后来拼音匹配
（pypinyin 把「曾」拼成 Ceng）没命中，又建了一个。

**必须排在 AlterUniqueTogether 之前** —— 否则唯一索引会因为这条重复数据
直接抛 IntegrityError 1062，整个迁移失败。

通用实现而不是硬编码那两个人：同 (企业, userid) 分一组，保留 user_id 最小的，
其余并进去。将来再有同类重复也能自动处理。
"""
from django.db import migrations


def _merge(apps, winner_id, loser_id, corp_id, userid):
    """把 loser 的所有引用搬到 winner，然后删掉 loser。"""
    User = apps.get_model('auth', 'User')
    UserProfile = apps.get_model('accounts', 'UserProfile')
    WeComIdentity = apps.get_model('accounts', 'WeComIdentity')

    touched = []

    # ① 所有指向 User 的外键 / 多对多，逐个改指 winner。
    #    UserProfile 和 WeComIdentity 单独处理（各有唯一约束，见下）。
    for model in apps.get_models():
        if model._meta.label in ('accounts.UserProfile', 'accounts.WeComIdentity'):
            continue
        for field in model._meta.get_fields():
            if not field.is_relation or field.related_model is not User:
                continue

            if field.many_to_one:
                n = model.objects.filter(**{field.attname: loser_id}).update(
                    **{field.attname: winner_id})
                if n:
                    touched.append(f'{model._meta.label}.{field.name}×{n}')

            elif field.many_to_many:
                # ⚠️ Django 3.2 里 through 挂在**字段**上，remote_field 上取不到。
                # 会被扫到的是 auth.Permission.user / auth.Group.user 这两个
                # 反向 m2m（即 User.user_permissions / User.groups）。
                through = getattr(field, 'through', None)
                if through is None:
                    continue
                user_col = next(
                    (fk.attname for fk in through._meta.get_fields()
                     if fk.many_to_one and fk.related_model is User), None)
                if user_col is None:
                    continue
                moved = dup = 0
                for row in list(through.objects.filter(**{user_col: loser_id})):
                    same = {f.attname: getattr(row, f.attname)
                            for f in through._meta.fields
                            if not f.primary_key and f.attname != user_col}
                    if through.objects.filter(**{user_col: winner_id}, **same).exists():
                        row.delete()       # winner 已有同一条关系，避免撞 through 的唯一约束
                        dup += 1
                    else:
                        setattr(row, user_col, winner_id)
                        row.save(update_fields=[user_col])
                        moved += 1
                if moved or dup:
                    touched.append(f'{model._meta.label}.{field.name}(+{moved},重{dup})')

    # ② 身份：winner 在同一企业已经有身份了就把 loser 那条删掉，
    #    否则改指 winner。直接改指会撞 (user, corp) 唯一约束。
    for ident in WeComIdentity.objects.filter(user_id=loser_id).iterator():
        clash = WeComIdentity.objects.filter(
            corp_id=ident.corp_id, user_id=winner_id
        ).exists()
        if clash or ident.corp_id == corp_id:
            ident.delete()
        else:
            ident.user_id = winner_id
            ident.save(update_fields=['user'])

    # ③ 主身份缓存：winner 已经绑了的话保持不动，只补空的
    winner_profile = UserProfile.objects.filter(user_id=winner_id).first()
    if winner_profile is not None and not winner_profile.wechat_userid:
        winner_profile.wechat_userid = userid
        winner_profile.wecom_corp_id = corp_id
        winner_profile.save(update_fields=['wechat_userid', 'wecom_corp'])

    # ④ 删 loser（profile / identity 由 CASCADE 带走）
    User.objects.filter(pk=loser_id).delete()

    detail = '、'.join(touched) if touched else '无外键引用'
    print(f'  合并 user {loser_id} → {winner_id}（userid={userid!r}）：{detail}')


def dedupe(apps, schema_editor):
    UserProfile = apps.get_model('accounts', 'UserProfile')

    groups = {}
    for profile in UserProfile.objects.exclude(wechat_userid='').exclude(wecom_corp=None).iterator():
        groups.setdefault((profile.wecom_corp_id, profile.wechat_userid), []).append(profile.user_id)

    n = 0
    for (corp_id, userid), user_ids in sorted(groups.items()):
        if len(user_ids) < 2:
            continue
        # 保留最早创建（pk 最小）的那个账号，它在现网通常带着真实的业务引用
        user_ids = sorted(user_ids)
        for loser_id in user_ids[1:]:
            _merge(apps, user_ids[0], loser_id, corp_id, userid)
            n += 1

    if n:
        print(f'  去重完成：合并了 {n} 个重复账号')
    else:
        print('  去重：没有重复账号')


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_backfill_multicorp'),
    ]

    operations = [
        # 反向无法还原被合并的账号（业务引用已经改指），失败时从备份恢复
        migrations.RunPython(dedupe, migrations.RunPython.noop),
    ]
