from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from users.models import User
from .models import AuditLog
from .serializers import AuditLogSerializer


class AuditPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):  # no PUT/PATCH/DELETE routes exist
    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AuditPagination

    def get_queryset(self):
        user = self.request.user
        qs = AuditLog.objects.select_related('project', 'user')

        if user.role in (User.Role.HSE_OFFICER, User.Role.HSE_COORDINATOR):
            pass  # cross-project
        elif user.role in (User.Role.PROJECT_MANAGER, User.Role.SUPERVISOR):
            qs = qs.filter(project__members__user=user)
        else:  # subcontractor: only their own actions, refine when permits exist
            qs = qs.filter(user=user)

        p = self.request.query_params
        if p.get('resource_type'):
            qs = qs.filter(resource_type=p['resource_type'])
        if p.get('resource_id'):
            qs = qs.filter(resource_id=p['resource_id'])
        if p.get('user'):
            qs = qs.filter(user_id=p['user'])
        if p.get('project'):
            qs = qs.filter(project_id=p['project'])
        if p.get('action'):
            qs = qs.filter(action=p['action'])
        if p.get('date_from') and (d := parse_date(p['date_from'])):
            qs = qs.filter(timestamp__date__gte=d)
        if p.get('date_to') and (d := parse_date(p['date_to'])):
            qs = qs.filter(timestamp__date__lte=d)
        return qs.distinct()