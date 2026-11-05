from django.utils import timezone

from audit.labels import actor_name
from dashboard.queues import REVIEW, WAITING_ON
from projects.models import ProjectMember
from users.models import User
from . import text
from .models import Notification

K = Notification.Kind
R = User.Role
PROJECT_ROLES = (R.SUPERVISOR, R.PROJECT_MANAGER)        # tied to a project; the others are cross-project


def holders(rtype, status, project_id):
    """Users a document waiting in `status` is waiting on: everyone with that role in the project
    (Supervisor, PM), or every HSE Officer. Nobody for MK, who is reached by email."""
    if status not in REVIEW[rtype]:
        return User.objects.none()
    role = WAITING_ON[rtype].get(status)
    users = User.objects.filter(is_active=True, role=role)
    if role in PROJECT_ROLES:
        users = users.filter(project_memberships__project_id=project_id)
    elif role != R.HSE_OFFICER:
        return User.objects.none()
    return users.distinct()


def coordinators():
    return User.objects.filter(is_active=True, role=R.HSE_COORDINATOR)


def _send(users, kind, rtype, resource, title, body, actor, status='', needs_action=False):
    rows = [Notification(recipient=u, kind=kind, resource_type=rtype, resource_id=resource.pk,
                         project_id=resource.project_id, status=status, title=title, body=body,
                         needs_action=needs_action)
            for u in users if actor is None or u.pk != actor.pk]
    Notification.objects.bulk_create(rows)
    return rows


def resolve(rtype, resource, action, actor_label_text):
    """Someone handled the document: clear everyone's open "needs action" notice and say who did."""
    note = f'{text.RESOLVED[action]} oleh {actor_label_text}'
    return (Notification.objects
            .filter(resource_type=rtype, resource_id=resource.pk, needs_action=True)
            .update(needs_action=False, resolved_at=timezone.now(), resolved_by_name=actor_label_text,
                    resolution_note=note))


def on_event(resource, entry):
    """Turn one audit event into notifications. Called for every audit row; most are ignored."""
    rtype = text.TYPES.get(resource._meta.label_lower)
    if rtype is None:
        return
    action, actor = entry.action, entry.user

    if action in text.RESOLVED:
        resolve(rtype, resource, action, actor_name(actor, entry.actor_label))

    def wait(status):
        title, body = text.action_needed(rtype, resource, status)
        _send(holders(rtype, status, resource.project_id), K.ACTION_NEEDED, rtype, resource, title, body,
              actor, status=status, needs_action=True)

    creator = [resource.created_by] if resource.created_by.is_active else []

    if action == 'submitted':
        wait(resource.status)
        if resource.submission_round == 1:
            _send(coordinators(), K.SUBMITTED, rtype, resource, *text.submitted(rtype, resource), actor)
    elif action == 'approved':
        if resource.status == 'approved':
            _send(creator + list(coordinators()), K.APPROVED, rtype, resource,
                  *text.approved(rtype, resource), actor, status='approved')
        else:
            wait(resource.status)
    elif action == 'rejected':
        _send(creator, K.REJECTED, rtype, resource, *text.rejected(rtype, resource, entry), actor, status='rejected')
    elif action == 'close_requested':
        wait(resource.status)
    elif action == 'closed':
        _send(creator, K.CLOSED, rtype, resource, *text.closed(rtype, resource), actor, status='closed')
    elif action == 'cancelled':
        _send(creator, K.CANCELLED, rtype, resource, *text.cancelled(rtype, resource, entry), actor,
              status='cancelled')
