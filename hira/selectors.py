from users.models import User
from .models import HIRADocument

R = User.Role
S = HIRADocument.Status


def visible_documents(user):
    qs = HIRADocument.objects.all()
    if user.role == R.HSE_COORDINATOR:
        return qs                                   # oversight: everything, incl. drafts
    if user.role == R.HSE_OFFICER:
        return qs.exclude(status=S.DRAFT)           # cross-project, no one's drafts
    if user.role in (R.PROJECT_MANAGER, R.SUPERVISOR):
        return qs.filter(project__members__user=user).exclude(status=S.DRAFT)
    return qs.filter(created_by=user)               # subcontractor: own only