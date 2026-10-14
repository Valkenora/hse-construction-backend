"""Project-level rules shared by HIRA and permits.

A deactivated project is read-only: nothing under it can be edited, submitted, decided
or closed (reactivating the project brings everything back as it was). The one action
that stays open is a Coordinator cancelling, so clean-up is still possible. A user who
was removed from a project can no longer change their own documents in it.
"""
from rest_framework.exceptions import APIException, PermissionDenied

from .models import Project, ProjectMember


class ProjectInactive(APIException):
    status_code = 409
    default_detail = 'This project is not active. Its HIRAs and permits are read-only.'
    default_code = 'project_inactive'


def ensure_active(project_id):
    if not Project.objects.filter(pk=project_id, is_active=True).exists():
        raise ProjectInactive()


def ensure_creator_can_work(project_id, user):
    """For the author of a document: the project must be active and they must still belong to it."""
    ensure_active(project_id)
    if not ProjectMember.objects.filter(project_id=project_id, user=user).exists():
        raise PermissionDenied('You are no longer a member of this project.')
