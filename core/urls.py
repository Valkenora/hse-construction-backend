from django.contrib import admin
from django.urls import path, include
from .health import healthz

urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('admin/', admin.site.urls),
    path('api/users/', include('users.urls')),
    path('api/audit/', include('audit.urls')),
    path('api/dashboard/', include('dashboard.urls')),
    path('api/notifications/', include('notifications.urls')),
    path('api/projects/', include('projects.urls')),
    path('api/hira/', include('hira.urls')),
    path('api/permits/', include('permits.urls')),
    path('api/mk/', include('permits.mk_urls')),     # public: MK approval link + OTP
]