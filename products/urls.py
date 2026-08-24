from django.urls import path
from . import views

urlpatterns = [
    path('create/', views.product_create, name='product_create'),
    path('<int:pk>/', views.product_detail, name='product_detail'),
    path('<int:pk>/progress-modal/', views.product_progress_modal, name='product_progress_modal'),
    path('<int:pk>/progress-export.png', views.product_progress_export_png, name='product_progress_export_png'),
    path('<int:pk>/info-modal/', views.product_info_modal, name='product_info_modal'),
    path('stages/<int:stage_id>/detail-modal/', views.stage_detail_modal, name='stage_detail_modal'),
]

urlpatterns += [
    path('tasks/<int:task_id>/checklist-modal/', views.task_checklist_modal, name='task_checklist_modal'),
    path('tasks/<int:task_id>/complete/', views.task_complete, name='task_complete'),
    path('tasks/<int:task_id>/update-deadline/', views.task_update_deadline, name='task_update_deadline'),
    path('tasks/<int:task_id>/update-field/', views.task_update_field, name='task_update_field'),
    path('tasks/<int:task_id>/upload-attachment/', views.task_upload_attachment, name='task_upload_attachment'),
    path('attachments/<int:attachment_id>/download/', views.attachment_download, name='attachment_download'),
    path('tasks/<int:task_id>/move/<str:direction>/', views.task_move, name='task_move'),
    path('tasks/<int:task_id>/delete/', views.task_delete, name='task_delete'),
    path('tasks/<int:task_id>/checklist/add/', views.checklist_item_add, name='checklist_item_add'),
    path('checklist/<int:item_id>/toggle/', views.checklist_item_toggle, name='checklist_item_toggle'),
    path('checklist/<int:item_id>/delete/', views.checklist_item_delete, name='checklist_item_delete'),
    path('checklist/<int:item_id>/log/add/', views.checklist_log_add, name='checklist_log_add'),
    path('checklist/<int:item_id>/save-notes/', views.checklist_item_save_notes, name='checklist_item_save_notes'),
    path('tasks/<int:task_id>/save-all-notes/', views.task_save_all_notes, name='task_save_all_notes'),
    path('stages/<int:stage_id>/add-task/', views.stage_add_task, name='stage_add_task'),
    path('stages/<int:stage_id>/complete/', views.stage_complete, name='stage_complete'),
    path('stages/<int:stage_id>/start/', views.stage_start, name='stage_start'),
    path('stages/<int:stage_id>/update-field/', views.stage_update_field, name='stage_update_field'),
    path('<int:pk>/cancel/', views.product_cancel, name='product_cancel'),
    path('<int:pk>/delete/', views.product_delete, name='product_delete'),
    path('<int:pk>/publish/', views.product_publish, name='product_publish'),
    path('<int:pk>/update-field/', views.product_update_field, name='product_update_field'),
    path('<int:pk>/update-profile/', views.product_update_profile, name='product_update_profile'),
]
