from django.test import TestCase
from rest_framework.test import APIClient

from audit.models import AuditLog
from hira.models import HIRAActivity, HIRADocument, HIRAGroup
from projects.models import Project, ProjectMember
from users.models import User

R = User.Role
URL = '/api/permits/'
LS = {'R': (1, 1), 'M': (2, 3), 'T': (3, 4), 'E': (4, 5)}   # (likelihood, severity) -> Rt 1/6/12/20


class PermitWorkflowTests(TestCase):
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

    def make_hira(self, cat, status=HIRADocument.Status.APPROVED, project=None):
        doc = HIRADocument.objects.create(project=project or self.p1, work_package='WP', work_area='WA',
                                          created_by=self.sub, status=status)
        grp = HIRAGroup.objects.create(hira_document=doc, location_function='Lantai 3')
        l, s = LS[cat]
        return HIRAActivity.objects.create(
            group=grp, activity_name='Kerja', activity_type='R', potential_hazard='x', risk_impact='y',
            initial_likelihood=l, initial_severity=s, residual_likelihood=1, residual_severity=1)

    def payload(self, act, **over):
        data = {
            'project': self.p1.id, 'hira_activity': act.id, 'permit_type': 'working_at_height',
            'work_date': '2026-10-20', 'work_start': '08:00', 'work_end': '16:00',
            'work_location': 'Lantai 3', 'work_description': 'Pasang bekisting',
            'equipment_used': 'Tangga', 'person_in_charge': 'Budi', 'worker_count': 4,
            'safety_areas': ['A'], 'checklist': {str(i): True for i in range(1, 15)},
            'ppe': ['body_harness', 'sepatu'], 'ppe_other': [],
        }
        data.update(over)
        return data

    def create(self, cat='R', **over):
        r = self.call(self.sub, 'post', URL, self.payload(self.make_hira(cat), **over))
        self.assertEqual(r.status_code, 201, r.data)
        return r.data['id']

    def submit(self, pid):
        return self.call(self.sub, 'post', f'{URL}{pid}/submit/')

    def act(self, user, pid, name, data=None):
        return self.call(user, 'post', f'{URL}{pid}/{name}/', data)

    # tests
    def test_submit_copies_category_and_audits_with_project(self):
        pid = self.create('M')
        r = self.submit(pid)
        self.assertEqual((r.data['status'], r.data['risk_category']), ('pending_supervisor', 'M'))
        self.assertEqual([c['step'] for c in r.data['approval_chain']], ['supervisor', 'hse_officer'])
        entry = AuditLog.objects.get(resource_type='permits.permit', resource_id=pid, action='submitted')
        self.assertEqual(entry.project, self.p1)

    def test_chain_low_supervisor_only(self):
        pid = self.create('R')
        self.submit(pid)
        self.assertEqual(self.act(self.sup, pid, 'approve').data['status'], 'approved')

    def test_chain_medium(self):
        pid = self.create('M')
        self.submit(pid)
        self.assertEqual(self.act(self.sup, pid, 'approve').data['status'], 'pending_officer')
        self.assertEqual(self.act(self.officer, pid, 'approve').data['status'], 'approved')

    def test_chain_high_and_extreme_stop_at_mk(self):
        for cat in ('T', 'E'):
            pid = self.create(cat)
            self.submit(pid)
            self.assertEqual(self.act(self.sup, pid, 'approve').data['status'], 'pending_officer')
            self.assertEqual(self.act(self.officer, pid, 'approve').data['status'], 'pending_pm')
            r = self.act(self.pm, pid, 'approve')
            self.assertEqual(r.data['status'], 'pending_mk')
            self.assertEqual(r.data['approval_chain'][-1], {'step': 'mk', 'state': 'pending'})
            self.assertEqual(self.act(self.officer, pid, 'approve').status_code, 409)

    def test_wrong_role_or_project(self):
        pid = self.create('T')
        self.submit(pid)
        self.assertEqual(self.act(self.pm, pid, 'approve').status_code, 403)       # PM before Supervisor
        self.assertEqual(self.act(self.officer, pid, 'approve').status_code, 403)
        self.assertEqual(self.act(self.coord, pid, 'approve').status_code, 403)
        self.assertEqual(self.act(self.pm2, pid, 'approve').status_code, 404)      # other project: invisible

    def test_hira_must_be_approved_and_same_project(self):
        draft = self.make_hira('R', status=HIRADocument.Status.DRAFT)
        self.assertEqual(self.call(self.sub, 'post', URL, self.payload(draft)).status_code, 400)
        foreign = self.make_hira('R', project=self.p2)
        self.assertEqual(self.call(self.sub, 'post', URL, self.payload(foreign)).status_code, 400)

    def test_incomplete_content_blocks_submit(self):
        pid = self.create('R', checklist={'1': True}, safety_areas=[])
        r = self.submit(pid)
        self.assertEqual(r.status_code, 400)
        self.assertIn('checklist', r.data)
        self.assertIn('safety_areas', r.data)

    def test_bad_checklist_shape_rejected(self):
        act = self.make_hira('R')
        self.assertEqual(self.call(self.sub, 'post', URL,
                                   self.payload(act, checklist={'99': True})).status_code, 400)
        self.assertEqual(self.call(self.sub, 'post', URL,
                                   self.payload(act, ppe=['jetpack'])).status_code, 400)
        self.assertEqual(self.call(self.sub, 'post', URL,
                                   self.payload(act, permit_type='lifting')).status_code, 400)

    def test_reject_edit_resubmit(self):
        pid = self.create('R')
        self.submit(pid)
        self.assertEqual(self.act(self.sup, pid, 'reject').status_code, 400)        # reason required
        self.assertEqual(self.act(self.sup, pid, 'reject', {'remarks': 'Lengkapi APD'}).data['status'], 'rejected')
        url = f'{URL}{pid}/'
        self.assertEqual(self.call(self.sub, 'patch', url, {'worker_count': 6}).status_code, 200)
        self.assertEqual(self.call(self.sub2, 'patch', url, {'worker_count': 7}).status_code, 404)
        r = self.submit(pid)
        self.assertEqual((r.data['status'], r.data['submission_round']), ('pending_supervisor', 2))
        self.assertEqual(self.act(self.sup, pid, 'approve').data['status'], 'approved')

    def test_locked_while_pending(self):
        pid = self.create('R')
        self.submit(pid)
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{pid}/', {'worker_count': 9}).status_code, 409)

    def test_cancelled_hira_blocks_approval(self):
        pid = self.create('R')
        self.submit(pid)
        HIRADocument.objects.update(status=HIRADocument.Status.CANCELLED)
        self.assertEqual(self.act(self.sup, pid, 'approve').status_code, 409)
        self.assertEqual(self.act(self.sup, pid, 'reject', {'remarks': 'HIRA batal'}).status_code, 200)

    def test_close_flow(self):
        pid = self.create('R')
        self.submit(pid)
        self.assertEqual(self.act(self.sub, pid, 'request-close', {'post_work_evaluation': 'x'}).status_code, 409)
        self.act(self.sup, pid, 'approve')
        self.assertEqual(self.act(self.sub, pid, 'request-close').status_code, 400)  # evaluation required
        r = self.act(self.sub, pid, 'request-close', {'post_work_evaluation': 'Selesai aman'})
        self.assertEqual(r.data['status'], 'pending_close')
        self.assertEqual(self.act(self.sub, pid, 'confirm-close').status_code, 403)
        r = self.act(self.officer, pid, 'confirm-close')
        self.assertEqual(r.data['status'], 'closed')
        self.assertEqual(self.act(self.coord, pid, 'cancel', {'reason': 'x'}).status_code, 409)

    def test_coordinator_cancel(self):
        pid = self.create('M')
        self.submit(pid)
        self.assertEqual(self.act(self.coord, pid, 'cancel').status_code, 400)
        self.assertEqual(self.act(self.coord, pid, 'cancel', {'reason': 'Salah lokasi'}).data['status'], 'cancelled')
        self.assertEqual(self.act(self.sup, pid, 'approve').status_code, 409)

    def test_visibility(self):
        pid = self.create('R')
        detail = f'{URL}{pid}/'
        for u in (self.sub2, self.sup, self.officer, self.pm, self.pm2):   # draft hidden
            self.assertEqual(self.call(u, 'get', detail).status_code, 404, u.email)
        self.assertEqual(self.call(self.coord, 'get', detail).status_code, 200)
        self.submit(pid)
        for u, code in ((self.sup, 200), (self.officer, 200), (self.pm, 200),
                        (self.pm2, 404), (self.sub2, 404), (self.sub, 200)):
            self.assertEqual(self.call(u, 'get', detail).status_code, code, u.email)

    def test_officer_sets_number_and_form_schema(self):
        pid = self.create('R')
        self.submit(pid)
        r = self.act(self.officer, pid, 'set-number', {'document_number': 'F.01/P.10/SHE/KPT-001'})
        self.assertEqual(r.data['document_number'], 'F.01/P.10/SHE/KPT-001')
        self.assertEqual(self.act(self.sub, pid, 'set-number', {'document_number': 'x'}).status_code, 403)
        s = self.call(self.sub, 'get', URL + 'form-schema/')
        self.assertEqual(s.status_code, 200)
        self.assertEqual(len(s.data['checklist']), 14)