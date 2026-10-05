from django.contrib import admin
from .models import HIRAActivity, HIRAApproval, HIRADocument, HIRAGroup


class HIRAApprovalInline(admin.TabularInline):
    model = HIRAApproval
    extra = 0
    can_delete = False
    readonly_fields = ['step', 'decision', 'decided_by', 'remarks', 'submission_round', 'decided_at']

    def has_add_permission(self, request, obj=None):
        return False


class HIRAGroupInline(admin.TabularInline):
    model = HIRAGroup
    extra = 0


@admin.register(HIRADocument)
class HIRADocumentAdmin(admin.ModelAdmin):
    list_display = ['document_number', 'title', 'project', 'status', 'created_by', 'document_date']
    list_filter = ['status', 'project']
    search_fields = ['document_number', 'title']
    list_select_related = ['project', 'created_by']
    readonly_fields = ['status', 'submission_round', 'created_by', 'cancelled_by',
                       'cancelled_at', 'created_at', 'updated_at']
    inlines = [HIRAGroupInline, HIRAApprovalInline]


class HIRAActivityInline(admin.StackedInline):
    model = HIRAActivity
    extra = 0
    readonly_fields = list(HIRAActivity.COMPUTED)


@admin.register(HIRAGroup)
class HIRAGroupAdmin(admin.ModelAdmin):
    list_display = ['location_function', 'hira_document']
    inlines = [HIRAActivityInline]