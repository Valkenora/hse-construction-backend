from django.db import transaction
from django.db.models import Exists, OuterRef
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from audit.services import Action, log_event
from users.models import User
from users.permissions import HasRole
from . import services
from .models import HIRAActivity, HIRAApproval, HIRADocument, HIRAGroup
from .selectors import visible_documents
from .serializers import (
    CancelSerializer, HIRAActivitySerializer, HIRADocumentDetailSerializer,
    HIRADocumentListSerializer, HIRAGroupSerializer, RemarksSerializer, SetNumberSerializer,
)

R = User.Role
WRITE_METHODS = ['get', 'post', 'patch', 'delete', 'head', 'options']  # no PUT


class HIRADocumentViewSet(viewsets.ModelViewSet):
    http_method_names = WRITE_METHODS

    def get_permissions(self):
        roles = {
            'create': (R.SUBCONTRACTOR,), 'partial_update': (R.SUBCONTRACTOR,),
            'destroy': (R.SUBCONTRACTOR,), 'submit': (R.SUBCONTRACTOR,),
            'approve': (R.HSE_OFFICER, R.PROJECT_MANAGER),
            'reject': (R.HSE_OFFICER, R.PROJECT_MANAGER),
            'cancel': (R.HSE_COORDINATOR,),
            'set_number': (R.HSE_OFFICER, R.HSE_COORDINATOR),
        }.get(self.action)
        return [HasRole(*roles)] if roles else [IsAuthenticated()]

    def get_serializer_class(self):
        return HIRADocumentListSerializer if self.action == 'list' else HIRADocumentDetailSerializer

    def get_queryset(self):
        qs = (visible_documents(self.request.user)
              .select_related('project', 'created_by', 'cancelled_by')
              .annotate(has_unacceptable_residual=Exists(
                  HIRAActivity.objects.filter(group__hira_document=OuterRef('pk'),
                                              residual_acceptable=False))))
        if self.action != 'list':
            qs = qs.prefetch_related('groups__activities', 'approvals__decided_by')
        p = self.request.query_params
        if p.get('project'):
            qs = qs.filter(project_id=p['project'])
        if p.get('status'):
            qs = qs.filter(status=p['status'])
        return qs

    def _detail(self):
        return Response(self.get_serializer(self.get_object()).data)

    @transaction.atomic
    def perform_create(self, serializer):
        doc = serializer.save(created_by=self.request.user)
        log_event(doc, Action.CREATED, request=self.request)

    @transaction.atomic
    def perform_update(self, serializer):
        services.lock_for_edit(serializer.instance.pk, self.request.user)
        log_event(serializer.save(), Action.UPDATED, request=self.request)

    @transaction.atomic
    def perform_destroy(self, instance):
        doc = services.lock_for_edit(instance.pk, self.request.user)
        if doc.submission_round > 0:
            raise services.InvalidTransition('A HIRA that has been submitted cannot be deleted.')
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
                        HIRAApproval.Decision.APPROVED, body.validated_data.get('remarks', ''))
        return self._detail()

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        body = RemarksSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.decide(self.get_object(), request.user, request,
                        HIRAApproval.Decision.REJECTED, body.validated_data.get('remarks', ''))
        return self._detail()

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        body = CancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.cancel(self.get_object(), request.user, request, body.validated_data['reason'])
        return self._detail()

    @action(detail=True, methods=['post'], url_path='set-number')
    def set_number(self, request, pk=None):
        body = SetNumberSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.set_number(self.get_object(), request.user, request,
                            body.validated_data['document_number'])
        return self._detail()


class HIRAContentViewSet(viewsets.ModelViewSet):
    """Shared edit-lock + audit logic for groups and activities."""
    http_method_names = WRITE_METHODS

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return [HasRole(R.SUBCONTRACTOR)]

    def doc_id_from_data(self, data):
        raise NotImplementedError

    def doc_id_of(self, instance):
        raise NotImplementedError

    @transaction.atomic
    def perform_create(self, serializer):
        services.lock_for_edit(self.doc_id_from_data(serializer.validated_data), self.request.user)
        log_event(serializer.save(), Action.CREATED, request=self.request)

    @transaction.atomic
    def perform_update(self, serializer):
        services.lock_for_edit(self.doc_id_of(serializer.instance), self.request.user)
        log_event(serializer.save(), Action.UPDATED, request=self.request)

    @transaction.atomic
    def perform_destroy(self, instance):
        services.lock_for_edit(self.doc_id_of(instance), self.request.user)
        log_event(instance, Action.DELETED, request=self.request)
        instance.delete()


class HIRAGroupViewSet(HIRAContentViewSet):
    serializer_class = HIRAGroupSerializer

    def get_queryset(self):
        qs = (HIRAGroup.objects
              .filter(hira_document__in=visible_documents(self.request.user))
              .prefetch_related('activities'))
        if doc := self.request.query_params.get('hira_document'):
            qs = qs.filter(hira_document_id=doc)
        return qs

    def doc_id_from_data(self, data):
        return data['hira_document'].pk

    def doc_id_of(self, instance):
        return instance.hira_document_id


class HIRAActivityViewSet(HIRAContentViewSet):
    serializer_class = HIRAActivitySerializer

    def get_queryset(self):
        qs = HIRAActivity.objects.filter(
            group__hira_document__in=visible_documents(self.request.user))
        p = self.request.query_params
        if p.get('group'):
            qs = qs.filter(group_id=p['group'])
        if p.get('hira_document'):
            qs = qs.filter(group__hira_document_id=p['hira_document'])
        return qs

    def doc_id_from_data(self, data):
        return data['group'].hira_document_id

    def doc_id_of(self, instance):
        return instance.group.hira_document_id