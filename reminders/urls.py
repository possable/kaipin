from django.urls import path
from . import views

urlpatterns = [
    path('send/<str:entity_type>/<int:entity_id>/', views.send_message, name='send_wechat_message_view'),
]
