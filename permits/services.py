from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from audit.services import Action, log_event
from hira.models import HIRADocument
from hira.services import InvalidTransition
from projects.models import ProjectMember
from users.models import User
from . import routing
from .forms import FORMS
from .models import Permit, PermitApproval

S = Permit.Status
R = User.Role

EDITABLE_STATUSES = (S.DRAFT, S.REJECTED)
FINAL_STATUSES = (S.CLOSED, S.CANCELLED)
PROJECT_SCOPED = (R.SUPERVISOR, R.PROJECT_MANAGER)


class PAction:
    CLOSE_REQUESTED = 'close_requested'
    CLOSED = 'closed'


def _lock(pk):
    return Permit.objects.select_for_update().get(pk=pk)   # inside transaction.atomic


def _is_member(project_id, user):
    return ProjectMember.objects.filter(project_id=project_id, user=user).exists()


def can_decide(permit, user):
    role = routing.STATUS_ROLE.get(permit.status)
    if role is None or user.role != role:
        return False
    return not (role in PROJECT_SCOPED and not _is_member(permit.project_id, user))


def lock_for_edit(pk, user):
    permit = _lock(pk)
    if permit.created_by_id != user.id:
        raise PermissionDenied('Only the creator can edit this permit.')
    if permit.status not in EDITABLE_STATUSES:
        raise InvalidTransition(f'Permit is locked while its status is "{permit.status}".')
    return permit


def incomplete_fields(permit):
    form = FORMS[permit.permit_type]
    errors = {}
    if not permit.safety_areas:
        errors['safety_areas'] = 'Select at least one safety area.'
    missing = [k for k in form['checklist'] if k not in permit.checklist]
    if missing:
        errors['checklist'] = f'All checklist items must be answered. Missing: {", ".join(missing)}.'
    return errors


@transaction.atomic
def submit(permit, user, request):
    permit = _lock(permit.pk)
    if permit.created_by_id != user.id:
        raise PermissionDenied('Only the creator can submit this permit.')
    if permit.status not in EDITABLE_STATUSES:
        raise InvalidTransition(f'Cannot submit a permit with status "{permit.status}".')

    activity = permit.hira_activity
    hira = activity.group.hira_document
    if hira.status != HIRADocument.Status.APPROVED:
        raise ValidationError({'hira_activity': 'The referenced HIRA is not approved.'})
    if hira.project_id != permit.project_id:
        raise ValidationError({'hira_activity': 'The HIRA belongs to a different project.'})
    if errors := incomplete_fields(permit):
        raise ValidationError(errors)

    resubmission = permit.status == S.REJECTED
    permit.risk_category = activity.initial_category      # locks the chain for this round
    permit.submission_round += 1
    permit.status = routing.first_status(permit.risk_category)
    permit.save(update_fields=['risk_category', 'submission_round', 'status', 'updated_at'])
    log_event(permit, Action.SUBMITTED, request=request,
              remarks=f'risk={permit.risk_category}' + (' (resubmission)' if resubmission else ''))
    return permit


@transaction.atomic
def decide(permit, user, request, decision, remarks=''):
    permit = _lock(permit.pk)
    role = routing.STATUS_ROLE.get(permit.status)
    if role is None:
        raise InvalidTransition(f'No approval is pending (status: "{permit.status}").')
    if user.role != role:
        raise PermissionDenied(f'This step can only be done by: {role}.')
    if role in PROJECT_SCOPED and not _is_member(permit.project_id, user):
        raise PermissionDenied('You are not assigned to this project.')

    remarks = (remarks or '').strip()
    if decision == PermitApproval.Decision.APPROVED:
        if permit.hira_activity.group.hira_document.status != HIRADocument.Status.APPROVED:
            raise InvalidTransition('The referenced HIRA is no longer approved. Reject this permit instead.')
        # Week 4: from M upward, the Officer step also requires the inspection photo here.
        new_status, action = routing.status_after(permit.risk_category, role), Action.APPROVED
    else:
        if not remarks:
            raise ValidationError({'remarks': 'A reason is required when rejecting.'})
        new_status, action = S.REJECTED, Action.REJECTED

    PermitApproval.objects.create(
        permit=permit, step=str(role), decision=decision, decided_by=user,
        remarks=remarks, submission_round=permit.submission_round)
    permit.status = new_status
    permit.save(update_fields=['status', 'updated_at'])
    log_event(permit, action, request=request, remarks=f'[{role}] {remarks}'.strip())
    return permit


@transaction.atomic
def cancel(permit, user, request, reason):
    permit = _lock(permit.pk)
    if permit.status in FINAL_STATUSES:
        raise InvalidTransition(f'Permit is already {permit.status}.')
    permit.status = S.CANCELLED
    permit.cancelled_by, permit.cancelled_at, permit.cancel_reason = user, timezone.now(), reason
    permit.save(update_fields=['status', 'cancelled_by', 'cancelled_at', 'cancel_reason', 'updated_at'])
    log_event(permit, Action.CANCELLED, request=request, remarks=reason)
    return permit


@transaction.atomic
def request_close(permit, user, request, evaluation):
    permit = _lock(permit.pk)
    if permit.created_by_id != user.id:
        raise PermissionDenied('Only the creator can request closure.')
    if permit.status != S.APPROVED:
        raise InvalidTransition('Only an approved permit can be closed.')
    permit.post_work_evaluation = evaluation
    permit.close_requested_at = timezone.now()
    permit.status = S.PENDING_CLOSE
    permit.save(update_fields=['post_work_evaluation', 'close_requested_at', 'status', 'updated_at'])
    log_event(permit, PAction.CLOSE_REQUESTED, request=request)
    return permit


@transaction.atomic
def confirm_close(permit, user, request):
    permit = _lock(permit.pk)
    if permit.status != S.PENDING_CLOSE:
        raise InvalidTransition('No closure request is pending.')
    permit.status, permit.closed_by, permit.closed_at = S.CLOSED, user, timezone.now()
    permit.save(update_fields=['status', 'closed_by', 'closed_at', 'updated_at'])
    log_event(permit, PAction.CLOSED, request=request)
    return permit


@transaction.atomic
def set_number(permit, user, request, number):
    permit = _lock(permit.pk)
    if permit.status == S.CANCELLED:
        raise InvalidTransition('A cancelled permit cannot be changed.')
    old = permit.document_number
    permit.document_number = number
    permit.save(update_fields=['document_number', 'updated_at'])
    log_event(permit, Action.UPDATED, request=request,
              remarks=f'document_number: "{old}" -> "{number}"')
    return permit