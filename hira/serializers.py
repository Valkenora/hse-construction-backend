from django.conf import settings
from rest_framework import serializers

from projects.models import ProjectMember
from users.models import User
from .models import HIRAActivity, HIRAApproval, HIRADocument, HIRAGroup
from .services import DECISION_STEPS, EDITABLE_STATUSES


class UserBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'full_name', 'role']


class HIRAActivitySerializer(serializers.ModelSerializer):
    class Meta:
        model = HIRAActivity
        fields = [
            'id', 'group', 'order', 'activity_name', 'activity_type',
            'potential_hazard', 'risk_impact', 'applicable_regulation',
            'existing_controls', 'control_category', 'opportunity',
            'initial_severity', 'initial_likelihood',
            'initial_score', 'initial_category', 'initial_acceptable',
            'residual_severity', 'residual_likelihood',
            'residual_score', 'residual_category', 'residual_acceptable',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'initial_score', 'initial_category', 'initial_acceptable',
            'residual_score', 'residual_category', 'residual_acceptable',
            'created_at', 'updated_at',
        ]

    def validate_group(self, group):
        if self.instance is not None and group != self.instance.group:
            raise serializers.ValidationError('An activity cannot be moved to another group.')
        return group


class HIRAGroupSerializer(serializers.ModelSerializer):
    activities = HIRAActivitySerializer(many=True, read_only=True)

    class Meta:
        model = HIRAGroup
        fields = ['id', 'hira_document', 'location_function', 'order', 'activities']

    def validate_hira_document(self, doc):
        if self.instance is not None and doc != self.instance.hira_document:
            raise serializers.ValidationError('A group cannot be moved to another HIRA.')
        return doc


class HIRAApprovalSerializer(serializers.ModelSerializer):
    decided_by = UserBriefSerializer(read_only=True)

    class Meta:
        model = HIRAApproval
        fields = ['id', 'step', 'decision', 'decided_by', 'remarks',
                  'submission_round', 'decided_at']


class HIRADocumentListSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    created_by = UserBriefSerializer(read_only=True)
    has_unacceptable_residual = serializers.BooleanField(read_only=True)  # annotated in view

    class Meta:
        model = HIRADocument
        fields = [
            'id', 'project', 'project_name', 'work_package', 'work_area',
            'document_number', 'document_date', 'external_procedure_url',
            'status', 'submission_round', 'created_by',
            'has_unacceptable_residual', 'created_at', 'updated_at',
        ]
        read_only_fields = ['document_number', 'status', 'submission_round',
                            'created_at', 'updated_at']

    def validate_project(self, project):
        if self.instance is not None:
            if project != self.instance.project:
                raise serializers.ValidationError('Project cannot be changed after creation.')
            return project
        if not project.is_active:
            raise serializers.ValidationError('This project is not active.')
        user = self.context['request'].user
        if not ProjectMember.objects.filter(project=project, user=user).exists():
            raise serializers.ValidationError('You are not a member of this project.')
        return project


class HIRADocumentDetailSerializer(HIRADocumentListSerializer):
    groups = HIRAGroupSerializer(many=True, read_only=True)
    approvals = HIRAApprovalSerializer(many=True, read_only=True)
    cancelled_by = UserBriefSerializer(read_only=True)
    project_info = serializers.SerializerMethodField()
    form_revision = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    class Meta(HIRADocumentListSerializer.Meta):
        fields = HIRADocumentListSerializer.Meta.fields + [
            'groups', 'approvals', 'cancelled_by', 'cancelled_at', 'cancel_reason',
            'project_info', 'form_revision', 'allowed_actions',
        ]
        read_only_fields = HIRADocumentListSerializer.Meta.read_only_fields + [
            'cancelled_at', 'cancel_reason']

    def get_project_info(self, obj):
        p = obj.project
        return {'name': p.name, 'location': p.location, 'client_name': p.client_name}

    def get_form_revision(self, obj):
        return settings.HIRA_FORM_REVISION

    def get_allowed_actions(self, obj):
        u = self.context['request'].user
        own_editable = u.id == obj.created_by_id and obj.status in EDITABLE_STATUSES
        step = DECISION_STEPS.get(obj.status)
        return {
            'can_edit': own_editable,
            'can_submit': own_editable,
            'can_decide': bool(step and u.role == step[1]),
            'can_cancel': u.role == User.Role.HSE_COORDINATOR and obj.status != HIRADocument.Status.CANCELLED,
            'can_set_number': u.role in (User.Role.HSE_OFFICER, User.Role.HSE_COORDINATOR)
                              and obj.status != HIRADocument.Status.CANCELLED,
        }


# --- action payloads ---
class RemarksSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True)


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField()


class SetNumberSerializer(serializers.Serializer):
    document_number = serializers.CharField(max_length=100)