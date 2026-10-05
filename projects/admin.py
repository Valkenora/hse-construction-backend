from django.contrib import admin
from .models import Project, ProjectMember, ProjectMKContact


class ProjectMemberInline(admin.TabularInline):
    model = ProjectMember
    extra = 1
    autocomplete_fields = ['user']


class ProjectMKContactInline(admin.TabularInline):
    model = ProjectMKContact
    extra = 1


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ['name', 'client_name', 'location', 'start_date', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'client_name']
    inlines = [ProjectMemberInline, ProjectMKContactInline]