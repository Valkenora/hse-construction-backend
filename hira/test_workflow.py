from django.test import TestCase
from rest_framework.test import APIClient

from audit.models import AuditLog
from projects.models import Project, ProjectMember
from users.models import User
from .models import HIRAApproval

R = User.Role
URL = '/api/hira/'


class HIRAWorkflowTests(TestCase):
    def setUp(self):
        mk = lambda n, r: User.objects.create_user(f'{n}@t.com', 'pass12345', full_name=n, role=r)
        self.p1 = Project.objects.create(name='P1', location='L', client_name='C', start_date='2026-10-01')
        self.p2 = Project.objects.create(name='P2', location='L', client_name='C', start_date='2026-10-01')
        self.sub, self.sub2 = mk('sub', R.SUBCONTRACTOR), mk('sub2', R.SUBCONTRACTOR)
        self.sup, self.pm, self.pm2 = mk('sup', R.SUPERVISOR), mk('pm', R.PROJECT_MANAGER), mk('pm2', R.PROJECT_MANAGER)
        self.officer, self.coord = mk('off', R.HSE_OFFICER), mk('coord', R.HSE_COORDINATOR)
        for u in (self.sub, self.sub2, self.sup, self.pm):
            ProjectMember.objects.create(project=self.p1, user=u)
        ProjectMember.objects.create(project=self.p2, user=self.pm2)

    # helpers
    def call(self, user, method, url, data=None):
        c = APIClient()
        c.force_authenticate(user)
        if method == 'get':
            return c.get(url)
        return getattr(c, method)(url, data or {}, format='json')

    def create_hira(self, residual_l=1, residual_s=2):
        r = self.call(self.sub, 'post', URL + 'documents/', {
            'project': self.p1.id, 'work_package': 'Penyimpanan Bekisting', 'work_area': 'Workshop'})
        self.assertEqual(r.status_code, 201, r.data)
        doc = r.data['id']
        g = self.call(self.sub, 'post', URL + 'groups/', {'hira_document': doc, 'location_function': 'Alat Bekisting'})
        self.assertEqual(g.status_code, 201, g.data)
        a = self.call(self.sub, 'post', URL + 'activities/', {
            'group': g.data['id'], 'order': 1, 'activity_name': 'Penerimaan Bekisting', 'activity_type': 'NR',
            'potential_hazard': 'Terlindas truk', 'risk_impact': 'Luka, Cidera',
            'control_category': 'ADM, APD',
            'initial_severity': 4, 'initial_likelihood': 3,
            'residual_severity': residual_s, 'residual_likelihood': residual_l,
            'initial_score': 1,  # client-supplied score must be ignored
        })
        self.assertEqual(a.status_code, 201, a.data)
        return doc, a.data

    def submit(self, doc):
        return self.call(self.sub, 'post', f'{URL}documents/{doc}/submit/')

    def act(self, user, doc, name, data=None):
        return self.call(user, 'post', f'{URL}documents/{doc}/{name}/', data)

    # tests
    def test_scores_computed_server_side_and_audited_with_project(self):
        _, act = self.create_hira()
        self.assertEqual(act['initial_score'], 12)
        self.assertEqual(act['initial_category'], 'T')
        self.assertEqual(act['residual_score'], 2)
        self.assertTrue(act['residual_acceptable'])
        self.assertEqual(act['control_category'], 'ADM, APD')
        entry = AuditLog.objects.get(resource_type='hira.hiraactivity', resource_id=act['id'])
        self.assertEqual(entry.project, self.p1)  # fails if get_audit_project() is missing

    def test_cannot_create_in_foreign_project(self):
        r = self.call(self.sub, 'post', URL + 'documents/',
                      {'project': self.p2.id, 'work_package': 'X', 'work_area': 'X'})
        self.assertEqual(r.status_code, 400)

    def test_cannot_submit_empty_hira(self):
        r = self.call(self.sub, 'post', URL + 'documents/',
                      {'project': self.p1.id, 'work_package': 'X', 'work_area': 'X'})
        self.assertEqual(self.submit(r.data['id']).status_code, 400)

    def test_full_approval_chain_and_audit(self):
        doc, _ = self.create_hira()
        self.assertEqual(self.submit(doc).data['status'], 'pending_officer')
        self.assertEqual(self.act(self.officer, doc, 'approve').data['status'], 'pending_pm')
        self.assertEqual(self.act(self.pm, doc, 'approve').data['status'], 'approved')
        self.assertEqual(HIRAApproval.objects.filter(hira_document_id=doc).count(), 2)
        actions = list(AuditLog.objects.filter(resource_type='hira.hiradocument', resource_id=doc)
                       .order_by('id').values_list('action', flat=True))
        self.assertEqual(actions, ['created', 'submitted', 'approved', 'approved'])

    def test_wrong_role_or_step_is_blocked(self):
        doc, _ = self.create_hira()
        self.submit(doc)
        self.assertEqual(self.act(self.pm, doc, 'approve').status_code, 403)       # PM before Officer
        self.assertEqual(self.act(self.coord, doc, 'approve').status_code, 403)    # Coordinator can't approve
        self.act(self.officer, doc, 'approve')
        self.assertEqual(self.act(self.officer, doc, 'approve').status_code, 403)  # Officer at PM step
        self.assertEqual(self.act(self.pm2, doc, 'approve').status_code, 404)      # PM of another project

    def test_residual_gate_forces_rejection(self):
        doc, _ = self.create_hira(residual_l=2, residual_s=4)  # Rt 8 > 4
        self.submit(doc)
        self.assertEqual(self.act(self.officer, doc, 'approve').status_code, 400)
        self.assertEqual(self.act(self.officer, doc, 'reject').status_code, 400)   # reason required
        r = self.act(self.officer, doc, 'reject', {'remarks': 'Residual terlalu tinggi'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'rejected')

    def test_residual_boundary_at_4(self):
        doc, _ = self.create_hira(residual_l=2, residual_s=2)  # Rt 4: acceptable
        self.submit(doc)
        self.assertEqual(self.act(self.officer, doc, 'approve').status_code, 200)
        doc2, _ = self.create_hira(residual_l=2, residual_s=3)  # Rt 6: now blocked
        self.submit(doc2)
        self.assertEqual(self.act(self.officer, doc2, 'approve').status_code, 400)

    def test_edit_lock_until_rejected_then_resubmit(self):
        doc, act = self.create_hira()
        self.submit(doc)
        url = f'{URL}activities/{act["id"]}/'
        self.assertEqual(self.call(self.sub, 'patch', url, {'activity_name': 'x'}).status_code, 409)
        self.act(self.officer, doc, 'reject', {'remarks': 'Perbaiki'})
        self.assertEqual(self.call(self.sub, 'patch', url, {'activity_name': 'x'}).status_code, 200)
        r = self.submit(doc)
        self.assertEqual((r.data['status'], r.data['submission_round']), ('pending_officer', 2))
        self.assertEqual(self.call(self.sub2, 'patch', url, {'activity_name': 'y'}).status_code, 404)

    def test_visibility(self):
        doc, _ = self.create_hira()
        detail = f'{URL}documents/{doc}/'
        for u in (self.sub2, self.sup, self.officer, self.pm, self.pm2):   # draft: hidden
            self.assertEqual(self.call(u, 'get', detail).status_code, 404, u.email)
        self.assertEqual(self.call(self.coord, 'get', detail).status_code, 200)
        self.submit(doc)
        for u, code in ((self.sup, 200), (self.officer, 200), (self.pm, 200),
                        (self.pm2, 404), (self.sub2, 404), (self.sub, 200)):
            self.assertEqual(self.call(u, 'get', detail).status_code, code, u.email)

    def test_coordinator_cancel(self):
        doc, _ = self.create_hira()
        self.submit(doc)
        self.assertEqual(self.act(self.coord, doc, 'cancel').status_code, 400)  # reason required
        r = self.act(self.coord, doc, 'cancel', {'reason': 'Salah proyek'})
        self.assertEqual(r.data['status'], 'cancelled')
        self.assertEqual(self.submit(doc).status_code, 409)
        self.assertEqual(self.act(self.officer, doc, 'approve').status_code, 409)

    def test_officer_sets_document_number(self):
        doc, _ = self.create_hira()
        self.submit(doc)
        r = self.act(self.officer, doc, 'set-number', {'document_number': 'F.01/P.01/HSE/KPT'})
        self.assertEqual(r.data['document_number'], 'F.01/P.01/HSE/KPT')
        self.assertEqual(self.act(self.sub, doc, 'set-number', {'document_number': 'x'}).status_code, 403)