from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from hira.models import HIRADocument
from permits import test_multi_hira as mh
from permits import tests as base
from permits.models import Permit
from permits.tests import LOC, MEMORY

STATS, TASKS = '/api/dashboard/stats/', '/api/dashboard/tasks/'
BUCKETS = {'total', 'draft', 'pending', 'approved', 'rejected'}
TASK_KEYS = {'id', 'type', 'title', 'category', 'status', 'status_label', 'project', 'project_name',
             'waiting_on', 'waiting_on_label', 'updated_at'}
H = HIRADocument.Status


@MEMORY
@override_settings(FRONTEND_BASE_URL='https://app.test')
class DashboardTests(TestCase):
    # Borrow the permit fixtures without inheriting (and re-running) their tests.
    setUpFixtures = base.PermitWorkflowTests.setUp
    call = base.PermitWorkflowTests.call
    make_doc = mh.MultiHIRATests.make_doc
    header = mh.MultiHIRATests.header
    make_permit = mh.MultiHIRATests.make_permit
    submit = mh.MultiHIRATests.submit

    def setUp(self):
        cache.clear()
        self.setUpFixtures()
        # HIRA documents by sub: approved, draft, rejected, cancelled, and one waiting on each approver
        self.doc, self.groups = self.make_doc([['R', 'M']])
        self.h_draft, _ = self.make_doc([['R']], status=H.DRAFT)
        self.h_rejected, _ = self.make_doc([['R']], status=H.REJECTED)
        self.h_cancelled, _ = self.make_doc([['R']], status=H.CANCELLED)
        self.h_officer, _ = self.make_doc([['R']], status=H.PENDING_OFFICER)
        self.h_pm, _ = self.make_doc([['R']], status=H.PENDING_PM)
        # permits by sub: draft, pending supervisor, rejected, cancelled
        self.p_draft = self.make_permit(self.doc, self.groups[0])
        self.p_pending = self.make_permit(self.doc, self.groups[0])
        self.submit(self.p_pending)
        self.p_rejected = self.make_permit(self.doc, self.groups[0])
        self.submit(self.p_rejected)
        self.call(self.sup, 'post', f'/api/permits/{self.p_rejected}/reject/', {'remarks': 'Ulang'})
        self.p_cancelled = self.make_permit(self.doc, self.groups[0])
        self.submit(self.p_cancelled)
        self.call(self.coord, 'post', f'/api/permits/{self.p_cancelled}/cancel/', {'reason': 'x'})

    def stats(self, user):
        r = self.call(user, 'get', STATS)
        self.assertEqual(r.status_code, 200)
        return r.data

    def tasks(self, user, query=''):
        r = self.call(user, 'get', TASKS + query)
        self.assertEqual(r.status_code, 200)
        return r.data

    def test_login_is_required(self):
        self.assertEqual(APIClient().get(STATS).status_code, 401)
        self.assertEqual(APIClient().get(TASKS).status_code, 401)

    def test_subcontractor_counts_their_own_documents_per_type(self):
        s = self.stats(self.sub)
        self.assertEqual(set(s), {'role', 'hira', 'permits', 'pending_action'})
        self.assertEqual(set(s['hira']), BUCKETS)
        self.assertEqual(s['role'], 'subcontractor')
        # 5 HIRA documents count, the cancelled one never does
        self.assertEqual(s['hira']['total'], 5)
        self.assertEqual((s['hira']['draft'], s['hira']['pending'], s['hira']['approved'], s['hira']['rejected']),
                         (1, 2, 1, 1))
        self.assertEqual(s['permits'], {'total': 3, 'draft': 1, 'pending': 1, 'approved': 0, 'rejected': 1})
        self.assertEqual(self.stats(self.sub2)['hira']['total'], 0)

    def test_the_subcontractor_queue_is_what_they_must_act_on(self):
        s = self.stats(self.sub)
        rows = self.tasks(self.sub)
        self.assertEqual(s['pending_action'], 4)                   # rejected + draft HIRA, rejected + draft permit
        self.assertEqual(len(rows), 4)
        self.assertEqual({r['waiting_on'] for r in rows}, {'subcontractor'})
        self.assertEqual({(r['type'], r['status']) for r in rows},
                         {('hira', 'rejected'), ('hira', 'draft'), ('permit', 'rejected'), ('permit', 'draft')})

    def test_an_approved_permit_waits_on_its_creator_to_request_closure(self):
        pid = self.make_permit(self.doc, [self.groups[0][0]])      # category R: supervisor only
        self.submit(pid)
        self.call(self.sup, 'post', f'/api/permits/{pid}/approve/', LOC)
        row = next(r for r in self.tasks(self.sub) if (r['type'], r['id']) == ('permit', pid))
        self.assertEqual((row['status'], row['waiting_on'], row['status_label']),
                         ('approved', 'subcontractor', 'Disetujui'))
        base.add_photo(pid, 'after')
        self.call(self.sub, 'post', f'/api/permits/{pid}/request-close/', {'post_work_evaluation': 'Selesai'})
        officer_rows = [r for r in self.tasks(self.officer) if (r['type'], r['id']) == ('permit', pid)]
        self.assertEqual([(r['status'], r['waiting_on']) for r in officer_rows], [('pending_close', 'hse_officer')])

    def test_supervisor_queue_holds_permits_waiting_on_them(self):
        rows = self.tasks(self.sup)
        self.assertEqual(self.stats(self.sup)['pending_action'], 1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), TASK_KEYS)
        self.assertEqual((row['id'], row['type'], row['category'], row['status'], row['waiting_on']),
                         (self.p_pending, 'permit', 'Izin Kerja', 'pending_supervisor', 'supervisor'))
        self.assertEqual((row['project_name'], row['waiting_on_label']), ('P1', 'Supervisor'))
        self.assertIn('Pasang bekisting', row['title'])
        # permits only: no draft, no cancelled, so 2 visible permits and 2 non-draft HIRA statuses beyond
        self.assertEqual(self.stats(self.sup)['permits']['total'], 2)

    def test_officer_and_pm_queues_follow_the_chain(self):
        self.call(self.sup, 'post', f'/api/permits/{self.p_pending}/approve/', LOC)    # now with the officer
        officer = self.tasks(self.officer)
        self.assertEqual({(r['type'], r['id']) for r in officer},
                         {('hira', self.h_officer.id), ('permit', self.p_pending)})
        self.assertEqual(self.stats(self.officer)['pending_action'], 2)
        pm = self.tasks(self.pm)
        self.assertEqual([(r['type'], r['id'], r['waiting_on']) for r in pm],
                         [('hira', self.h_pm.id, 'project_manager')])
        self.assertEqual(self.tasks(self.pm2), [])                  # a PM of another project sees nothing

    def test_coordinator_sees_everything_in_review_and_who_holds_it(self):
        self.call(self.sup, 'post', f'/api/permits/{self.p_pending}/approve/', LOC)
        rows = self.tasks(self.coord, '?limit=50')
        pairs = {(r['type'], r['id']): r['waiting_on'] for r in rows}
        self.assertEqual(pairs, {('hira', self.h_officer.id): 'hse_officer', ('hira', self.h_pm.id): 'project_manager',
                                 ('permit', self.p_pending): 'hse_officer'})
        self.assertEqual(self.stats(self.coord)['pending_action'], 3)
        s = self.stats(self.coord)
        self.assertEqual(s['permits']['total'], 3)                  # the coordinator also counts drafts
        self.assertEqual(s['permits']['draft'], 1)

    def test_pending_action_always_equals_the_full_queue(self):
        self.call(self.sup, 'post', f'/api/permits/{self.p_pending}/approve/', LOC)
        for user in (self.sub, self.sub2, self.sup, self.officer, self.pm, self.pm2, self.coord):
            self.assertEqual(self.stats(user)['pending_action'], len(self.tasks(user, '?limit=50')), user.email)

    def test_cancelled_documents_never_appear(self):
        for user in (self.sub, self.coord):
            self.assertNotIn(('permit', self.p_cancelled), {(r['type'], r['id']) for r in self.tasks(user, '?limit=50')})
            self.assertNotIn(('hira', self.h_cancelled.id), {(r['type'], r['id']) for r in self.tasks(user, '?limit=50')})

    def test_the_longest_waiting_comes_first_and_the_limit_applies(self):
        rows = self.tasks(self.sub, '?limit=50')
        stamps = [r['updated_at'] for r in rows]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(self.tasks(self.sub, '?limit=2')), 2)
        self.assertEqual(self.tasks(self.sub, '?limit=2'), rows[:2])
        self.assertEqual(self.stats(self.sub)['pending_action'], 4)  # the tile still shows the full size
        for bad in ('?limit=abc', '?limit=0', '?limit=-5'):
            self.assertLessEqual(len(self.tasks(self.sub, bad)), 10)
        self.assertEqual(len(self.tasks(self.sub, '?limit=9999')), 4)

    def test_a_decided_document_leaves_the_approvers_queue(self):
        self.call(self.sup, 'post', f'/api/permits/{self.p_pending}/approve/', LOC)
        self.assertEqual(self.tasks(self.sup), [])
        self.assertEqual(self.stats(self.sup)['pending_action'], 0)
