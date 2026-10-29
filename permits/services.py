import logging
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from core.storage import StorageError, get_storage

from audit.services import Action, log_event
from hira.models import HIRADocument
from hira.services import InvalidTransition
from projects.guards import ensure_active, ensure_creator_can_work
from projects.models import ProjectMember
from users.models import User
from . import evidence, mk, routing
from .forms import FORMS
from .models import Permit, PermitApproval, PermitAttachment, PermitGeoStamp, PermitPhoto

S = Permit.Status
R = User.Role

EDITABLE_STATUSES = (S.DRAFT, S.REJECTED)
FINAL_STATUSES = (S.CLOSED, S.CANCELLED)
PROJECT_SCOPED = (R.SUPERVISOR, R.PROJECT_MANAGER)
# Roles that stand on site must send their location when approving. PM and MK need not.
GEO_STEPS = {R.SUPERVISOR: PermitGeoStamp.Step.SUPERVISOR, R.HSE_OFFICER: PermitGeoStamp.Step.OFFICER}
Stage = PermitPhoto.Stage
Kind = PermitAttachment.Kind
MAX_HIRA_ACTIVITIES = 10

log = logging.getLogger(__name__)


class PAction:
    CLOSE_REQUESTED = 'close_requested'
    CLOSED = 'closed'
    PHOTO_ADDED = 'photo_added'
    PHOTO_REMOVED = 'photo_removed'
    ATTACHMENT_ADDED = 'attachment_added'
    ATTACHMENT_REMOVED = 'attachment_removed'


def _lock(pk):
    return Permit.objects.select_for_update().get(pk=pk)   # inside transaction.atomic


def _is_member(project_id, user):
    return ProjectMember.objects.filter(project_id=project_id, user=user).exists()


def record_location(permit, step, user, location):
    """Store a geo stamp for this round and return a line for the audit remarks."""
    PermitGeoStamp.objects.create(permit=permit, step=step, submission_round=permit.submission_round,
                                  recorded_by=user, **location)
    return (f'location={location["latitude"]:.6f},{location["longitude"]:.6f} '
            f'accuracy={location["accuracy"]:.0f}m captured_at={location["captured_at"].isoformat()}')


def discard_objects(paths):
    """Best-effort removal of files from storage. A leftover file is harmless (private,
    unreferenced); failing the request over it would not be."""
    try:
        get_storage().delete(list(paths))
    except StorageError:
        log.warning('Could not remove %d evidence file(s) from storage', len(paths))


def signed_urls(paths):
    """{path: url}. If storage is down the permit still loads, just without picture links."""
    try:
        return get_storage().signed_urls(list(paths))
    except StorageError:
        log.warning('Could not sign %d evidence URL(s)', len(paths))
        return {}


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
    ensure_creator_can_work(permit.project_id, user)
    return permit


def validate_hira_selection(project_id, owner_id, document, activities):
    """The rules for what a permit may link to. Returns {field: message}, empty if fine.
    One document (approved, same project, made by the permit's creator) and up to
    MAX_HIRA_ACTIVITIES of its activities, from any of its groups."""
    errors = {}
    if document is None:
        if activities:
            errors['hira_document'] = 'Choose the HIRA document first.'
        return errors
    if document.status != HIRADocument.Status.APPROVED:
        errors['hira_document'] = 'The referenced HIRA is not approved.'
    elif document.project_id != project_id:
        errors['hira_document'] = 'The HIRA belongs to a different project.'
    elif document.created_by_id != owner_id:
        errors['hira_document'] = 'You can only use a HIRA that you created.'
    if len(activities) > MAX_HIRA_ACTIVITIES:
        errors['hira_activity_ids'] = f'At most {MAX_HIRA_ACTIVITIES} activities can be linked to one permit.'
    elif any(a.group.hira_document_id != document.id for a in activities):
        errors['hira_activity_ids'] = 'All activities must belong to the selected HIRA document.'
    return errors


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
def submit(permit, user, request, location):
    permit = _lock(permit.pk)
    if permit.created_by_id != user.id:
        raise PermissionDenied('Only the creator can submit this permit.')
    if permit.status not in EDITABLE_STATUSES:
        raise InvalidTransition(f'Cannot submit a permit with status "{permit.status}".')
    ensure_creator_can_work(permit.project_id, user)

    document = permit.hira_document
    activities = list(permit.hira_activities.select_related('group'))
    errors = validate_hira_selection(permit.project_id, permit.created_by_id, document, activities)
    if document is None or not activities:
        errors.setdefault('hira_activity_ids', 'Link at least one HIRA activity before submitting.')
    errors.update(incomplete_fields(permit))
    if not permit.photos.filter(stage=Stage.BEFORE).exists():
        errors['photos'] = 'Upload at least one "before" photo before submitting.'
    have = set(permit.attachments.values_list('kind', flat=True))
    missing = [k for k in Kind.values if k not in have]
    if missing:
        errors['attachments'] = ('Attach at least one PDF of each kind. Missing: '
                                 + ', '.join(missing) + '.')
    category = routing.highest(a.initial_category for a in activities) if activities else None
    if category in routing.MK_CATEGORIES and not mk.active_contacts(permit.project_id).exists():
        errors['mk'] = ('This permit needs MK approval, but the project has no active MK contact. '
                        'Ask HSE to add one to the project.')
    if errors:
        raise ValidationError(errors)

    resubmission = permit.status == S.REJECTED
    permit.risk_category = category      # the highest linked activity decides the chain, for this round
    permit.submission_round += 1
    permit.status = routing.first_status(permit.risk_category)
    permit.save(update_fields=['risk_category', 'submission_round', 'status', 'updated_at'])
    where = record_location(permit, PermitGeoStamp.Step.CREATOR, user, location)
    log_event(permit, Action.SUBMITTED, request=request,
              remarks=f'risk={permit.risk_category}' + (' (resubmission)' if resubmission else '') + f' {where}')
    return permit


@transaction.atomic
def decide(permit, user, request, decision, remarks='', location=None):
    permit = _lock(permit.pk)
    role = routing.STATUS_ROLE.get(permit.status)
    if role is None:
        raise InvalidTransition(f'No approval is pending (status: "{permit.status}").')
    if user.role != role:
        raise PermissionDenied(f'This step can only be done by: {role}.')
    if role in PROJECT_SCOPED and not _is_member(permit.project_id, user):
        raise PermissionDenied('You are not assigned to this project.')
    ensure_active(permit.project_id)

    remarks = (remarks or '').strip()
    where = ''
    if decision == PermitApproval.Decision.APPROVED:
        if role in GEO_STEPS and location is None:
            raise ValidationError({'location': 'Your current location is required to approve: '
                                               'latitude, longitude, accuracy and captured_at.'})
        if permit.hira_document.status != HIRADocument.Status.APPROVED:
            raise InvalidTransition('The referenced HIRA is no longer approved. Reject this permit instead.')
        new_status, action = routing.status_after(permit.risk_category, role), Action.APPROVED
        if new_status == S.PENDING_MK:
            mk.require_contacts(permit.project_id)      # fail now, not after the decision is saved
    else:
        if not remarks:
            raise ValidationError({'remarks': 'A reason is required when rejecting.'})
        new_status, action = S.REJECTED, Action.REJECTED

    PermitApproval.objects.create(
        permit=permit, step=str(role), decision=decision, decided_by=user,
        remarks=remarks, submission_round=permit.submission_round)
    if decision == PermitApproval.Decision.APPROVED and role in GEO_STEPS:
        where = record_location(permit, GEO_STEPS[role], user, location)
    permit.status = new_status
    permit.save(update_fields=['status', 'updated_at'])
    log_event(permit, action, request=request, remarks=f'[{role}] {remarks} {where}'.strip())
    if new_status == S.PENDING_MK:
        mk.issue_links(permit, request)     # emails go out after this transaction commits
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
    ensure_creator_can_work(permit.project_id, user)
    if not permit.photos.filter(stage=Stage.AFTER).exists():
        raise ValidationError({'photos': 'Upload at least one "after" photo before requesting closure.'})
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
    ensure_active(permit.project_id)
    permit.status, permit.closed_by, permit.closed_at = S.CLOSED, user, timezone.now()
    permit.save(update_fields=['status', 'closed_by', 'closed_at', 'updated_at'])
    log_event(permit, PAction.CLOSED, request=request)
    return permit


def _require_stage_open(permit, stage):
    if stage == Stage.BEFORE and permit.status not in EDITABLE_STATUSES:
        raise InvalidTransition('"Before" photos can only be changed while the permit is a draft or rejected.')
    if stage == Stage.AFTER and permit.status != S.APPROVED:
        raise InvalidTransition('"After" photos can only be changed while the permit is approved, '
                                'before closure is requested.')


@transaction.atomic
def add_photos(permit, user, request, stage, files):
    permit = _lock(permit.pk)
    if permit.created_by_id != user.id:
        raise PermissionDenied('Only the creator can add photos.')
    _require_stage_open(permit, stage)
    ensure_creator_can_work(permit.project_id, user)
    if not files:
        raise ValidationError({'photos': 'No file received. Send the images in the "photos" field.'})
    have = permit.photos.filter(stage=stage).count()
    if have + len(files) > evidence.MAX_PHOTOS_PER_STAGE:
        raise ValidationError({'photos': f'At most {evidence.MAX_PHOTOS_PER_STAGE} photos per stage '
                                         f'({have} already uploaded).'})
    checked = [evidence.inspect(f) for f in files]     # every file is checked before any is uploaded

    storage, uploaded = get_storage(), []
    try:
        for data, content_type, ext, name in checked:
            path = f'permits/{permit.pk}/{stage}/{uuid.uuid4().hex}.{ext}'
            storage.upload(path, data, content_type)
            uploaded.append(path)
            PermitPhoto.objects.create(permit=permit, stage=stage, storage_path=path, original_name=name,
                                       content_type=content_type, size=len(data), uploaded_by=user)
        log_event(permit, PAction.PHOTO_ADDED, request=request, remarks=f'{stage}: {len(uploaded)} photo(s)')
    except Exception:
        discard_objects(uploaded)       # the transaction rolls the rows back; remove the files too
        raise
    return permit


@transaction.atomic
def delete_photo(permit, user, request, photo_id):
    permit = _lock(permit.pk)
    if permit.created_by_id != user.id:
        raise PermissionDenied('Only the creator can remove photos.')
    photo = permit.photos.filter(pk=photo_id).first()
    if photo is None:
        raise NotFound('Photo not found.')
    _require_stage_open(permit, photo.stage)
    ensure_creator_can_work(permit.project_id, user)
    path = photo.storage_path
    photo.delete()
    log_event(permit, PAction.PHOTO_REMOVED, request=request, remarks=f'{photo.stage}: {path}')
    transaction.on_commit(lambda: discard_objects([path]))
    return permit


def _require_editable_attachments(permit):
    if permit.status not in EDITABLE_STATUSES:
        raise InvalidTransition('Attachments can only be changed while the permit is a draft or rejected.')


@transaction.atomic
def add_attachments(permit, user, request, kind, files):
    permit = _lock(permit.pk)
    if permit.created_by_id != user.id:
        raise PermissionDenied('Only the creator can add attachments.')
    _require_editable_attachments(permit)
    ensure_creator_can_work(permit.project_id, user)
    if not files:
        raise ValidationError({'files': 'No file received. Send the PDFs in the "files" field.'})
    have = permit.attachments.filter(kind=kind).count()
    if have + len(files) > evidence.MAX_ATTACHMENTS_PER_KIND:
        raise ValidationError({'files': f'At most {evidence.MAX_ATTACHMENTS_PER_KIND} files of this kind '
                                        f'({have} already uploaded).'})
    checked = [evidence.inspect_pdf(f) for f in files]     # every file is checked before any is uploaded

    storage, uploaded = get_storage(), []
    try:
        for data, name in checked:
            path = f'permits/{permit.pk}/attachments/{kind}/{uuid.uuid4().hex}.pdf'
            storage.upload(path, data, 'application/pdf')
            uploaded.append(path)
            PermitAttachment.objects.create(permit=permit, kind=kind, storage_path=path,
                                            original_name=name, size=len(data), uploaded_by=user)
        log_event(permit, PAction.ATTACHMENT_ADDED, request=request, remarks=f'{kind}: {len(uploaded)} file(s)')
    except Exception:
        discard_objects(uploaded)       # the transaction rolls the rows back; remove the files too
        raise
    return permit


@transaction.atomic
def delete_attachment(permit, user, request, attachment_id):
    permit = _lock(permit.pk)
    if permit.created_by_id != user.id:
        raise PermissionDenied('Only the creator can remove attachments.')
    attachment = permit.attachments.filter(pk=attachment_id).first()
    if attachment is None:
        raise NotFound('Attachment not found.')
    _require_editable_attachments(permit)
    ensure_creator_can_work(permit.project_id, user)
    path = attachment.storage_path
    attachment.delete()
    log_event(permit, PAction.ATTACHMENT_REMOVED, request=request, remarks=f'{attachment.kind}: {path}')
    transaction.on_commit(lambda: discard_objects([path]))
    return permit


@transaction.atomic
def set_number(permit, user, request, number):
    permit = _lock(permit.pk)
    if permit.status == S.CANCELLED:
        raise InvalidTransition('A cancelled permit cannot be changed.')
    ensure_active(permit.project_id)
    old = permit.document_number
    permit.document_number = number
    permit.save(update_fields=['document_number', 'updated_at'])
    log_event(permit, Action.UPDATED, request=request,
              remarks=f'document_number: "{old}" -> "{number}"')
    return permit


def cancel_for_hira(hira, user, request, reason):
    """Cancel every permit that rests on this HIRA and is not already closed or cancelled.
    Called from hira.services.cancel inside its transaction. Returns how many were cancelled."""
    permits = (Permit.objects.filter(hira_document=hira)
               .exclude(status__in=FINAL_STATUSES).order_by('pk'))
    for permit in permits:
        cancel(permit, user, request, f'HIRA #{hira.pk} was cancelled: {reason}')
    return len(permits)
