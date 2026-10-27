from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/users/', include('users.urls')),
    path('api/audit/', include('audit.urls')),
    path('api/projects/', include('projects.urls')),
    path('api/hira/', include('hira.urls')),
    path('api/permits/', include('permits.urls')),
    path('api/mk/', include('permits.mk_urls')),     # public: MK approval link + OTP
]