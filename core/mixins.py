from django.db import transaction
from rest_framework.response import Response

from audit.services import Action, log_event


class LockedPartialUpdateMixin:
    """PATCH that takes the row lock BEFORE validating.

    Without this, the serializer validates against a copy read before the lock, and a
    concurrent submit/approve could change the status in between. Order here:
    lock + rule check -> re-read under the lock -> validate -> save + audit, one transaction.
    The viewset must define `lock_for_update(instance)` and exclude PUT from its methods."""

    def lock_for_update(self, instance):
        raise NotImplementedError

    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        self.lock_for_update(self.get_object())
        instance = self.get_object()   # fresh read, now protected by the lock
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        log_event(serializer.save(), Action.UPDATED, request=request)
        return Response(self.get_serializer(self.get_object()).data)
