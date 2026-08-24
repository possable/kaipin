"""大小写不敏感的密码认证后端"""
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User


class CaseInsensitiveModelBackend(ModelBackend):
    """用户名大小写不敏感认证"""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get('username')
        if username is None or password is None:
            return
        try:
            user = User.objects.get(username__iexact=username)
        except User.DoesNotExist:
            return
        except User.MultipleObjectsReturned:
            # 如果大小写不同导致多个匹配，取第一个
            user = User.objects.filter(username__iexact=username).first()
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
