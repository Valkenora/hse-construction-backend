from django.contrib import admin
from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('id', 'recipient', 'kind', 'resource_type', 'resource_id', 'created_at', 'read_at', 'needs_action')
    list_filter = ('kind', 'resource_type', 'needs_action')
    search_fields = ('recipient__email', 'title', 'body')
    raw_id_fields = ('recipient', 'project')
