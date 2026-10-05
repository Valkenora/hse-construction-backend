from rest_framework import serializers
from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = ['id', 'resource_type', 'resource_id', 'action', 'project', 'user',
                  'actor_label', 'ip_address', 'user_agent', 'timestamp', 'remarks', 'snapshot']
        read_only_fields = fields