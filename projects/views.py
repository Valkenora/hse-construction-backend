from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from users.models import User
from .models import Project
from .serializers import ProjectSerializer


class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only. Projects, members and MK contacts are managed in /admin."""
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Project.objects.all()
        if user.role not in (User.Role.HSE_OFFICER, User.Role.HSE_COORDINATOR):
            qs = qs.filter(members__user=user)
        active = self.request.query_params.get('is_active')
        if active in ('true', 'false'):
            qs = qs.filter(is_active=(active == 'true'))
        return qs.order_by('name').distinct()