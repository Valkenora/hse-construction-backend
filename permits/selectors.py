from users.models import User
from .models import Permit

R = User.Role
S = Permit.Status


def visible_permits(user):
    qs = Permit.objects.all()
    if user.role == R.HSE_COORDINATOR:
        return qs
    if user.role == R.HSE_OFFICER:
        return qs.exclude(status=S.DRAFT)
    if user.role in (R.PROJECT_MANAGER, R.SUPERVISOR):
        return qs.filter(project__members__user=user).exclude(status=S.DRAFT)
    return qs.filter(created_by=user)