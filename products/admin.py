from django.contrib import admin
from .models import Product, ProductStage, Task, TaskAttachment, TaskChecklistItem, TaskChecklistLog


class ProductStageInline(admin.TabularInline):
    model = ProductStage
    extra = 0


class TaskInline(admin.TabularInline):
    model = Task
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['name', 'creator', 'current_stage_order', 'status', 'created_at']
    inlines = [ProductStageInline]


@admin.register(ProductStage)
class ProductStageAdmin(admin.ModelAdmin):
    list_display = ['product', 'name', 'order', 'department', 'status']
    inlines = [TaskInline]


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ['name', 'product_stage', 'deadline', 'status']


@admin.register(TaskAttachment)
class TaskAttachmentAdmin(admin.ModelAdmin):
    list_display = ['task', 'file', 'uploaded_by', 'uploaded_at']


@admin.register(TaskChecklistItem)
class TaskChecklistItemAdmin(admin.ModelAdmin):
    list_display = ['name', 'task', 'is_done', 'completed_at']


@admin.register(TaskChecklistLog)
class TaskChecklistLogAdmin(admin.ModelAdmin):
    list_display = ['item', 'user', 'content', 'created_at']
