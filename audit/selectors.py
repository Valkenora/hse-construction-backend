from django.db.models import Q

from hira.models import HIRAActivity, HIRADocument, HIRAGroup
from permits.models import Permit
from users.models import User
from .labels import MK_OTP_ACTIONS
from .models import AuditLog

R = User.Role
MANAGEMENT = (R.HSE_OFFICER, R.HSE_COORDINATOR)     # may open the full record (snapshot, IP, user agent)


def can_see_full_record(user):
    return user.role in MANAGEMENT


def visible_entries(user):
    """Audit rows this user may read. Mirrors who can see the record itself:
    only the Coordinator and the creator see drafts, subcontractors see the history
    of their own permits and HIRA documents, and MK code details stay with the HSE roles."""
    qs = AuditLog.objects.select_related('project', 'user')

    if user.role == R.HSE_COORDINATOR:
        return qs

    if user.role == R.SUBCONTRACTOR:
        mine_permits = Permit.objects.filter(created_by=user).values('id')
        mine_hira = HIRADocument.objects.filter(created_by=user).values('id')
        return qs.filter(
            Q(resource_type='permits.permit', resource_id__in=mine_permits)
            | Q(resource_type='hira.hiradocument', resource_id__in=mine_hira)
        ).exclude(action__in=MK_OTP_ACTIONS)

    # Officer, PM, Supervisor: never someone's draft
    draft = HIRADocument.Status.DRAFT
    qs = qs.exclude(
        Q(resource_type='permits.permit',
          resource_id__in=Permit.objects.filter(status=Permit.Status.DRAFT).values('id'))
        | Q(resource_type='hira.hiradocument',
            resource_id__in=HIRADocument.objects.filter(status=draft).values('id'))
        | Q(resource_type='hira.hiragroup',
            resource_id__in=HIRAGroup.objects.filter(hira_document__status=draft).values('id'))
        | Q(resource_type='hira.hiraactivity',
            resource_id__in=HIRAActivity.objects.filter(group__hira_document__status=draft).values('id'))
    )
    if user.role == R.HSE_OFFICER:
        return qs
    return qs.filter(project__members__user=user).exclude(action__in=MK_OTP_ACTIONS)
