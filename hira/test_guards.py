from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from projects.models import Project, ProjectMember
from users.models import User
from .models import HIRAActivity, HIRADocument, HIRAGroup

R = User.Role
URL = '/api/hira/documents/'
PLAIN_STATIC = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class HIRAProjectGuardTests(TestCase):
    def setUp(self):
        mk = lambda n, r: User.objects.create_user(f'{n}@t.com', 'pass12345', full_name=n, role=r)
        self.p1 = Project.objects.create(name='P1', location='L', client_name='C', start_date='2026-10-01')
        self.sub, self.officer = mk('sub', R.SUBCONTRACTOR), mk('off', R.HSE_OFFICER)
        self.coord = User.objects.create_superuser('coord@t.com', 'pass12345', full_name='coord',
                                                   role=R.HSE_COORDINATOR)
        ProjectMember.objects.create(project=self.p1, user=self.sub)
        self.doc = self.make_doc()
        self.pending = self.make_doc(HIRADocument.Status.PENDING_OFFICER)

    def make_doc(self, status=HIRADocument.Status.DRAFT):
        doc = HIRADocument.objects.create(project=self.p1, work_package='WP', work_area='WA',
                                          created_by=self.sub, status=status)
        group = HIRAGroup.objects.create(hira_document=doc, location_function='G')
        HIRAActivity.objects.create(
            group=group, activity_name='A', activity_type='R', potential_hazard='x', risk_impact='y',
            initial_likelihood=2, initial_severity=2, residual_likelihood=1, residual_severity=1)
        return doc

    def call(self, user, method, url, data=None):
        c = APIClient()
        c.force_authenticate(user)
        return getattr(c, method)(url, data or {}, format='json')

    def test_patch_is_refused_by_the_lock_before_the_body_is_validated(self):
        r = self.call(self.sub, 'patch', f'{URL}{self.pending.pk}/', {'project': 999})
        self.assertEqual(r.status_code, 409)

    def test_inactive_project_is_read_only_except_coordinator_cancel(self):
        group = self.doc.groups.get()
        Project.objects.filter(pk=self.p1.pk).update(is_active=False)
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{self.doc.pk}/', {'work_area': 'X'}).status_code, 409)
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{self.doc.pk}/submit/').status_code, 409)
        self.assertEqual(self.call(self.sub, 'delete', f'{URL}{self.doc.pk}/').status_code, 409)
        self.assertEqual(self.call(self.sub, 'post', '/api/hira/groups/',
                                   {'hira_document': self.doc.pk, 'location_function': 'G2'}).status_code, 409)
        self.assertEqual(self.call(self.sub, 'patch', f'/api/hira/groups/{group.pk}/',
                                   {'location_function': 'Z'}).status_code, 409)
        self.assertEqual(self.call(self.officer, 'post', f'{URL}{self.pending.pk}/approve/').status_code, 409)
        self.assertEqual(self.call(self.officer, 'post', f'{URL}{self.pending.pk}/set-number/',
                                   {'document_number': 'X'}).status_code, 409)
        self.assertEqual(self.call(self.sub, 'get', f'{URL}{self.doc.pk}/').status_code, 200)

        Project.objects.filter(pk=self.p1.pk).update(is_active=True)
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{self.doc.pk}/', {'work_area': 'X'}).status_code, 200)

        Project.objects.filter(pk=self.p1.pk).update(is_active=False)
        self.assertEqual(self.call(self.coord, 'post', f'{URL}{self.doc.pk}/cancel/',
                                   {'reason': 'x'}).status_code, 200)

    def test_a_removed_member_can_no_longer_change_their_hira(self):
        group = self.doc.groups.get()
        ProjectMember.objects.filter(project=self.p1, user=self.sub).delete()
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{self.doc.pk}/', {'work_area': 'X'}).status_code, 403)
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{self.doc.pk}/submit/').status_code, 403)
        self.assertEqual(self.call(self.sub, 'delete', f'{URL}{self.doc.pk}/').status_code, 403)
        self.assertEqual(self.call(self.sub, 'post', '/api/hira/activities/', {
            'group': group.pk, 'activity_name': 'B', 'activity_type': 'R', 'potential_hazard': 'x',
            'risk_impact': 'y', 'initial_severity': 1, 'initial_likelihood': 1,
            'residual_severity': 1, 'residual_likelihood': 1}).status_code, 403)
        self.assertEqual(self.call(self.sub, 'get', f'{URL}{self.doc.pk}/').status_code, 200)

    @override_settings(STORAGES=PLAIN_STATIC)
    def test_admin_page_is_fully_read_only_when_the_project_is_inactive(self):
        Project.objects.filter(pk=self.p1.pk).update(is_active=False)
        self.client.force_login(self.coord)
        page = self.client.get(f'/admin/hira/hiradocument/{self.pending.pk}/change/')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(list(page.context['adminform'].form.fields), [])
