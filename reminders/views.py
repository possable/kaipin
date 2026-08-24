from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from products.models import Product, ProductStage, Task
from products.views import _check_task_permission
from activity_log.utils import log_action
from .wechat import send_wechat_message

ENTITY_MODELS = {
    'product': Product,
    'stage': ProductStage,
    'task': Task,
}


def _can_message_entity(user, entity_type, entity):
    """发消息权限与编辑权限同步：谁能编辑这个人负责的东西，谁就能给这个人发消息"""
    if entity_type == 'product':
        return entity.can_be_managed_by(user)
    if entity_type == 'stage':
        return entity.can_be_managed_by(user)
    if entity_type == 'task':
        return _check_task_permission(user, entity)
    return False


@login_required
@require_POST
def send_message(request, entity_type, entity_id):
    """向品/阶段/任务的负责人发送一条企业微信消息。权限与该实体的编辑权限一致。"""
    model = ENTITY_MODELS.get(entity_type)
    if model is None:
        return JsonResponse({'error': '不支持的类型'}, status=400)
    entity = get_object_or_404(model, pk=entity_id)

    target = entity.assignee
    if not target:
        return JsonResponse({'error': '该负责人不存在'}, status=400)

    if not _can_message_entity(request.user, entity_type, entity):
        return JsonResponse({'error': '无权限对此负责人发消息'}, status=403)

    wechat_id = target.profile.wechat_userid
    if not wechat_id:
        return JsonResponse({'error': '该用户尚未绑定企业微信，无法发送'}, status=400)

    content = request.POST.get('content', '').strip()
    if not content:
        return JsonResponse({'error': '消息内容不能为空'}, status=400)

    sender_name = request.user.first_name or request.user.username
    full_content = f'{sender_name} 在产品管理系统给你留言：\n{content}'

    success = send_wechat_message(wechat_id, full_content)
    if not success:
        return JsonResponse({'error': '发送失败，请稍后重试'}, status=502)

    target_name = target.first_name or target.username
    log_action(request.user, '发送企微消息', entity_type, entity.id,
               f'致 {target_name}', content)
    return JsonResponse({'success': True})
