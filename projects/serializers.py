from rest_framework import serializers
from .models import Project


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ['id', 'name', 'location', 'client_name', 'start_date', 'is_active']
        read_only_fields = fields