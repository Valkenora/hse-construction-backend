from rest_framework import serializers

from . import labels
from .models import AuditLog


class AuditEntrySerializer(serializers.ModelSerializer):
    """The preview row: readable text only. Used for the list and, for non-HSE roles, the detail."""
    action_label = serializers.SerializerMethodField()
    resource_label = serializers.SerializerMethodField()
    resource_title = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    actor_name = serializers.SerializerMethodField()
    actor_role = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    remarks = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ['id', 'timestamp', 'action', 'action_label', 'resource_type', 'resource_label',
                  'resource_id', 'resource_title', 'project', 'project_name',
                  'actor_name', 'actor_role', 'summary', 'remarks']
        read_only_fields = fields

    def get_action_label(self, obj):
        return labels.action_label(obj.action)

    def get_resource_label(self, obj):
        return labels.resource_label(obj.resource_type)

    def get_resource_title(self, obj):
        return labels.resource_title(obj.resource_type, obj.snapshot, obj.resource_id)

    def get_project_name(self, obj):
        return obj.project.name if obj.project_id else None

    def get_actor_name(self, obj):
        if obj.user_id:
            return obj.user.full_name
        if obj.actor_label.startswith('MK:'):          # "MK: Budi <mk@x> (external, ...)" -> "Budi (MK)"
            return obj.actor_label[3:].split('<')[0].strip() + ' (MK)'
        return 'Sistem'

    def get_actor_role(self, obj):
        if obj.user_id:
            return obj.user.role
        return 'mk' if obj.actor_label.startswith('MK:') else 'system'

    def get_summary(self, obj):
        return labels.summary(obj.action, obj.remarks, obj.snapshot)

    def get_remarks(self, obj):
        return labels.clean_remarks(obj.action, obj.remarks)


class AuditDetailSerializer(AuditEntrySerializer):
    """The full record for the HSE Officer and Coordinator: the preview plus the raw data."""
    remarks = serializers.CharField(read_only=True)             # raw, as written

    class Meta(AuditEntrySerializer.Meta):
        fields = AuditEntrySerializer.Meta.fields + ['user', 'actor_label', 'ip_address',
                                                      'user_agent', 'snapshot']
        read_only_fields = fields
