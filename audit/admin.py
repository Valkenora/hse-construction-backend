from django.contrib import admin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['timestamp', 'resource_type', 'resource_id', 'action', 'actor_label', 'project']
    list_filter = ['resource_type', 'action', 'project']
    search_fields = ['actor_label', 'remarks']
    date_hierarchy = 'timestamp'
    list_select_related = ['project']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False