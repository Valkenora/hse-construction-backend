from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/users/', include('users.urls')),
    path('api/audit/', include('audit.urls')),
    path('api/projects/', include('projects.urls')),
    path('api/hira/', include('hira.urls')),
]