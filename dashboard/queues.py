"""What each role's dashboard counts and lists. One place defines the rules, so the tiles and the
task list can never disagree: "Pending Aksi" is exactly the size of the task queue."""
from django.db.models import Count

from hira.models import HIRADocument
from hira.selectors import visible_documents
from permits.models import Permit
from permits.selectors import visible_permits
from users.models import User

R = User.Role
H = HIRADocument.Status
P = Permit.Status

HIRA, PERMIT = 'hira', 'permit'

# Who a status is waiting on. Statuses that wait on the creator map to the subcontractor.
WAITING_ON = {
    HIRA: {H.DRAFT: 'subcontractor', H.REJECTED: 'subcontractor',
           H.PENDING_OFFICER: 'hse_officer', H.PENDING_PM: 'project_manager'},
    PERMIT: {P.DRAFT: 'subcontractor', P.REJECTED: 'subcontractor', P.APPROVED: 'subcontractor',
             P.PENDING_SUPERVISOR: 'supervisor', P.PENDING_OFFICER: 'hse_officer',
             P.PENDING_PM: 'project_manager', P.PENDING_MK: 'mk', P.PENDING_CLOSE: 'hse_officer'},
}
WAITING_ON_LABELS = {'subcontractor': 'Subkontraktor', 'supervisor': 'Supervisor', 'hse_officer': 'HSE Officer',
                     'project_manager': 'Project Manager', 'mk': 'Manajemen Konstruksi'}

REVIEW = {                       # everything that is in someone's hands right now
    HIRA: [H.PENDING_OFFICER, H.PENDING_PM],
    PERMIT: [P.PENDING_SUPERVISOR, P.PENDING_OFFICER, P.PENDING_PM, P.PENDING_MK, P.PENDING_CLOSE],
}
QUEUE_STATUSES = {               # role -> {type: statuses that need that role's attention}
    R.SUBCONTRACTOR: {HIRA: [H.REJECTED, H.DRAFT], PERMIT: [P.REJECTED, P.APPROVED, P.DRAFT]},
    R.SUPERVISOR: {HIRA: [], PERMIT: [P.PENDING_SUPERVISOR]},
    R.HSE_OFFICER: {HIRA: [H.PENDING_OFFICER], PERMIT: [P.PENDING_OFFICER, P.PENDING_CLOSE]},
    R.PROJECT_MANAGER: {HIRA: [H.PENDING_PM], PERMIT: [P.PENDING_PM]},
    R.HSE_COORDINATOR: REVIEW,   # oversight: the system-wide list of what is waiting on others
}


def visible(kind, user):
    return visible_documents(user) if kind == HIRA else visible_permits(user)


def queue(kind, user):
    """The documents of this type that need this user's attention (or, for the Coordinator, anyone's)."""
    statuses = QUEUE_STATUSES.get(user.role, {}).get(kind, [])
    return visible(kind, user).filter(status__in=statuses).distinct() if statuses else visible(kind, user).none()


def queue_size(user):
    return queue(HIRA, user).count() + queue(PERMIT, user).count()


def status_buckets(kind, user):
    """Counts for one document type: total (cancelled excluded), draft, pending, approved, rejected.
    For permits `approved` includes closed ones, and `pending` includes permits awaiting closure."""
    counts = dict(visible(kind, user).exclude(status='cancelled').order_by()
                  .values_list('status').annotate(n=Count('id', distinct=True)))
    n = lambda *names: sum(counts.get(s, 0) for s in names)
    pending = [str(s) for s in REVIEW[kind]]
    done = ['approved', 'closed'] if kind == PERMIT else ['approved']
    return {'total': sum(counts.values()), 'draft': n('draft'), 'pending': n(*pending),
            'approved': n(*done), 'rejected': n('rejected')}
