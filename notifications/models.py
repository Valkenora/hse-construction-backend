from django.conf import settings
from django.db import models
from django.utils import timezone


class Notification(models.Model):
    """One row per recipient. "Needs action" rows are cleared for everyone else as soon as one person
    handles the document, but kept, with a note saying who did."""

    class Kind(models.TextChoices):
        ACTION_NEEDED = 'action_needed', 'Perlu tindakan'
        SUBMITTED     = 'submitted',     'Dokumen baru diajukan'
        APPROVED      = 'approved',      'Disetujui'
        REJECTED      = 'rejected',      'Ditolak'
        CLOSED        = 'closed',        'Ditutup'
        CANCELLED     = 'cancelled',     'Dibatalkan'

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    kind = models.CharField(max_length=20, choices=Kind.choices)
    resource_type = models.CharField(max_length=10)          # 'permit' | 'hira' (same keys as the dashboard)
    resource_id = models.PositiveBigIntegerField()
    project = models.ForeignKey('projects.Project', null=True, blank=True, on_delete=models.CASCADE,
                                related_name='notifications')
    status = models.CharField(max_length=30, blank=True)     # the document's status when this was sent
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    read_at = models.DateTimeField(null=True, blank=True)

    needs_action = models.BooleanField(default=False)        # true until someone handles the document
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by_name = models.CharField(max_length=150, blank=True)
    resolution_note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['recipient', 'read_at']),
            models.Index(fields=['resource_type', 'resource_id', 'needs_action']),
        ]

    def __str__(self):
        return f'{self.recipient_id}: {self.kind} {self.resource_type}#{self.resource_id}'
