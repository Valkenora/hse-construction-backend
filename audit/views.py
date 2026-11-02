from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from .models import AuditLog
from .selectors import can_see_full_record, visible_entries
from .serializers import AuditDetailSerializer, AuditEntrySerializer


class AuditPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):  # no PUT/PATCH/DELETE routes exist
    permission_classes = [IsAuthenticated]
    pagination_class = AuditPagination

    def get_serializer_class(self):
        # The list is always the light preview. Only the HSE Officer and Coordinator open the full record.
        if self.action == 'retrieve' and can_see_full_record(self.request.user):
            return AuditDetailSerializer
        return AuditEntrySerializer

    def get_queryset(self):
        qs = visible_entries(self.request.user)

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