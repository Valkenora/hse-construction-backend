from django.db import transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from audit.services import Action, log_event
from core.mixins import LockedPartialUpdateMixin
from hira.serializers import CancelSerializer, SetNumberSerializer
from users.models import User
from users.permissions import HasRole
from . import mk, services
from .forms import ENABLED_TYPES, FORMS, WAH
from .models import Permit, PermitApproval, PermitAttachment, PermitPhoto
from .selectors import visible_permits
from .serializers import (CloseRequestSerializer, DecisionSerializer, PermitDetailSerializer,
                          PermitListSerializer, SubmitSerializer)

R = User.Role
WRITE_METHODS = ['get', 'post', 'patch', 'delete', 'head', 'options']  # no PUT


class PermitViewSet(LockedPartialUpdateMixin, viewsets.ModelViewSet):
    http_method_names = WRITE_METHODS

    def get_permissions(self):
        roles = {
            'create': (R.SUBCONTRACTOR,), 'partial_update': (R.SUBCONTRACTOR,),
            'destroy': (R.SUBCONTRACTOR,), 'submit': (R.SUBCONTRACTOR,),
            'request_close': (R.SUBCONTRACTOR,), 'photos': (R.SUBCONTRACTOR,),
            'delete_photo': (R.SUBCONTRACTOR,),
            'attachments': (R.SUBCONTRACTOR,), 'delete_attachment': (R.SUBCONTRACTOR,),
            'approve': (R.SUPERVISOR, R.HSE_OFFICER, R.PROJECT_MANAGER),
            'reject': (R.SUPERVISOR, R.HSE_OFFICER, R.PROJECT_MANAGER),
            'confirm_close': (R.HSE_OFFICER,),
            'cancel': (R.HSE_COORDINATOR,),
            'set_number': (R.HSE_OFFICER, R.HSE_COORDINATOR),
            'mk_resend': (R.HSE_OFFICER, R.HSE_COORDINATOR),
        }.get(self.action)
        return [HasRole(*roles)] if roles else [IsAuthenticated()]

    def get_serializer_class(self):
        return PermitListSerializer if self.action == 'list' else PermitDetailSerializer

    def get_queryset(self):
        qs = visible_permits(self.request.user).select_related(
            'project', 'created_by', 'cancelled_by', 'closed_by', 'hira_document')
        if self.action != 'list':
            qs = qs.prefetch_related('approvals__decided_by', 'photos__uploaded_by',
                                     'geo_stamps__recorded_by', 'mk_requests__contact',
                                     'hira_activities__group', 'attachments__uploaded_by')
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

    def lock_for_update(self, instance):
        services.lock_for_edit(instance.pk, self.request.user)

    @transaction.atomic
    def perform_destroy(self, instance):
        permit = services.lock_for_edit(instance.pk, self.request.user)
        if permit.submission_round > 0:
            raise services.InvalidTransition('A permit that has been submitted cannot be deleted.')
        paths = (list(instance.photos.values_list('storage_path', flat=True))
                 + list(instance.attachments.values_list('storage_path', flat=True)))
        log_event(instance, Action.DELETED, request=self.request)
        instance.delete()
        if paths:      # the rows are gone; remove the files too, once the delete has committed
            transaction.on_commit(lambda: services.discard_objects(paths))

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        body = SubmitSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.submit(self.get_object(), request.user, request, body.validated_data['location'])
        return self._detail()

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        body = DecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.decide(self.get_object(), request.user, request, PermitApproval.Decision.APPROVED,
                        body.validated_data.get('remarks', ''), body.validated_data['location'])
        return self._detail()

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        body = DecisionSerializer(data=request.data)
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

    @action(detail=True, methods=['post'], parser_classes=[MultiPartParser])
    def photos(self, request, pk=None):
        """multipart/form-data: `stage` ("before" | "after") and one or more `photos` files."""
        stage = request.data.get('stage')
        if stage not in PermitPhoto.Stage.values:
            raise ValidationError({'stage': 'Must be "before" or "after".'})
        services.add_photos(self.get_object(), request.user, request, stage,
                            request.FILES.getlist('photos'))
        return Response(self.get_serializer(self.get_object()).data, status=201)

    @action(detail=True, methods=['delete'], url_path=r'photos/(?P<photo_id>\d+)')
    def delete_photo(self, request, pk=None, photo_id=None):
        services.delete_photo(self.get_object(), request.user, request, int(photo_id))
        return self._detail()

    @action(detail=True, methods=['post'], url_path='mk-resend')
    def mk_resend(self, request, pk=None):
        """Issue fresh MK links (old ones stop working) while the permit waits for MK."""
        mk.resend_links(self.get_object(), request)
        return self._detail()

    @action(detail=True, methods=['post'], parser_classes=[MultiPartParser])
    def attachments(self, request, pk=None):
        """multipart/form-data: `kind` ("medical" | "certificate") and one or more PDFs in `files`."""
        kind = request.data.get('kind')
        if kind not in PermitAttachment.Kind.values:
            raise ValidationError({'kind': 'Must be "medical" or "certificate".'})
        services.add_attachments(self.get_object(), request.user, request, kind,
                                 request.FILES.getlist('files'))
        return Response(self.get_serializer(self.get_object()).data, status=201)

    @action(detail=True, methods=['delete'], url_path=r'attachments/(?P<attachment_id>\d+)')
    def delete_attachment(self, request, pk=None, attachment_id=None):
        services.delete_attachment(self.get_object(), request.user, request, int(attachment_id))
        return self._detail()
