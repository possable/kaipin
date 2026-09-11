import time
import requests
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

ACCESS_TOKEN_URL = 'https://qyapi.weixin.qq.com/cgi-bin/gettoken'
MESSAGE_SEND_URL = 'https://qyapi.weixin.qq.com/cgi-bin/message/send'

# access_token 进程内缓存（提前 5 分钟过期）。
# gunicorn sync worker 单线程处理请求，多进程各自缓存，可接受。
_token_cache = {'token': None, 'expires_at': 0}


def get_access_token():
    """获取企业微信 access_token（带进程内缓存，默认有效期 7200 秒）"""
    now = time.time()
    if _token_cache['token'] and now < _token_cache['expires_at']:
        return _token_cache['token']
    resp = requests.get(ACCESS_TOKEN_URL, params={
        'corpid': settings.WECHAT_CORP_ID,
        'corpsecret': settings.WECHAT_APP_SECRET,
    }, timeout=10)
    data = resp.json()
    if data.get('errcode') != 0:
        logger.error(f'获取企业微信 token 失败: {data}')
        return None
    _token_cache['token'] = data['access_token']
    _token_cache['expires_at'] = now + int(data.get('expires_in', 7200)) - 300
    return _token_cache['token']


def build_oauth_url(redirect_uri, state=''):
    """构建企业微信 OAuth 授权链接（静默授权，只获取 userid）"""
    from urllib.parse import quote
    encoded_redirect = quote(redirect_uri, safe='')
    url = (
        f'https://open.weixin.qq.com/connect/oauth2/authorize'
        f'?appid={settings.WECHAT_CORP_ID}'
        f'&redirect_uri={encoded_redirect}'
        f'&response_type=code'
        f'&scope=snsapi_base'
        f'&agentid={settings.WECHAT_AGENT_ID}'
        f'&state={state}'
        f'#wechat_redirect'
    )
    return url


def get_userid_by_code(code):
    """用 OAuth code 换取企业微信 userid；仅返回企业成员 UserId，非成员返回 None。"""
    token = get_access_token()
    if not token:
        return None
    resp = requests.get(
        'https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo',
        params={'access_token': token, 'code': code},
        timeout=10,
    )
    data = resp.json()
    if data.get('errcode') != 0:
        logger.error(f'获取 userid 失败: {data}')
        return None
    if data.get('UserId'):
        return data['UserId']
    # 只返回 OpenId：用户不在企业通讯录中（或不在应用可见范围内），拒绝登录
    if data.get('OpenId'):
        logger.warning('企微 OAuth 返回 OpenId（非企业成员），拒绝登录: %s', data['OpenId'])
    return None


def get_user_detail(userid):
    """获取企业微信用户详细信息（姓名、部门、头像等）"""
    token = get_access_token()
    if not token:
        return None
    resp = requests.get(
        'https://qyapi.weixin.qq.com/cgi-bin/user/get',
        params={'access_token': token, 'userid': userid},
        timeout=10,
    )
    data = resp.json()
    if data.get('errcode') != 0:
        logger.error(f'获取用户详情失败: {data}')
        return None
    return data


def get_department_list(token=None):
    """获取企业微信全部部门列表"""
    if token is None:
        token = get_access_token()
    if not token:
        return None
    resp = requests.get(
        'https://qyapi.weixin.qq.com/cgi-bin/department/list',
        params={'access_token': token, 'id': ''},
        timeout=30,
    )
    data = resp.json()
    if data.get('errcode') != 0:
        logger.error(f'获取部门列表失败: {data}')
        return None
    return data.get('department', [])


def get_department_users(department_id, token=None, fetch_child=True):
    """获取指定部门及其子部门的所有用户详情"""
    if token is None:
        token = get_access_token()
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
        logger.error(f'获取部门用户失败: {data}')
        return None
    return data.get('userlist', [])


def send_wechat_message(userid, content):
    """
    向指定用户发送企业微信文本消息。
    返回 True 表示发送成功，False 表示失败（网络错误或 API 返回错误）。
    """
    token = get_access_token()
    if not token:
        return False

    payload = {
        'touser': userid,
        'msgtype': 'text',
        'agentid': settings.WECHAT_AGENT_ID,
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
            logger.error(f'企业微信消息发送失败: {data}')
            return False
        return True
    except requests.RequestException as e:
        logger.error(f'企业微信消息发送网络异常: {e}')
        return False
