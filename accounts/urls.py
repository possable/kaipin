from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('auto-login/', views.auto_login, name='auto_login'),
    path('wechat-login/', views.wechat_login, name='wechat_login'),
    path('change-password/', views.ChangePasswordView.as_view(), name='change_password'),
    path('change-password/done/', auth_views.PasswordChangeDoneView.as_view(
        template_name='registration/change_password_done.html',
    ), name='password_change_done'),
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
