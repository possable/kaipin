"""企业微信 API 封装。

支持多个企业：每个函数都接受一个可选的 corp 参数（WeComCorp 实例）。
**不传 corp 时落到默认企业**，所以单企业时代的调用点一行都不用改，
三个企业共用一套项目数据时也能平滑接进来。

token 缓存按企业分键，键里带上 app_secret —— 这样在后台重置了 Secret
不需要重启进程，新的 secret 天然是一个新的缓存键，旧 token 自然失效。
"""
import hashlib
import time
import requests
import logging

logger = logging.getLogger(__name__)

ACCESS_TOKEN_URL = 'https://qyapi.weixin.qq.com/cgi-bin/gettoken'
MESSAGE_SEND_URL = 'https://qyapi.weixin.qq.com/cgi-bin/message/send'

# access_token 进程内缓存（提前 5 分钟过期），按企业分键。
# gunicorn sync worker 单线程处理请求，多进程各自缓存，可接受 ——
# 每个进程最多多取几次 token，不会互相踩。
_token_cache = {}


def _cache_key(corp):
    """缓存键带 app_secret 是为了让「重置 Secret」立即生效，不用重启进程。"""
    secret_fp = hashlib.sha256(corp.app_secret.encode('utf-8')).hexdigest()[:16]
    return (corp.code, corp.corp_id, secret_fp)


# 60020 = 来源 IP 不在「企业可信 IP」白名单里。
# ⚠️ 2026-09-15 实测：**gettoken 不受这条限制**。C 企业接入时三家的 gettoken 全是
# errcode=0，只有 department/list 报了 60020 —— 所以这个提示必须挂在业务接口上。
# 原先它挂在 get_access_token 里并写着「60020 是最常见的失败」，那永远不会触发，
# 真正的失败点反而只说了一句原始 JSON，运维看不出该怎么办。
IP_NOT_ALLOWED = 60020


def _log_api_error(what, corp, data):
    """业务接口报错的统一出口。60020 单独说人话 —— 原始 errcode 对运维没有可操作性。"""
    code = getattr(corp, 'code', None) or '默认企业'
    if data.get('errcode') == IP_NOT_ALLOWED:
        logger.error(
            '[%s] %s失败：errcode 60020 —— 服务器出口 IP 不在该企业的「企业可信 IP」'
            '白名单里。到 企业微信管理后台 → 应用管理 → 自建应用 → 企业可信IP '
            '把本机出口 IP 加进去即可。注意 gettoken 不受此限制，只会在业务接口报错。'
            '原始返回: %s', code, what, data)
    else:
        logger.error('[%s] %s失败: %s', code, what, data)


def get_access_token(corp=None):
    """获取指定企业的 access_token（带进程内缓存，默认有效期 7200 秒）"""
    if corp is None:
        from accounts.wecom import default_corp
        corp = default_corp()

    key = _cache_key(corp)
    now = time.time()
    cached = _token_cache.get(key)
    if cached and cached['token'] and now < cached['expires_at']:
        return cached['token']

    resp = requests.get(ACCESS_TOKEN_URL, params={
        'corpid': corp.corp_id,
        'corpsecret': corp.app_secret,
    }, timeout=10)
    data = resp.json()
    if data.get('errcode') != 0:
        _log_api_error('获取企业微信 token', corp, data)
        return None
    _token_cache[key] = {
        'token': data['access_token'],
        'expires_at': now + int(data.get('expires_in', 7200)) - 300,
    }
    return data['access_token']


def build_oauth_url(redirect_uri, state='', corp=None):
    """构建企业微信 OAuth 授权链接（静默授权，只获取 userid）"""
    from urllib.parse import quote
    from accounts.wecom import default_corp
    if corp is None:
        corp = default_corp()
    encoded_redirect = quote(redirect_uri, safe='')
    url = (
        f'https://open.weixin.qq.com/connect/oauth2/authorize'
        f'?appid={corp.corp_id}'
        f'&redirect_uri={encoded_redirect}'
        f'&response_type=code'
        f'&scope=snsapi_base'
        f'&agentid={corp.agent_id}'
        f'&state={state}'
        f'#wechat_redirect'
    )
    return url


def get_userid_by_code(code, corp=None):
    """用 OAuth code 换取企业微信 userid；仅返回企业成员 UserId，非成员返回 None。

    ⚠️ 这个 code 只能拿**同一个企业**的 token 去换。用别的企业的 token 换，
    企微会返回 OpenId 而不是 UserId，表现为「登录失败」。
    """
    token = get_access_token(corp)
    if not token:
        return None
    resp = requests.get(
        'https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo',
        params={'access_token': token, 'code': code},
        timeout=10,
    )
    data = resp.json()
    if data.get('errcode') != 0:
        logger.error('获取 userid 失败 [%s]: %s', getattr(corp, 'code', None) or '默认企业', data)
        return None
    if data.get('UserId'):
        return data['UserId']
    # 只返回 OpenId：用户不在企业通讯录中（或不在应用可见范围内），拒绝登录
    if data.get('OpenId'):
        logger.warning('企微 OAuth 返回 OpenId（非企业成员），拒绝登录: %s', data['OpenId'])
    return None


def get_user_detail(userid, corp=None):
    """获取企业微信用户详细信息（姓名、部门、头像等）"""
    token = get_access_token(corp)
    if not token:
        return None
    resp = requests.get(
        'https://qyapi.weixin.qq.com/cgi-bin/user/get',
        params={'access_token': token, 'userid': userid},
        timeout=10,
    )
    data = resp.json()
    if data.get('errcode') != 0:
        _log_api_error('获取用户详情', corp, data)
        return None
    return data


def get_department_list(token=None, corp=None):
    """获取指定企业的全部部门列表"""
    if token is None:
        token = get_access_token(corp)
    if not token:
        return None
    resp = requests.get(
        'https://qyapi.weixin.qq.com/cgi-bin/department/list',
        params={'access_token': token, 'id': ''},
        timeout=30,
    )
    data = resp.json()
    if data.get('errcode') != 0:
        _log_api_error('获取部门列表', corp, data)
        return None
    return data.get('department', [])


def get_department_users(department_id, token=None, fetch_child=True, corp=None):
    """获取指定部门及其子部门的所有用户详情"""
    if token is None:
        token = get_access_token(corp)
    if not token:
        return None
    resp = requests.get(
        'https://qyapi.weixin.qq.com/cgi-bin/user/list',
        params={
            'access_token': token,
            'department_id': department_id,
            'fetch_child': 1 if fetch_child else 0,
        },
        timeout=30,
    )
    data = resp.json()
    if data.get('errcode') != 0:
        _log_api_error('获取部门用户', corp, data)
        return None
    return data.get('userlist', [])


def send_wechat_message(userid, content, corp=None):
    """
    向指定企业的指定用户发送文本消息。
    返回 True 表示发送成功，False 表示失败（网络错误或 API 返回错误）。

    ⚠️ 收件人必须在 corp 这个企业的通讯录里，否则企微返回 81013 之类的错误。
    调用方拿不准该用哪个企业时，用 accounts.wecom 的 send_message_to_user(user, content)。
    """
    token = get_access_token(corp)
    if not token:
        return False

    from accounts.wecom import default_corp
    agent_id = (corp.agent_id if corp is not None else default_corp().agent_id)

    payload = {
        'touser': userid,
        'msgtype': 'text',
        'agentid': agent_id,
        'text': {'content': content},
    }
    try:
        resp = requests.post(
            MESSAGE_SEND_URL,
            params={'access_token': token},
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if data.get('errcode') != 0:
            _log_api_error('企业微信消息发送', corp, data)
            return False
        return True
    except requests.RequestException as e:
        logger.error('企业微信消息发送网络异常: %s', e)
        return False


def send_message_to_user(user, content):
    """按收件人的**主身份**发消息 —— 调用方不需要知道 corp 是哪个。

    这是防串号的护栏：用 A 企业的应用给 B 企业的人发消息，企微会返回
    「userid 不存在」，消息静默丢失。

    为什么要走身份表而不是 profile 字段：多企业的人在两边的 userid 可能不同
    （阮仕云 A=Ruanivan / B=ivanRuan，实测 B 企业有 5 个这样的人），
    profile 只能存其中一套，拿错就发不通。

    身份表里还没有这个人时退回 profile 上的字段，过渡期行为与改造前一致。
    """
    from accounts.utils import get_primary_identity

    ident = get_primary_identity(user)
    if ident is not None:
        return send_wechat_message(ident.userid, content, corp=ident.corp)

    profile = getattr(user, 'profile', None)
    wechat_userid = getattr(profile, 'wechat_userid', '')
    if not wechat_userid:
        return False

    corp = getattr(profile, 'wecom_corp', None)
    if corp is None:
        from accounts.wecom import default_corp
        corp = default_corp()

    return send_wechat_message(wechat_userid, content, corp=corp)
