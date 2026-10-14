from django.contrib import admin

from . import services
from .models import HIRAActivity, HIRAApproval, HIRADocument, HIRAGroup
from .selectors import visible_documents

# The admin is an inspection window. HIRA content, status and approvals change only
# through the API, because that is where the edit lock, the approval rules and the
# audit log live. The one exception is the document number (the client's office code),
# which HSE Officer / Coordinator set here, and every change is written to the audit log.


class ReadOnlyMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def scoped(request, qs, path):
    """Limit `qs` to HIRAs this user may see (same rule as the API). `path` is the lookup
    from `qs` to the document, e.g. 'pk' or 'group__hira_document'. Superusers see all."""
    if request.user.is_superuser:
        return qs
    return qs.filter(**{f'{path}__in': visible_documents(request.user)})


class HIRAGroupInline(ReadOnlyMixin, admin.TabularInline):
    model = HIRAGroup
    extra = 0
    show_change_link = True   # opens the group page, which lists its activities


class HIRAApprovalInline(ReadOnlyMixin, admin.TabularInline):
    model = HIRAApproval
    extra = 0
    readonly_fields = ['decided_at']


@admin.register(HIRADocument)
class HIRADocumentAdmin(admin.ModelAdmin):
    list_display = ['document_number', 'work_package', 'work_area', 'project', 'status',
                    'submission_round', 'created_by', 'document_date']
    list_filter = ['status', 'project']
    search_fields = ['document_number', 'work_package', 'work_area']
    list_select_related = ['project', 'created_by']
    inlines = [HIRAGroupInline, HIRAApprovalInline]
    editable_fields = ('document_number',)

    def get_queryset(self, request):
        return scoped(request, super().get_queryset(request), 'pk')

    def get_readonly_fields(self, request, obj=None):
        names = [f.name for f in self.model._meta.concrete_fields if f.name != 'id']
        if obj is not None and obj.status != HIRADocument.Status.CANCELLED:
            names = [n for n in names if n not in self.editable_fields]
        return names

    def has_add_permission(self, request):
        return False   # created by Subkontraktor through the API

    def has_delete_permission(self, request, obj=None):
        return False   # the API allows deleting only a never-submitted draft

    def save_model(self, request, obj, form, change):
        # Never obj.save(): set_number locks the row, saves only the number and writes
        # the audit entry with the old and new value.
        if 'document_number' in form.changed_data:
            services.set_number(obj, request.user, request, form.cleaned_data['document_number'])


class HIRAActivityInline(ReadOnlyMixin, admin.StackedInline):
    model = HIRAActivity
    extra = 0
    readonly_fields = [*HIRAActivity.COMPUTED, 'created_at', 'updated_at']


@admin.register(HIRAGroup)
class HIRAGroupAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ['location_function', 'hira_document']
    search_fields = ['location_function', 'hira_document__work_package']
    list_select_related = ['hira_document']
    inlines = [HIRAActivityInline]

    def get_queryset(self, request):
        return scoped(request, super().get_queryset(request), 'hira_document')


@admin.register(HIRAActivity)
class HIRAActivityAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ['activity_name', 'activity_type', 'document', 'group',
                    'initial_score', 'initial_category',
                    'residual_score', 'residual_category', 'residual_acceptable']
    list_filter = ['activity_type', 'initial_category', 'residual_category', 'residual_acceptable']
    search_fields = ['activity_name', 'potential_hazard', 'group__hira_document__work_package']
    list_select_related = ['group__hira_document']
    readonly_fields = [*HIRAActivity.COMPUTED, 'created_at', 'updated_at']

    @admin.display(description='HIRA document', ordering='group__hira_document')
    def document(self, obj):
        return obj.group.hira_document

    def get_queryset(self, request):
        return scoped(request, super().get_queryset(request), 'group__hira_document')


@admin.register(HIRAApproval)
class HIRAApprovalAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ['hira_document', 'step', 'decision', 'decided_by',
                    'submission_round', 'decided_at']
    list_filter = ['step', 'decision']
    search_fields = ['hira_document__work_package', 'hira_document__document_number', 'remarks']
    list_select_related = ['hira_document', 'decided_by']
    readonly_fields = ['decided_at']

    def get_queryset(self, request):
        return scoped(request, super().get_queryset(request), 'hira_document')
