"""企业微信企业的解析入口。

改造前 corp_id / agent_id / app_secret 是 settings 里的三个全局常量，只支持一个企业。
现在落到 WeComCorp 表里，管理员可自助增删企业，不用改 .env + 重启。

**表为空时回退到 settings** —— 这样本模块可以在迁移回填之前就被引用，
且改造前后行为逐字节一致。回退产出的是一条**未保存**的 WeComCorp 实例，
调用方只读字段，不要 save()。

这里不做进程内缓存：调用点只有登录和发消息两处，一天几十次，
一次主键查询的开销远小于缓存失效带来的「改了企业配置不生效」的困惑。
真到了热路径再加。
"""
from django.conf import settings

from .models import WeComCorp


def _fallback_corp():
    """用 settings 里的 WECHAT_* 拼一条未保存的默认企业，供表为空时兜底。"""
    return WeComCorp(
        code='default',
        name='默认企业',
        corp_id=getattr(settings, 'WECHAT_CORP_ID', '') or '',
        agent_id=str(getattr(settings, 'WECHAT_AGENT_ID', '') or ''),
        app_secret=getattr(settings, 'WECHAT_APP_SECRET', '') or '',
        is_active=True,
        is_default=True,
    )


def default_corp():
    """默认企业：优先 is_default，其次任意一条启用的，最后回退 settings。

    旧入口 /accounts/auto-login/ 用的就是它 —— 现网企微工作台配的是那个 URL，
    不能断，所以它必须永远返回一个可用企业。
    """
    corp = WeComCorp.objects.filter(is_default=True, is_active=True).first()
    if corp is not None:
        return corp
    corp = WeComCorp.objects.filter(is_active=True).first()
    if corp is not None:
        return corp
    return _fallback_corp()


class UnknownCorp(Exception):
    """入口 URL 上的 code 不在表里，或该企业已停用。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def get_corp_strict(code):
    """按 code 取企业，取不到就抛 UnknownCorp —— 只给 `/accounts/c/<code>/` 用。

    `get_corp` 的「查不到就落默认企业」对 `/accounts/auto-login/`（那里 code
    本来就是空的）是对的；但对带 code 的入口 URL 是**帮凶**：管理员把 code 填错时，
    员工会被静默送去另一个企业的 OAuth，拿回 OpenId 再被拒，最后卡在「登录失败」。
    2026-09-15 B 企业登不进去就是这个成因 —— 后台 code 填的是 corp_id，
    工作台配的是 b-corp，两边对不上，日志里只看到一句「返回 OpenId，拒绝登录」。
    入口 code 来自 URL，错了就当场说清楚，不能替用户猜一个企业。
    """
    corp = WeComCorp.objects.filter(code=code, is_active=True).first()
    if corp is None:
        raise UnknownCorp(code)
    return corp


def get_corp(code=None):
    """按 code 取企业；code 为空、查不到、或已停用，都落到默认企业。

    「查不到就落默认」而不是报错，是刻意的：入口 URL 由管理员在企微后台手工填写，
    填错的 code 不应该变成 500 页面，落到默认企业至少能登进去。
    """
    if code:
        corp = WeComCorp.objects.filter(code=code, is_active=True).first()
        if corp is not None:
            return corp
    return default_corp()


def resolve_corp(request, code=None):
    """⚠️ **目前没有生产调用点**，只有测试在用 —— 别照这份说明去推断线上行为。

    它描述的是「URL → session → 默认企业」的兜底顺序，但实际的两个入口都
    **不需要** session 兜底，因为回调路径本身就带企业标识：

      * `/accounts/c/<code>/` 走 `get_corp_strict(code)`，企业来自路径，回调还回这个路径；
      * `/accounts/auto-login/` 走 `default_corp()`，**永远**是默认企业。

    所以「session 里存的企业」只被用来做一件事：`corp_login` 里比对
    `session` 与路径上的 code 是否一致，不一致就强制重走 OAuth
    （同一台手机三个企业共用一个 cookie jar，不重走就会拿着 A 的身份进 B）。
    真要按 URL→session 兜底，得先有「路径不带企业」的入口，现在没有。
    """
    return get_corp(code or request.session.get('wecom_corp_code'))
