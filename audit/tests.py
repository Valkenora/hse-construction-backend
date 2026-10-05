from django.test import TestCase
from projects.models import Project
from users.models import User
from .models import AuditLog, ImmutableError
from .services import Action, log_event
from rest_framework.test import APIClient


class AuditLogTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            'sub@test.com', 'pass12345', full_name='Sub One', role=User.Role.SUBCONTRACTOR)
        self.project = Project.objects.create(
            name='P', location='L', client_name='C', start_date='2026-10-01')

    def make(self):
        return log_event(self.project, Action.CREATED, user=self.user, project=self.project)

    def test_log_records_actor_and_snapshot(self):
        entry = self.make()
        self.assertEqual(entry.resource_type, 'projects.project')
        self.assertEqual(entry.snapshot['name'], 'P')
        self.assertIn('sub@test.com', entry.actor_label)

    def test_cannot_modify_or_delete(self):
        entry = self.make()
        entry.remarks = 'tampered'
        with self.assertRaises(ImmutableError):
            entry.save()
        with self.assertRaises(ImmutableError):
            entry.delete()
        with self.assertRaises(ImmutableError):
            AuditLog.objects.all().update(remarks='x')
        with self.assertRaises(ImmutableError):
            AuditLog.objects.all().delete()

    def test_api_is_read_only(self):
        entry = self.make()
        client = APIClient()
        client.force_authenticate(self.user)
        url = f'/api/audit/{entry.pk}/'
        self.assertEqual(client.get(url).status_code, 200)   # readable
        self.assertEqual(client.delete(url).status_code, 405)
        self.assertEqual(client.patch(url, {}, format='json').status_code, 405)
        self.assertEqual(client.put(url, {}, format='json').status_code, 405)