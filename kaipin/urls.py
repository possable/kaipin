from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('products/', include('products.urls')),
    path('templates/', include('templates_app.urls')),
    path('activity/', include('activity_log.urls')),
    path('reminders/', include('reminders.urls')),
    path('', include('dashboard.urls')),
]
