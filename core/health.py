from django.http import JsonResponse
from django.views.decorators.http import require_safe


@require_safe        # GET and HEAD, which is what uptime pingers send
def healthz(request):
    """Liveness only: answers without authentication and without touching the database,
    so a pinger every few minutes keeps the instance awake at no cost."""
    return JsonResponse({'status': 'ok'})
