from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from audit.services import Action, log_event
from projects.models import ProjectMember
from users.models import User
from .models import HIRAActivity, HIRAApproval, HIRADocument
from .risk import ACCEPTABLE_MAX_RT

S = HIRADocument.Status
R = User.Role

EDITABLE_STATUSES = (S.DRAFT, S.REJECTED)

# status -> (step recorded, role allowed to act, status if approved)
DECISION_STEPS = {
    S.PENDING_OFFICER: (HIRAApproval.Step.OFFICER, R.HSE_OFFICER, S.PENDING_PM),
    S.PENDING_PM:      (HIRAApproval.Step.PM,      R.PROJECT_MANAGER, S.APPROVED),
}


class InvalidTransition(APIException):
    status_code = 409
    default_detail = 'This action is not allowed in the current status.'
    default_code = 'invalid_transition'


def _lock(doc_id):
    # Must run inside transaction.atomic
    return HIRADocument.objects.select_for_update().get(pk=doc_id)


def lock_for_edit(doc_id, user):
    """Lock the HIRA row and check that this user may change its content right now."""
    doc = _lock(doc_id)
    if doc.created_by_id != user.id:
        raise PermissionDenied('Only the creator can edit this HIRA.')
    if doc.status not in EDITABLE_STATUSES:
        raise InvalidTransition(f'HIRA is locked while its status is "{doc.status}".')
    return doc


@transaction.atomic
def submit(doc, user, request):
    doc = _lock(doc.pk)
    if doc.created_by_id != user.id:
        raise PermissionDenied('Only the creator can submit this HIRA.')
    if doc.status not in EDITABLE_STATUSES:
        raise InvalidTransition(f'Cannot submit a HIRA with status "{doc.status}".')
    if not HIRAActivity.objects.filter(group__hira_document=doc).exists():
        raise ValidationError({'detail': 'Add at least one activity before submitting.'})

    resubmission = doc.status == S.REJECTED
    doc.submission_round += 1
    doc.status = S.PENDING_OFFICER
    doc.save(update_fields=['status', 'submission_round', 'updated_at'])
    log_event(doc, Action.SUBMITTED, request=request,
              remarks='Resubmission after rejection' if resubmission else '')
    return doc


@transaction.atomic
def decide(doc, user, request, decision, remarks=''):
    doc = _lock(doc.pk)
    if doc.status not in DECISION_STEPS:
        raise InvalidTransition(f'No approval is pending (status: "{doc.status}").')
    step, role, next_status = DECISION_STEPS[doc.status]
    if user.role != role:
        raise PermissionDenied(f'This step can only be done by: {role}.')
    if role == R.PROJECT_MANAGER and not ProjectMember.objects.filter(
            project_id=doc.project_id, user=user).exists():
        raise PermissionDenied('You are not assigned to this project.')

    remarks = (remarks or '').strip()
    if decision == HIRAApproval.Decision.APPROVED:
        bad = HIRAActivity.objects.filter(group__hira_document=doc, residual_acceptable=False)
        if bad.exists():
            names = ', '.join(f'{a.activity_name} (Rt {a.residual_score})' for a in bad[:5])
            raise ValidationError({'detail': (
                f'Cannot approve: residual risk is above the acceptable limit '
                f'(Rt > {ACCEPTABLE_MAX_RT}) on: {names}. Reject the HIRA instead.')})
        new_status, action = next_status, Action.APPROVED
    else:
        if not remarks:
            raise ValidationError({'remarks': 'A reason is required when rejecting.'})
        new_status, action = S.REJECTED, Action.REJECTED

    HIRAApproval.objects.create(
        hira_document=doc, step=step, decision=decision, decided_by=user,
        remarks=remarks, submission_round=doc.submission_round)
    doc.status = new_status
    doc.save(update_fields=['status', 'updated_at'])
    log_event(doc, action, request=request, remarks=f'[{step}] {remarks}'.strip())
    return doc


@transaction.atomic
def cancel(doc, user, request, reason):
    doc = _lock(doc.pk)
    if doc.status == S.CANCELLED:
        raise InvalidTransition('HIRA is already cancelled.')
    doc.status = S.CANCELLED
    doc.cancelled_by = user
    doc.cancelled_at = timezone.now()
    doc.cancel_reason = reason
    doc.save(update_fields=['status', 'cancelled_by', 'cancelled_at', 'cancel_reason', 'updated_at'])
    log_event(doc, Action.CANCELLED, request=request, remarks=reason)
    return doc


@transaction.atomic
def set_number(doc, user, request, number):
    doc = _lock(doc.pk)
    if doc.status == S.CANCELLED:
        raise InvalidTransition('A cancelled HIRA cannot be changed.')
    old = doc.document_number
    doc.document_number = number
    doc.save(update_fields=['document_number', 'updated_at'])
    log_event(doc, Action.UPDATED, request=request,
              remarks=f'document_number: "{old}" -> "{number}"')
    return doc