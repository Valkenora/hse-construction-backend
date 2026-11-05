import logging

from django.db import transaction
from django.dispatch import receiver

from audit.services import event_logged
from . import services

logger = logging.getLogger(__name__)


def handle(resource, entry):
    services.on_event(resource, entry)


@receiver(event_logged, dispatch_uid='notifications.on_audit_event')
def on_audit_event(sender, resource, entry, **kwargs):
    """Runs in the caller's transaction, so a notification exists exactly when its change does.
    Notifications are best effort: a failure here is logged and never blocks an approval."""
    try:
        with transaction.atomic():          # savepoint: a database error can't poison the outer transaction
            handle(resource, entry)
    except Exception:
        logger.exception('Could not create notifications for %s #%s (%s)', entry.resource_type,
                         entry.resource_id, entry.action)
