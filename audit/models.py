from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone


class ImmutableError(Exception):
    pass


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableError('AuditLog rows cannot be updated.')

    def delete(self):
        raise ImmutableError('AuditLog rows cannot be deleted.')

    def bulk_update(self, *args, **kwargs):
        raise ImmutableError('AuditLog rows cannot be updated.')


class AuditLog(models.Model):
    resource_type = models.CharField(max_length=60)        # e.g. 'hira.hiradocument'
    resource_id   = models.PositiveBigIntegerField()
    action        = models.CharField(max_length=50)        # see audit.services.Action
    project       = models.ForeignKey('projects.Project', null=True, blank=True,
                                      on_delete=models.PROTECT, related_name='audit_logs')

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.PROTECT, related_name='audit_logs')
    # Text snapshot of the actor, so history survives edits and covers MK (no account).
    actor_label = models.CharField(max_length=255)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    user_agent  = models.CharField(max_length=255, blank=True)

    timestamp = models.DateTimeField(default=timezone.now, editable=False)
    remarks   = models.TextField(blank=True)
    snapshot  = models.JSONField(encoder=DjangoJSONEncoder, default=dict, blank=True)

    objects = AuditQuerySet.as_manager()

    class Meta:
        ordering = ['-timestamp', '-id']
        indexes = [
            models.Index(fields=['resource_type', 'resource_id']),
            models.Index(fields=['timestamp']),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableError('AuditLog rows cannot be modified.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableError('AuditLog rows cannot be deleted.')

    def __str__(self):
        return f'{self.resource_type}#{self.resource_id} {self.action} by {self.actor_label}'