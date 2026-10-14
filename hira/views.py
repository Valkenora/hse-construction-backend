from django.db import transaction
from django.db.models import Exists, OuterRef
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from audit.services import Action, log_event
from core.mixins import LockedPartialUpdateMixin
from users.models import User
from users.permissions import HasRole
from . import services
from .models import HIRAActivity, HIRAApproval, HIRADocument, HIRAGroup
from .selectors import visible_documents
from .serializers import (
    CancelSerializer, HIRAActivitySerializer, HIRADocumentCreateSerializer,
    HIRADocumentDetailSerializer, HIRADocumentListSerializer, HIRAGroupSerializer,
    RemarksSerializer, SetNumberSerializer,
)

R = User.Role
WRITE_METHODS = ['get', 'post', 'patch', 'delete', 'head', 'options']  # no PUT


class HIRADocumentViewSet(LockedPartialUpdateMixin, viewsets.ModelViewSet):
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
        if self.action == 'list':
            return HIRADocumentListSerializer
        if self.action == 'create':
            return HIRADocumentCreateSerializer
        return HIRADocumentDetailSerializer

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

    def create(self, request, *args, **kwargs):
        """Header plus optional groups -> activities, saved in one transaction."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        doc = serializer.save()   # HIRADocumentCreateSerializer -> services.create_document
        # Re-read through the queryset so the response carries the same annotations and
        # prefetches as every other detail response.
        detail = HIRADocumentDetailSerializer(
            self.get_queryset().get(pk=doc.pk), context=self.get_serializer_context())
        return Response(detail.data, status=status.HTTP_201_CREATED)

    def lock_for_update(self, instance):
        services.lock_for_edit(instance.pk, self.request.user)

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


class HIRAContentViewSet(LockedPartialUpdateMixin, viewsets.ModelViewSet):
    """Shared edit-lock + audit logic for groups and activities."""
    http_method_names = WRITE_METHODS

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return [HasRole(R.SUBCONTRACTOR)]

    def create_content(self, data):
        """Lock the document, then create through the shared services."""
        raise NotImplementedError

    def doc_id_of(self, instance):
        raise NotImplementedError

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.instance = self.create_content(dict(serializer.validated_data))

    def lock_for_update(self, instance):
        services.lock_for_edit(self.doc_id_of(instance), self.request.user)

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

    def create_content(self, data):
        doc = services.lock_for_edit(data.pop('hira_document').pk, self.request.user)
        return services.add_group(doc, data, self.request)

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

    def create_content(self, data):
        group = data.pop('group')
        services.lock_for_edit(group.hira_document_id, self.request.user)
        return services.add_activity(group, data, self.request)

    def doc_id_of(self, instance):
        return instance.group.hira_document_id