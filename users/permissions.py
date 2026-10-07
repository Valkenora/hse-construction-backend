from rest_framework.permissions import BasePermission


class HasRole(BasePermission):
    """HasRole(User.Role.HSE_OFFICER, User.Role.PROJECT_MANAGER)"""

    def __init__(self, *roles):
        self.roles = roles

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role in self.roles)