from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from audit.models import AuditLog
from projects.models import Project, ProjectMember
from users.models import User
from . import services
from .models import HIRAActivity, HIRAApproval, HIRADocument, HIRAGroup

R = User.Role
URL = '/api/hira/documents/'


def act(name, order=1, **over):
    data = {
        'order': order, 'activity_name': name, 'activity_type': 'NR',
        'potential_hazard': 'Terlindas truk', 'risk_impact': 'Luka, cidera',
        'initial_severity': 4, 'initial_likelihood': 2,      # Rt 8  -> M, not acceptable
        'residual_severity': 2, 'residual_likelihood': 1,    # Rt 2  -> R, acceptable
    }
    data.update(over)
    return data


class NestedCreateTests(TestCase):
    def setUp(self):
        mk = lambda n, r: User.objects.create_user(f'{n}@t.com', 'pass12345', full_name=n, role=r)
        self.p1 = Project.objects.create(name='P1', location='L', client_name='C', start_date='2026-10-01')
        self.p2 = Project.objects.create(name='P2', location='L', client_name='C', start_date='2026-10-01')
        self.sub, self.officer = mk('sub', R.SUBCONTRACTOR), mk('off', R.HSE_OFFICER)
        ProjectMember.objects.create(project=self.p1, user=self.sub)

    # helpers
    def payload(self, **over):
        data = {
            'project': self.p1.id, 'work_package': 'Penyimpanan Bekisting', 'work_area': 'Workshop',
            'groups': [
                {'location_function': 'Alat Bekisting', 'order': 1,
                 'activities': [act('Penerimaan'), act('Penyusunan', 2)]},
                {'location_function': 'Gudang', 'order': 2,
                 'activities': [act('Penyimpanan')]},
            ],
        }
        data.update(over)
        return data

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def post(self, data, user=None):
        return self.client_for(user or self.sub).post(URL, data, format='json')

    def counts(self):
        """(documents, groups, activities, audit entries)"""
        return (HIRADocument.objects.count(), HIRAGroup.objects.count(),
                HIRAActivity.objects.count(), AuditLog.objects.count())

    # tests
    def test_one_request_creates_everything(self):
        r = self.post(self.payload())
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.counts()[:3], (1, 2, 3))
        self.assertEqual((r.data['status'], r.data['created_by']['id']), ('draft', self.sub.id))
        self.assertEqual([len(g['activities']) for g in r.data['groups']], [2, 1])
        first = r.data['groups'][0]['activities'][0]
        self.assertIsNotNone(first['id'])   # the response carries ids
        self.assertEqual((first['initial_score'], first['initial_category'], first['initial_acceptable']),
                         (8, 'M', False))
        self.assertEqual((first['residual_score'], first['residual_category'], first['residual_acceptable']),
                         (2, 'R', True))
        self.assertIs(r.data['has_unacceptable_residual'], False)
        self.assertTrue(r.data['allowed_actions']['can_submit'])

    def test_scores_come_from_the_server_only(self):
        sneaky = act('X', initial_score=1, initial_category='R',
                     initial_acceptable=True, residual_acceptable=False)
        r = self.post(self.payload(groups=[{'location_function': 'G', 'activities': [sneaky]}]))
        got = r.data['groups'][0]['activities'][0]
        self.assertEqual((got['initial_score'], got['initial_category'], got['initial_acceptable']),
                         (8, 'M', False))
        self.assertTrue(got['residual_acceptable'])

    def test_every_row_has_exactly_one_created_audit_entry_with_its_project(self):
        self.post(self.payload())
        self.assertEqual(self.counts(), (1, 2, 3, 6))   # 1 document + 2 groups + 3 activities
        for label, model in (('hira.hiradocument', HIRADocument), ('hira.hiragroup', HIRAGroup),
                             ('hira.hiraactivity', HIRAActivity)):
            for pk in model.objects.values_list('pk', flat=True):
                entries = AuditLog.objects.filter(resource_type=label, resource_id=pk, action='created')
                self.assertEqual(entries.count(), 1, f'{label} #{pk}')
                entry = entries.get()
                self.assertEqual((entry.project, entry.user), (self.p1, self.sub), f'{label} #{pk}')

    def test_header_only_create_still_works(self):
        r = self.post({'project': self.p1.id, 'work_package': 'WP', 'work_area': 'WA'})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['groups'], [])
        self.assertEqual(self.counts(), (1, 0, 0, 1))

    def test_one_invalid_row_rejects_everything_and_saves_nothing(self):
        bad = self.payload()
        bad['groups'][0]['activities'][1]['initial_severity'] = 6
        r = self.post(bad)
        self.assertEqual(r.status_code, 400)
        self.assertIn('initial_severity', r.data['groups'][0]['activities'][1])
        # DRF keys nested-list errors by row index and lists only the invalid rows
        self.assertEqual(list(r.data['groups']), [0])               # only group 0 has errors
        self.assertEqual(list(r.data['groups'][0]['activities']), [1])   # only its row 1
        self.assertEqual(self.counts(), (0, 0, 0, 0))

        missing = self.payload()
        del missing['groups'][1]['activities'][0]['activity_type']
        self.assertEqual(self.post(missing).status_code, 400)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_failure_midway_rolls_back_rows_and_audit(self):
        real, calls = services.add_activity, []

        def flaky(group, data, request):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError('connection lost')
            return real(group, data, request)

        data = {'project': self.p1, 'work_package': 'WP', 'work_area': 'WA', 'groups': [
            {'location_function': 'A', 'activities': [act('1'), act('2')]},
            {'location_function': 'B', 'activities': [act('3')]},
        ]}
        with mock.patch.object(services, 'add_activity', flaky):
            with self.assertRaises(RuntimeError):
                services.create_document(self.sub, None, data)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.counts(), (0, 0, 0, 0))   # nothing left, not even audit rows

    def test_patch_cannot_carry_groups(self):
        doc = self.post(self.payload()).data['id']
        c = self.client_for(self.sub)
        r = c.patch(f'{URL}{doc}/', {'groups': []}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('groups', r.data)
        self.assertEqual(HIRAGroup.objects.count(), 2)   # untouched
        ok = c.patch(f'{URL}{doc}/', {'work_area': 'Lantai 2'}, format='json')
        self.assertEqual((ok.status_code, ok.data['work_area']), (200, 'Lantai 2'))

    def test_only_a_subcontractor_in_their_own_active_project(self):
        self.assertEqual(self.post(self.payload(), user=self.officer).status_code, 403)
        self.assertEqual(self.post(self.payload(project=self.p2.id)).status_code, 400)   # not a member
        self.p1.is_active = False
        self.p1.save()
        self.assertEqual(self.post(self.payload()).status_code, 400)                     # inactive project
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_granular_endpoints_share_the_services_and_the_lock(self):
        doc = self.post({'project': self.p1.id, 'work_package': 'WP', 'work_area': 'WA'}).data['id']
        c = self.client_for(self.sub)
        g = c.post('/api/hira/groups/', {'hira_document': doc, 'location_function': 'G'}, format='json')
        self.assertEqual(g.status_code, 201, g.data)
        a = c.post('/api/hira/activities/', dict(act('A'), group=g.data['id']), format='json')
        self.assertEqual(a.status_code, 201, a.data)
        self.assertEqual(a.data['initial_score'], 8)
        for label, pk in (('hira.hiragroup', g.data['id']), ('hira.hiraactivity', a.data['id'])):
            entry = AuditLog.objects.get(resource_type=label, resource_id=pk, action='created')
            self.assertEqual(entry.project, self.p1)
        self.assertEqual(c.post(f'{URL}{doc}/submit/').status_code, 200)
        self.assertEqual(c.post('/api/hira/groups/', {'hira_document': doc, 'location_function': 'G2'},
                                format='json').status_code, 409)
        self.assertEqual(c.post('/api/hira/activities/', dict(act('B'), group=g.data['id']),
                                format='json').status_code, 409)

    def test_submit_requires_every_group_to_have_an_activity(self):
        groups = [{'location_function': 'Isi', 'activities': [act('A')]},
                  {'location_function': 'Kosong'}]
        created = self.post(self.payload(groups=groups))
        self.assertEqual(created.status_code, 201, created.data)
        doc, empty_group = created.data['id'], created.data['groups'][1]['id']
        c = self.client_for(self.sub)
        r = c.post(f'{URL}{doc}/submit/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Kosong', str(r.data))
        c.post('/api/hira/activities/', dict(act('B'), group=empty_group), format='json')
        r = c.post(f'{URL}{doc}/submit/')
        self.assertEqual((r.status_code, r.data['status']), (200, 'pending_officer'))

    def test_a_step_decides_only_once_per_round(self):
        doc = HIRADocument.objects.create(project=self.p1, work_package='WP', work_area='WA',
                                          created_by=self.sub)

        def decide(step, rnd):
            return HIRAApproval.objects.create(hira_document=doc, step=step, decision='approved',
                                               decided_by=self.officer, submission_round=rnd)

        decide('hse_officer', 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            decide('hse_officer', 1)         # same step, same round
        decide('hse_officer', 2)             # next round is fine
        decide('project_manager', 1)         # other step is fine
