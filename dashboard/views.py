from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.labels import resource_label, resource_title
from . import queues

DEFAULT_LIMIT, MAX_LIMIT = 10, 50


def _limit(request):
    try:
        return max(1, min(int(request.query_params.get('limit', DEFAULT_LIMIT)), MAX_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


class DashboardStatsView(APIView):
    """Counters for the left card. Same shape for every role; each role only counts what it can see."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({
            'role': user.role,
            'hira': queues.status_buckets(queues.HIRA, user),
            'permits': queues.status_buckets(queues.PERMIT, user),
            'pending_action': queues.queue_size(user),
        })


def _task(kind, obj):
    if kind == queues.PERMIT:
        rtype = 'permits.permit'
        title = resource_title(rtype, {'document_number': obj.document_number,
                                       'work_description': obj.work_description}, obj.pk)
    else:
        rtype = 'hira.hiradocument'
        title = resource_title(rtype, {'document_number': obj.document_number,
                                       'work_package': obj.work_package}, obj.pk)
    waiting = queues.WAITING_ON[kind].get(obj.status)
    return {
        'id': obj.pk, 'type': kind, 'title': title, 'category': resource_label(rtype),
        'status': obj.status, 'status_label': obj.get_status_display(),
        'project': obj.project_id, 'project_name': obj.project.name,
        'waiting_on': waiting, 'waiting_on_label': queues.WAITING_ON_LABELS.get(waiting),
        'updated_at': obj.updated_at,
    }


class DashboardTasksView(APIView):
    """The right card: documents that need this user's attention, longest-waiting first.
    A plain array. `?limit=` (default 10, max 50); the full size is `pending_action` in /stats/."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        limit = _limit(request)
        rows = []
        for kind in (queues.HIRA, queues.PERMIT):
            qs = queues.queue(kind, request.user).select_related('project').order_by('updated_at', 'id')[:limit]
            rows += [(obj.updated_at, kind, obj.pk, _task(kind, obj)) for obj in qs]
        rows.sort(key=lambda r: r[:3])
        return Response([r[3] for r in rows[:limit]])
