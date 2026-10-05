from django.conf import settings
from django.db import models


class Project(models.Model):
    name        = models.CharField(max_length=255)
    location    = models.CharField(max_length=255)
    client_name = models.CharField(max_length=255)
    start_date  = models.DateField()
    is_active   = models.BooleanField(default=True)
    created_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='projects_created',
    )
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.name} — {self.client_name}'


class ProjectMember(models.Model):
    """Links project-scoped roles (PM, Supervisor, Subkontraktor) to a project.
    HSE Officer / HSE Coordinator are cross-project and don't need a row."""
    project   = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='members')
    user      = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='project_memberships')
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'user'], name='uniq_member_per_project'),
        ]

    def __str__(self):
        return f'{self.user} @ {self.project}'


class ProjectMKContact(models.Model):
    """External MK person. No login. Used for OTP approval (T/E) and FYI email (R/M)."""
    project    = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='mk_contacts')
    full_name  = models.CharField(max_length=150)
    email      = models.EmailField()
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'email'], name='uniq_mk_email_per_project'),
        ]

    def __str__(self):
        return f'{self.full_name} <{self.email}> ({self.project})'