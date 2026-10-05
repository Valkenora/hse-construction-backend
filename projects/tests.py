from django.test import TestCase
from rest_framework.test import APIClient

from users.models import User
from .models import Project, ProjectMember

R = User.Role


class ProjectListTests(TestCase):
    def setUp(self):
        mk = lambda n, r: User.objects.create_user(f'{n}@t.com', 'pass12345', full_name=n, role=r)
        self.p1 = Project.objects.create(name='P1', location='L', client_name='C', start_date='2026-10-01')
        self.p2 = Project.objects.create(name='P2', location='L', client_name='C',
                                         start_date='2026-10-01', is_active=False)
        self.sub, self.pm2 = mk('sub', R.SUBCONTRACTOR), mk('pm2', R.PROJECT_MANAGER)
        self.officer, self.coord = mk('off', R.HSE_OFFICER), mk('coord', R.HSE_COORDINATOR)
        ProjectMember.objects.create(project=self.p1, user=self.sub)
        ProjectMember.objects.create(project=self.p2, user=self.pm2)

    def names(self, user, query=''):
        c = APIClient()
        c.force_authenticate(user)
        r = c.get('/api/projects/' + query)
        self.assertEqual(r.status_code, 200)
        return [p['name'] for p in r.data]

    def test_member_sees_only_own_projects(self):
        self.assertEqual(self.names(self.sub), ['P1'])
        self.assertEqual(self.names(self.pm2), ['P2'])

    def test_hse_roles_see_all(self):
        self.assertEqual(self.names(self.officer), ['P1', 'P2'])
        self.assertEqual(self.names(self.coord), ['P1', 'P2'])

    def test_active_filter(self):
        self.assertEqual(self.names(self.officer, '?is_active=true'), ['P1'])

    def test_read_only_and_auth_required(self):
        c = APIClient()
        self.assertEqual(c.get('/api/projects/').status_code, 401)
        c.force_authenticate(self.sub)
        self.assertEqual(c.post('/api/projects/', {'name': 'X'}, format='json').status_code, 405)