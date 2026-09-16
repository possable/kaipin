from django.urls import path
from django.contrib.auth import views as auth_views
from django.views.generic import RedirectView
from . import views

urlpatterns = [
    path('login/', views.wechat_login, name='login'),
    path('login/password/', auth_views.LoginView.as_view(), name='login_password'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('auto-login/', views.auto_login, name='auto_login'),
    # 按企业登录入口：三个企业的工作台各配一个，如 /accounts/c/b-corp/
    path('c/<slug:code>/', views.corp_login, name='corp_login'),
    path('wechat-login/', views.wechat_login, name='wechat_login'),
    # 自助改密已下线（全员企微免密登录）。保留旧地址重定向回看板，避免旧书签/旧标签页撞 404。
    path('change-password/', RedirectView.as_view(pattern_name='kanban', permanent=False)),
    path('users/', views.user_list, name='user_list'),
    path('users/<int:user_id>/reset-password/', views.reset_user_password, name='reset_user_password'),
    path('users/<int:user_id>/toggle-admin/', views.toggle_admin_role, name='toggle_admin_role'),
    path('todos/', views.todo_list, name='todo_list'),
    path('todos/add/', views.todo_add, name='todo_add'),
    path('todos/<int:todo_id>/toggle/', views.todo_toggle, name='todo_toggle'),
    path('todos/<int:todo_id>/delete/', views.todo_delete, name='todo_delete'),
    path('announcements/', views.announcement_list, name='announcement_list'),
    path('announcements/add/', views.announcement_create, name='announcement_create'),
    path('announcements/<int:pk>/edit/', views.announcement_edit, name='announcement_edit'),
    path('announcements/<int:pk>/delete/', views.announcement_delete, name='announcement_delete'),
]
