from django.contrib import admin

from . import services
from .models import (MKApprovalRequest, Permit, PermitApproval, PermitAttachment, PermitGeoStamp,
                     PermitHIRAActivity, PermitPhoto)
from .selectors import visible_permits

# The admin is an inspection window. Permit content, status and approvals change only
# through the API, where the edit lock, routing rules and audit log live. The one
# exception is the document number (set by HSE Officer / Coordinator), and every change
# goes through services.set_number so it is audited.


class ReadOnlyMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def scoped(request, qs, path):
    """Limit `qs` to permits this user may see (same rule as the API). Superusers see all."""
    if request.user.is_superuser:
        return qs
    return qs.filter(**{f'{path}__in': visible_permits(request.user)})


class PermitApprovalInline(ReadOnlyMixin, admin.TabularInline):
    model = PermitApproval
    extra = 0
    readonly_fields = ['decided_at']


class PermitPhotoInline(ReadOnlyMixin, admin.TabularInline):
    model = PermitPhoto
    extra = 0
    fields = readonly_fields = ['stage', 'storage_path', 'original_name', 'size', 'uploaded_by', 'uploaded_at']


class PermitAttachmentInline(ReadOnlyMixin, admin.TabularInline):
    model = PermitAttachment
    extra = 0
    fields = readonly_fields = ['kind', 'storage_path', 'original_name', 'size', 'uploaded_by', 'uploaded_at']


class PermitHIRAActivityInline(ReadOnlyMixin, admin.TabularInline):
    model = PermitHIRAActivity
    extra = 0
    fields = readonly_fields = ['hira_activity']


class PermitGeoStampInline(ReadOnlyMixin, admin.TabularInline):
    model = PermitGeoStamp
    extra = 0
    fields = readonly_fields = ['step', 'submission_round', 'latitude', 'longitude', 'accuracy',
                                'captured_at', 'recorded_by', 'recorded_at']


class MKApprovalRequestInline(ReadOnlyMixin, admin.TabularInline):
    model = MKApprovalRequest
    extra = 0
    # Never show the token or code hashes.
    fields = readonly_fields = ['contact', 'submission_round', 'status', 'expires_at', 'email_sent_at',
                                'email_error', 'otp_sent_count', 'created_at']


@admin.register(Permit)
class PermitAdmin(admin.ModelAdmin):
    list_display = ['permit_type', 'work_location', 'project', 'status', 'risk_category',
                    'document_number', 'created_by', 'work_date']
    list_filter = ['status', 'permit_type', 'risk_category', 'project']
    search_fields = ['document_number', 'work_location', 'work_description']
    list_select_related = ['project', 'created_by']
    inlines = [PermitHIRAActivityInline, PermitApprovalInline, PermitPhotoInline, PermitAttachmentInline,
               PermitGeoStampInline, MKApprovalRequestInline]
    editable_fields = ('document_number',)

    def get_queryset(self, request):
        return scoped(request, super().get_queryset(request), 'pk')

    def get_readonly_fields(self, request, obj=None):
        names = [f.name for f in self.model._meta.concrete_fields if f.name != 'id']
        if (obj is not None and obj.status != Permit.Status.CANCELLED
                and obj.project.is_active):
            names = [n for n in names if n not in self.editable_fields]
        return names

    def has_add_permission(self, request):
        return False   # created by Subkontraktor through the API

    def has_delete_permission(self, request, obj=None):
        return False   # the API allows deleting only a never-submitted draft

    def save_model(self, request, obj, form, change):
        # Never obj.save(): set_number locks the row, saves only the number and audits it.
        if 'document_number' in form.changed_data:
            services.set_number(obj, request.user, request, form.cleaned_data['document_number'])


@admin.register(PermitApproval)
class PermitApprovalAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ['permit', 'step', 'decision', 'decided_by', 'external_name', 'submission_round', 'decided_at']
    list_filter = ['step', 'decision']
    search_fields = ['permit__document_number', 'permit__work_location', 'remarks']
    list_select_related = ['permit', 'decided_by']
    readonly_fields = ['decided_at']

    def get_queryset(self, request):
        return scoped(request, super().get_queryset(request), 'permit')
