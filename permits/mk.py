"""MK (Manajemen Konstruksi) approval for T/E permits.

MK has no account. After the Project Manager approves, every active MK contact of the project
gets an emailed link. The link opens a public page; to decide, the contact asks for a one-time
code (sent to the same email), enters it, and approves or rejects.

What is stored: only hashes of the link token and of the OTP. What is limited: codes per link,
wrong tries per code, the cooldown between codes, and the link's lifetime. Every step is
audited with the contact as the actor.

Lock order is always permit first, then the MK request, so these functions and the staff-side
services in permits.services can never deadlock each other.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape
from rest_framework.exceptions import APIException, NotFound, ValidationError

from audit.services import Action, log_event
from core.mail import EmailDeliveryError
from hira.models import HIRADocument
from hira.services import InvalidTransition
from projects.guards import ensure_active
from projects.models import ProjectMKContact
from .models import MKApprovalRequest, Permit, PermitApproval

log = logging.getLogger(__name__)
S = Permit.Status
Req = MKApprovalRequest.Status


class Gone(APIException):
    status_code = 410
    default_detail = 'This link can no longer be used.'
    default_code = 'gone'


class TooManyRequests(APIException):
    status_code = 429
    default_detail = 'Too many requests. Please wait.'
    default_code = 'throttled'


class EmailUnavailable(APIException):
    status_code = 503
    default_detail = 'The email could not be sent right now. Please try again in a moment.'
    default_code = 'email_unavailable'


class MKAction:
    LINK_ISSUED = 'mk_link_issued'
    OTP_SENT = 'mk_otp_sent'
    OTP_FAILED = 'mk_otp_failed'


# ---------------------------------------------------------------- helpers
def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def hash_otp(request_id, otp):
    """Keyed hash: a six-digit code would be trivial to brute-force from a plain hash."""
    return hmac.new(settings.SECRET_KEY.encode(), f'{request_id}:{otp}'.encode(), hashlib.sha256).hexdigest()


def actor_label(contact, name=''):
    who = f'{name} ' if name else ''
    return f'MK: {who}<{contact.email}> (external, link + OTP)'


def mask_email(email):
    local, _, domain = email.partition('@')
    return f'{local[:1]}***@{domain}'


def active_contacts(project_id):
    return ProjectMKContact.objects.filter(project_id=project_id, is_active=True).order_by('pk')


def require_contacts(project_id):
    contacts = list(active_contacts(project_id))
    if not contacts:
        raise ValidationError({'mk': 'This project has no active MK contact. Add one to the project '
                                     '(admin → Project → MK contacts) before this permit can reach MK.'})
    return contacts


def effective_status(req, now=None):
    """The stored status, except that an unused link past its expiry reads as expired."""
    if req.status == Req.ACTIVE and req.expires_at <= (now or timezone.now()):
        return 'expired'
    return req.status


def _lock_permit(pk):
    return Permit.objects.select_for_update().get(pk=pk)


# ---------------------------------------------------------------- issuing links (staff side)
def issue_links(permit, request):
    """Create one link per active MK contact and email them once the transaction commits.
    Call inside the transaction that moved the permit to pending_mk (permit already locked)."""
    contacts = require_contacts(permit.project_id)
    MKApprovalRequest.objects.filter(permit=permit, status=Req.ACTIVE).update(status=Req.SUPERSEDED)
    expires = timezone.now() + settings.MK_LINK_TTL
    issued = []
    for contact in contacts:
        token = secrets.token_urlsafe(32)
        req = MKApprovalRequest.objects.create(
            permit=permit, contact=contact, submission_round=permit.submission_round,
            token_hash=hash_token(token), expires_at=expires)
        issued.append((req.pk, token))
    log_event(permit, MKAction.LINK_ISSUED, request=request,
              remarks=f'round {permit.submission_round}: ' + ', '.join(c.email for c in contacts))
    transaction.on_commit(lambda: [deliver_link(pk, token) for pk, token in issued])
    return len(issued)


@transaction.atomic
def resend_links(permit, request):
    """HSE staff: issue fresh links (the old ones stop working)."""
    permit = _lock_permit(permit.pk)
    if permit.status != S.PENDING_MK:
        raise InvalidTransition('Links can only be re-sent while the permit waits for MK.')
    ensure_active(permit.project_id)
    issue_links(permit, request)
    return permit


def deliver_link(req_id, token):
    """Send the link email. Runs after commit; a failure is recorded on the request (staff see it
    and can re-send), never raised into the PM's approval."""
    req = MKApprovalRequest.objects.select_related('permit__project', 'permit__created_by', 'contact').get(pk=req_id)
    try:
        if not settings.FRONTEND_BASE_URL:
            raise EmailDeliveryError('FRONTEND_BASE_URL is not set on the server.')
        _send(req.contact.email, *link_email(req, f'{settings.FRONTEND_BASE_URL}/mk/{token}'))
    except Exception as exc:    # provider down, bad config, anything: record, don't break approval
        log.warning('MK link email for request #%s failed: %s', req_id, exc)
        MKApprovalRequest.objects.filter(pk=req_id).update(email_error=str(exc)[:255])
    else:
        MKApprovalRequest.objects.filter(pk=req_id).update(email_sent_at=timezone.now(), email_error='')


def _send(to, subject, text, html):
    message = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [to])
    message.attach_alternative(html, 'text/html')
    message.send(fail_silently=False)


def link_email(req, url):
    permit, project = req.permit, req.permit.project
    when = timezone.localtime(req.expires_at).strftime('%d %b %Y %H:%M WIB')
    rows = [('Proyek', project.name), ('Jenis', permit.get_permit_type_display()),
            ('Pekerjaan', permit.work_description), ('Lokasi', permit.work_location),
            ('Tanggal kerja', permit.work_date.strftime('%d %b %Y')),
            ('Pemohon', permit.created_by.full_name)]
    text = (f'Yth. {req.contact.full_name},\n\n'
            'Izin Kerja berikut menunggu persetujuan Manajemen Konstruksi:\n\n'
            + '\n'.join(f'  {k}: {v}' for k, v in rows)
            + f'\n\nBuka halaman ini untuk melihat detail dan memberi keputusan:\n{url}\n\n'
              'Untuk menyetujui atau menolak, Anda akan meminta kode OTP yang dikirim ke email ini.\n'
              f'Tautan berlaku sampai {when}. Jangan teruskan tautan ini kepada orang lain.\n')
    html = (f'<p>Yth. {escape(req.contact.full_name)},</p>'
            '<p>Izin Kerja berikut menunggu persetujuan Manajemen Konstruksi:</p><ul>'
            + ''.join(f'<li><b>{escape(k)}:</b> {escape(v)}</li>' for k, v in rows)
            + f'</ul><p><a href="{escape(url)}">Buka halaman persetujuan</a></p>'
              '<p>Untuk menyetujui atau menolak, Anda akan meminta kode OTP yang dikirim ke email ini.<br>'
              f'Tautan berlaku sampai {escape(when)}. Jangan teruskan tautan ini kepada orang lain.</p>')
    return f'[K3] Izin Kerja menunggu persetujuan MK: {project.name}', text, html


def otp_email(req, otp):
    minutes = int(settings.MK_OTP_TTL.total_seconds() // 60)
    text = (f'Kode verifikasi persetujuan Izin Kerja: {otp}\n\n'
            f'Berlaku {minutes} menit. Jangan berikan kode ini kepada siapa pun.\n'
            'Jika Anda tidak memintanya, abaikan email ini.\n')
    html = (f'<p>Kode verifikasi persetujuan Izin Kerja:</p><p style="font-size:24px"><b>{otp}</b></p>'
            f'<p>Berlaku {minutes} menit. Jangan berikan kode ini kepada siapa pun.<br>'
            'Jika Anda tidak memintanya, abaikan email ini.</p>')
    return f'Kode verifikasi: {otp}', text, html


# ---------------------------------------------------------------- the public side
def lookup(token):
    req = (MKApprovalRequest.objects.select_related('permit', 'contact')
           .filter(token_hash=hash_token(token or '')).first())
    if req is None:
        raise NotFound('This link is not valid.')
    return req


def ensure_usable(req, permit):
    if req.status == Req.DECIDED:
        raise Gone('A decision has already been recorded through this link.')
    if req.status == Req.SUPERSEDED:
        raise Gone('This link was replaced or another reviewer already decided. '
                   'Use the newest link, or ask the HSE team to send a new one.')
    if permit.status != S.PENDING_MK or permit.submission_round != req.submission_round:
        raise Gone('This permit is no longer waiting for MK approval.')
    if req.expires_at <= timezone.now():
        raise Gone('This link has expired. Ask the HSE team to send a new one.')


def page_state(req, permit):
    """What the public page should show: active | decided | superseded | closed | expired."""
    if req.status == Req.DECIDED:
        return 'decided'
    if req.status == Req.SUPERSEDED:
        return 'superseded'
    if permit.status != S.PENDING_MK or permit.submission_round != req.submission_round:
        return 'closed'
    return 'expired' if req.expires_at <= timezone.now() else 'active'


def _lock_both(token):
    """permit first, then the request row. Returns (request, permit) both locked and fresh."""
    found = lookup(token)
    permit = _lock_permit(found.permit_id)
    req = MKApprovalRequest.objects.select_for_update().get(pk=found.pk)    # lock this row only
    return req, permit


def otp_state(req, now=None):
    now = now or timezone.now()
    live = bool(req.otp_hash) and req.otp_expires_at and req.otp_expires_at > now
    wait = 0
    if req.otp_sent_at:
        wait = max(0, int(settings.MK_OTP_COOLDOWN_SECONDS - (now - req.otp_sent_at).total_seconds()))
    return {
        'sent': bool(live),
        'expires_at': req.otp_expires_at if live else None,
        'attempts_left': max(0, settings.MK_OTP_MAX_ATTEMPTS - req.otp_attempts) if live else None,
        'can_request_in_seconds': wait,
        'requests_left': max(0, settings.MK_OTP_MAX_SENDS - req.otp_sent_count),
    }


@transaction.atomic
def send_otp(token, request):
    req, permit = _lock_both(token)
    ensure_usable(req, permit)
    ensure_active(permit.project_id)
    now = timezone.now()
    state = otp_state(req, now)
    if state['can_request_in_seconds']:
        raise TooManyRequests(f'Please wait {state["can_request_in_seconds"]} seconds before asking for another code.')
    if req.otp_sent_count >= settings.MK_OTP_MAX_SENDS:
        raise TooManyRequests('Too many codes were requested for this link. '
                              'Ask the HSE team to send a new link.')
    otp = f'{secrets.randbelow(10 ** 6):06d}'
    req.otp_hash = hash_otp(req.pk, otp)
    req.otp_expires_at = now + settings.MK_OTP_TTL
    req.otp_sent_at = now
    req.otp_sent_count += 1
    req.otp_attempts = 0
    req.save(update_fields=['otp_hash', 'otp_expires_at', 'otp_sent_at', 'otp_sent_count', 'otp_attempts'])
    try:
        # Sent before commit on purpose: if the email fails, nothing is consumed and the
        # reviewer is told so, instead of waiting for a code that never comes.
        _send(req.contact.email, *otp_email(req, otp))
    except Exception as exc:
        log.warning('MK OTP email for request #%s failed: %s', req.pk, exc)
        raise EmailUnavailable() from exc
    log_event(permit, MKAction.OTP_SENT, request=request, actor_label=actor_label(req.contact),
              remarks=f'code {req.otp_sent_count} of {settings.MK_OTP_MAX_SENDS}')
    return otp_state(req, now)


def _check_otp(req, permit, otp, request):
    """None if the code is right (and now spent); otherwise the message for the reviewer.
    A wrong try is counted and audited, so the caller must commit before raising."""
    now = timezone.now()
    if not req.otp_hash:
        return 'Request a code first.'
    if req.otp_expires_at <= now:
        return 'The code has expired. Request a new one.'
    if req.otp_attempts >= settings.MK_OTP_MAX_ATTEMPTS:
        return 'Too many wrong attempts for this code. Request a new one.'
    if hmac.compare_digest(req.otp_hash, hash_otp(req.pk, otp)):
        req.otp_hash, req.otp_expires_at = '', None
        req.save(update_fields=['otp_hash', 'otp_expires_at'])
        return None
    req.otp_attempts += 1
    req.save(update_fields=['otp_attempts'])
    log_event(permit, MKAction.OTP_FAILED, request=request, actor_label=actor_label(req.contact),
              remarks=f'wrong code, attempt {req.otp_attempts} of {settings.MK_OTP_MAX_ATTEMPTS}')
    left = settings.MK_OTP_MAX_ATTEMPTS - req.otp_attempts
    return f'The code is not correct. {left} attempt(s) left.' if left else \
        'The code is not correct. Request a new one.'


def decide(token, request, decision, otp, full_name, remarks=''):
    """MK approves or rejects. All or nothing, except that a wrong code is still counted."""
    problem = None
    with transaction.atomic():
        req, permit = _lock_both(token)
        ensure_usable(req, permit)
        ensure_active(permit.project_id)
        problem = _check_otp(req, permit, otp, request)
        if problem is None:
            if decision == PermitApproval.Decision.APPROVED:
                if permit.hira_document.status != HIRADocument.Status.APPROVED:
                    raise InvalidTransition('The referenced HIRA is no longer approved. Reject this permit instead.')
                new_status, action = S.APPROVED, Action.APPROVED
            else:
                new_status, action = S.REJECTED, Action.REJECTED
            PermitApproval.objects.create(
                permit=permit, step=PermitApproval.Step.MK, decision=decision, decided_by=None,
                mk_contact=req.contact, external_name=full_name, external_email=req.contact.email,
                remarks=remarks, submission_round=permit.submission_round)
            permit.status = new_status
            permit.save(update_fields=['status', 'updated_at'])
            req.status = Req.DECIDED
            req.save(update_fields=['status'])
            MKApprovalRequest.objects.filter(permit=permit, status=Req.ACTIVE).update(status=Req.SUPERSEDED)
            log_event(permit, action, request=request, actor_label=actor_label(req.contact, full_name),
                      remarks=f'[mk] {remarks}'.strip())
            return permit
    raise ValidationError({'otp': problem})
