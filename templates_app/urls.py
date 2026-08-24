from django.urls import path
from . import views

urlpatterns = [
    path('stages/', views.stage_template_list, name='stage_template_list'),
    path('stages/create/', views.stage_template_create, name='stage_template_create'),
    path('stages/<int:pk>/edit/', views.stage_template_edit, name='stage_template_edit'),
    path('stages/<int:pk>/delete/', views.stage_template_delete, name='stage_template_delete'),
    path('stages/<int:stage_pk>/tasks/', views.task_template_list, name='task_template_list'),
    path('stages/<int:stage_pk>/tasks/create/', views.task_template_create, name='task_template_create'),
    path('tasks/<int:pk>/edit/', views.task_template_edit, name='task_template_edit'),
    path('tasks/<int:pk>/delete/', views.task_template_delete, name='task_template_delete'),
    path('tasks/<int:task_pk>/checklist/', views.checklist_template_list, name='checklist_template_list'),
    path('tasks/<int:task_pk>/checklist/create/', views.checklist_template_create, name='checklist_template_create'),
    path('checklist/<int:pk>/edit/', views.checklist_template_edit, name='checklist_template_edit'),
    path('checklist/<int:pk>/delete/', views.checklist_template_delete, name='checklist_template_delete'),
]
