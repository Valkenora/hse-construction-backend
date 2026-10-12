from django.contrib import admin
from .models import Permit, PermitApproval


class PermitApprovalInline(admin.TabularInline):
    model = PermitApproval
    extra = 0
    can_delete = False
    readonly_fields = ['step', 'decision', 'decided_by', 'remarks', 'submission_round', 'decided_at']

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Permit)
class PermitAdmin(admin.ModelAdmin):
    list_display = ['permit_type', 'work_location', 'project', 'status', 'risk_category', 'created_by', 'work_date']
    list_filter = ['status', 'permit_type', 'risk_category', 'project']
    search_fields = ['document_number', 'work_location', 'work_description']
    list_select_related = ['project', 'created_by']
    readonly_fields = ['status', 'submission_round', 'risk_category', 'created_by', 'cancelled_by',
                       'cancelled_at', 'closed_by', 'closed_at', 'close_requested_at',
                       'created_at', 'updated_at']
    inlines = [PermitApprovalInline]