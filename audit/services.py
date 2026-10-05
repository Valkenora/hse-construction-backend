from django.db import models, transaction
from .models import AuditLog


class Action:
    CREATED = 'created'
    UPDATED = 'updated'
    DELETED = 'deleted'
    SUBMITTED = 'submitted'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    CANCELLED = 'cancelled'
    # permits / MK / notifications add their own strings later


def snapshot_of(instance):
    """JSON-safe dict of all concrete fields, including non-editable computed ones."""
    data = {}
    for f in instance._meta.concrete_fields:
        value = f.value_from_object(instance)
        if isinstance(f, models.FileField):
            value = value.name if value else ''
        data[f.name] = value
    return data


def client_meta(request):
    if request is None:
        return None, ''
    fwd = request.META.get('HTTP_X_FORWARDED_FOR')
    ip = fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR')
    return ip or None, request.META.get('HTTP_USER_AGENT', '')[:255]


def actor_label_for(user):
    if user is None:
        return 'system'
    return f'{user.full_name} <{user.email}> ({user.role})'


def log_event(resource, action, *, user=None, request=None, remarks='',
              project=None, actor_label=None, with_snapshot=True):
    """Call inside the same transaction as the change being recorded."""
    if user is None and request is not None and request.user.is_authenticated:
        user = request.user
    if project is None:
        getter = getattr(resource, 'get_audit_project', None)
        project = getter() if getter else getattr(resource, 'project', None)
    ip, ua = client_meta(request)
    return AuditLog.objects.create(
        resource_type=resource._meta.label_lower,
        resource_id=resource.pk,
        action=action,
        project=project,
        user=user,
        actor_label=actor_label or actor_label_for(user),
        ip_address=ip,
        user_agent=ua,
        remarks=remarks,
        snapshot=snapshot_of(resource) if with_snapshot else {},
    )


class AuditedViewSetMixin:
    """Logs plain create/update/delete. Status transitions call log_event themselves."""

    @transaction.atomic
    def perform_create(self, serializer):
        instance = serializer.save()
        log_event(instance, Action.CREATED, request=self.request)

    @transaction.atomic
    def perform_update(self, serializer):
        instance = serializer.save()
        log_event(instance, Action.UPDATED, request=self.request)

    @transaction.atomic
    def perform_destroy(self, instance):
        log_event(instance, Action.DELETED, request=self.request)  # snapshot before it's gone
        instance.delete()