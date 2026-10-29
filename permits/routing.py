"""Approval chain by initial risk category. Single source of truth for routing."""
from users.models import User
from .models import Permit

R = User.Role
S = Permit.Status

CHAINS = {
    'R': (R.SUPERVISOR,),
    'M': (R.SUPERVISOR, R.HSE_OFFICER),
    'T': (R.SUPERVISOR, R.HSE_OFFICER, R.PROJECT_MANAGER),
    'E': (R.SUPERVISOR, R.HSE_OFFICER, R.PROJECT_MANAGER),
}
MK_CATEGORIES = ('T', 'E')   # MK approval mandatory; R/M only get an FYI email (Phase 2)

STATUS_ROLE = {                      # who may act in each pending status
    S.PENDING_SUPERVISOR: R.SUPERVISOR,
    S.PENDING_OFFICER:    R.HSE_OFFICER,
    S.PENDING_PM:         R.PROJECT_MANAGER,
}
STEP_STATUS = {**{role: status for status, role in STATUS_ROLE.items()}, 'mk': S.PENDING_MK}


def first_status(category):
    return STEP_STATUS[CHAINS[category][0]]


def status_after(category, role):
    chain = CHAINS[category]
    i = chain.index(role)
    if i + 1 < len(chain):
        return STEP_STATUS[chain[i + 1]]
    return S.PENDING_MK if category in MK_CATEGORIES else S.APPROVED


def steps_for(category):
    return [str(r) for r in CHAINS[category]] + (['mk'] if category in MK_CATEGORIES else [])


CATEGORY_ORDER = 'RMTE'      # lowest to highest. Not alphabetical, so never compare the letters directly.


def highest(categories):
    """The riskiest of several initial categories: the one that decides the approval chain."""
    return max(categories, key=CATEGORY_ORDER.index)
