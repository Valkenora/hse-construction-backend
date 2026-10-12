from django.db import transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from audit.services import Action, log_event
from hira.serializers import CancelSerializer, RemarksSerializer, SetNumberSerializer
from users.models import User
from users.permissions import HasRole
from . import services
from .forms import ENABLED_TYPES, FORMS, WAH
from .models import Permit, PermitApproval
from .selectors import visible_permits
from .serializers import CloseRequestSerializer, PermitDetailSerializer, PermitListSerializer

R = User.Role
WRITE_METHODS = ['get', 'post', 'patch', 'delete', 'head', 'options']  # no PUT


class PermitViewSet(viewsets.ModelViewSet):
    http_method_names = WRITE_METHODS

    def get_permissions(self):
        roles = {
            'create': (R.SUBCONTRACTOR,), 'partial_update': (R.SUBCONTRACTOR,),
            'destroy': (R.SUBCONTRACTOR,), 'submit': (R.SUBCONTRACTOR,),
            'request_close': (R.SUBCONTRACTOR,),
            'approve': (R.SUPERVISOR, R.HSE_OFFICER, R.PROJECT_MANAGER),
            'reject': (R.SUPERVISOR, R.HSE_OFFICER, R.PROJECT_MANAGER),
            'confirm_close': (R.HSE_OFFICER,),
            'cancel': (R.HSE_COORDINATOR,),
            'set_number': (R.HSE_OFFICER, R.HSE_COORDINATOR),
        }.get(self.action)
        return [HasRole(*roles)] if roles else [IsAuthenticated()]

    def get_serializer_class(self):
        return PermitListSerializer if self.action == 'list' else PermitDetailSerializer

    def get_queryset(self):
        qs = visible_permits(self.request.user).select_related(
            'project', 'created_by', 'cancelled_by', 'closed_by', 'hira_activity__group__hira_document')
        if self.action != 'list':
            qs = qs.prefetch_related('approvals__decided_by')
        p = self.request.query_params
        for key in ('project', 'status', 'risk_category', 'permit_type'):
            if p.get(key):
                qs = qs.filter(**{f'{key}_id' if key == 'project' else key: p[key]})
        return qs

    def _detail(self):
        return Response(self.get_serializer(self.get_object()).data)

    @action(detail=False, methods=['get'], url_path='form-schema')
    def form_schema(self, request):
        ptype = request.query_params.get('type', WAH)
        if ptype not in ENABLED_TYPES:
            raise ValidationError({'type': 'This permit type is not available yet.'})
        return Response({'type': ptype, **FORMS[ptype]})

    @transaction.atomic
    def perform_create(self, serializer):
        permit = serializer.save(created_by=self.request.user)
        log_event(permit, Action.CREATED, request=self.request)

    @transaction.atomic
    def perform_update(self, serializer):
        services.lock_for_edit(serializer.instance.pk, self.request.user)
        log_event(serializer.save(), Action.UPDATED, request=self.request)

    @transaction.atomic
    def perform_destroy(self, instance):
        permit = services.lock_for_edit(instance.pk, self.request.user)
        if permit.submission_round > 0:
            raise services.InvalidTransition('A permit that has been submitted cannot be deleted.')
        log_event(instance, Action.DELETED, request=self.request)
        instance.delete()

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        services.submit(self.get_object(), request.user, request)
        return self._detail()

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        body = RemarksSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.decide(self.get_object(), request.user, request,
                        PermitApproval.Decision.APPROVED, body.validated_data.get('remarks', ''))
        return self._detail()

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        body = RemarksSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.decide(self.get_object(), request.user, request,
                        PermitApproval.Decision.REJECTED, body.validated_data.get('remarks', ''))
        return self._detail()

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        body = CancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.cancel(self.get_object(), request.user, request, body.validated_data['reason'])
        return self._detail()

    @action(detail=True, methods=['post'], url_path='request-close')
    def request_close(self, request, pk=None):
        body = CloseRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.request_close(self.get_object(), request.user, request,
                               body.validated_data['post_work_evaluation'])
        return self._detail()

    @action(detail=True, methods=['post'], url_path='confirm-close')
    def confirm_close(self, request, pk=None):
        services.confirm_close(self.get_object(), request.user, request)
        return self._detail()

    @action(detail=True, methods=['post'], url_path='set-number')
    def set_number(self, request, pk=None):
        body = SetNumberSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.set_number(self.get_object(), request.user, request,
                            body.validated_data['document_number'])
        return self._detail()