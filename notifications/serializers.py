from rest_framework import serializers

from . import text
from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source='resource_type', read_only=True)
    category = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ['id', 'kind', 'title', 'body', 'type', 'resource_id', 'category', 'project', 'project_name',
                  'status', 'created_at', 'read_at', 'needs_action', 'resolved_at', 'resolved_by_name',
                  'resolution_note']
        read_only_fields = fields

    def get_category(self, obj):
        return text.category(obj.resource_type)

    def get_project_name(self, obj):
        return obj.project.name if obj.project_id else None
