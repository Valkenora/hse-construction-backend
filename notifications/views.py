from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification
from .serializers import NotificationSerializer


class NotificationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def mine(request):
    return Notification.objects.filter(recipient=request.user).select_related('project')


def badge(qs):
    """The red number: unread and not already handled by someone else."""
    return qs.filter(read_at__isnull=True, resolved_at__isnull=True)


class NotificationListView(APIView):
    """The bell dropdown. Newest first, paginated. `?unread=true` keeps only unread rows."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = mine(request)
        if request.query_params.get('unread', '').lower() in ('1', 'true', 'yes'):
            qs = qs.filter(read_at__isnull=True)
        paginator = NotificationPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(NotificationSerializer(page, many=True).data)


class UnreadCountView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'unread_count': badge(mine(request)).count()})


class MarkReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        n = get_object_or_404(mine(request), pk=pk)
        if n.read_at is None:
            n.read_at = timezone.now()
            n.save(update_fields=['read_at'])
        return Response(NotificationSerializer(n).data)


class MarkAllReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        updated = mine(request).filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({'updated': updated})
