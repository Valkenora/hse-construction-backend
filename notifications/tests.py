import re
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from hira.models import HIRADocument
from permits import test_multi_hira as mh
from permits import tests as base
from permits.tests import LOC, MEMORY
from projects.models import ProjectMember
from users.models import User
from .models import Notification

URL = '/api/notifications/'
PERMITS = '/api/permits/'
H = HIRADocument.Status
R = User.Role


@MEMORY
@override_settings(FRONTEND_BASE_URL='https://app.test')
class NotificationTests(TestCase):
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
        mk = lambda n, r: User.objects.create_user(f'{n}@t.com', 'pass12345', full_name=n, role=r)
        self.sup2, self.sup_other = mk('sup2', R.SUPERVISOR), mk('sup_other', R.SUPERVISOR)
        self.officer2, self.coord2 = mk('off2', R.HSE_OFFICER), mk('coord2', R.HSE_COORDINATOR)
        ProjectMember.objects.create(project=self.p1, user=self.sup2)
        ProjectMember.objects.create(project=self.p2, user=self.sup_other)
        self.doc, self.groups = self.make_doc([['R', 'M', 'T']])
        self.everyone = (self.sub, self.sub2, self.sup, self.sup2, self.sup_other, self.officer, self.officer2,
                         self.pm, self.pm2, self.coord, self.coord2)

    # helpers
    def permit(self, *cats):
        """A submitted-ready permit whose highest category decides the chain (R, M or T)."""
        index = {'R': 0, 'M': 1, 'T': 2}
        return self.make_permit([self.groups[0][index[c]] for c in cats])

    def rows(self, user, pid=None, kind=None, rtype='permit'):
        qs = Notification.objects.filter(recipient=user, resource_type=rtype)
        if pid is not None:
            qs = qs.filter(resource_id=pid)
        return qs.filter(kind=kind) if kind else qs

    def who_has(self, pid, kind, rtype='permit'):
        return {u.email.split('@')[0] for u in self.everyone if self.rows(u, pid, kind, rtype).exists()}

    def approve(self, user, pid, **extra):
        return self.call(user, 'post', f'{PERMITS}{pid}/approve/', {**LOC, **extra})

    # who is told
    def test_submitting_pings_the_supervisors_of_that_project_and_the_coordinators(self):
        pid = self.permit('R', 'M')
        self.submit(pid)
        self.assertEqual(self.who_has(pid, 'action_needed'), {'sup', 'sup2'})   # not sup_other (project 2)
        self.assertEqual(self.who_has(pid, 'submitted'), {'coord', 'coord2'})
        n = self.rows(self.sup, pid, 'action_needed').get()
        self.assertEqual((n.title, n.status, n.needs_action, n.read_at), ('Menunggu persetujuan Anda',
                                                                          'pending_supervisor', True, None))
        self.assertIn('Izin Kerja', n.body)
        self.assertIn('Pasang bekisting', n.body)
        self.assertIn('P1', n.body)
        self.assertEqual(self.rows(self.sub, pid).count(), 0)                  # the creator is not pinged

    def test_a_resubmission_pings_approvers_again_but_not_the_coordinators(self):
        pid = self.permit('R')
        self.submit(pid)
        self.call(self.sup, 'post', f'{PERMITS}{pid}/reject/', {'remarks': 'Ulang'})
        self.call(self.sub, 'post', f'{PERMITS}{pid}/submit/', LOC)
        self.assertEqual(self.rows(self.sup2, pid, 'action_needed').count(), 2)
        self.assertEqual(self.rows(self.coord, pid, 'submitted').count(), 1)
        self.assertIn('Diajukan ulang', self.rows(self.sup2, pid, 'action_needed').first().body)

    def test_the_chain_pings_officers_everywhere_then_the_pm_of_the_project(self):
        pid = self.permit('R', 'T')
        self.submit(pid)
        self.approve(self.sup, pid)
        self.assertEqual(self.who_has(pid, 'action_needed') - {'sup', 'sup2'}, {'off', 'off2'})
        self.approve(self.officer, pid)
        self.assertEqual(self.who_has(pid, 'action_needed') - {'sup', 'sup2', 'off', 'off2'}, {'pm'})  # not pm2
        with self.captureOnCommitCallbacks(execute=True):
            self.call(self.pm, 'post', f'{PERMITS}{pid}/approve/', {})
        self.assertEqual(self.who_has(pid, 'action_needed') - {'sup', 'sup2', 'off', 'off2', 'pm'}, set())  # MK: email only

    # someone else already handled it
    def test_when_one_person_acts_the_others_notice_stays_but_says_who_handled_it(self):
        pid = self.permit('R', 'M')
        self.submit(pid)
        self.approve(self.sup, pid)
        mine, theirs = self.rows(self.sup, pid, 'action_needed').get(), self.rows(self.sup2, pid, 'action_needed').get()
        for n in (mine, theirs):
            self.assertFalse(n.needs_action)
            self.assertEqual((n.resolved_by_name, n.resolution_note), ('sup', 'Sudah disetujui oleh sup'))
            self.assertIsNotNone(n.resolved_at)
        self.assertIsNone(theirs.read_at)                                      # still in their list, unread
        # the officers' fresh notices are open
        self.assertTrue(all(n.needs_action for n in self.rows(self.officer, pid, 'action_needed')))
        self.approve(self.officer2, pid)
        o1 = self.rows(self.officer, pid, 'action_needed').get()
        self.assertEqual((o1.needs_action, o1.resolution_note), (False, 'Sudah disetujui oleh off2'))

    def test_the_badge_counts_only_unread_things_that_still_matter(self):
        pid = self.permit('R', 'M')
        self.submit(pid)
        count = lambda u: self.call(u, 'get', URL + 'unread-count/').data['unread_count']
        self.assertEqual((count(self.sup), count(self.sup2), count(self.officer)), (1, 1, 0))
        self.approve(self.sup, pid)
        self.assertEqual((count(self.sup), count(self.sup2), count(self.officer)), (0, 0, 1))
        rows = self.call(self.sup2, 'get', URL).data['results']
        self.assertEqual(len(rows), 1)                                         # still listed
        self.assertEqual((rows[0]['needs_action'], rows[0]['resolution_note']), (False, 'Sudah disetujui oleh sup'))

    def test_a_rejection_clears_every_open_notice_and_tells_the_creator_why(self):
        pid = self.permit('R', 'M')
        self.submit(pid)
        self.approve(self.sup, pid)
        self.call(self.officer, 'post', f'{PERMITS}{pid}/reject/', {'remarks': 'Foto kurang jelas', **LOC})
        self.assertFalse(Notification.objects.filter(resource_id=pid, needs_action=True).exists())
        n = self.rows(self.sub, pid, 'rejected').get()
        self.assertEqual(n.title, 'Ditolak')
        self.assertIn('HSE Officer: Foto kurang jelas', n.body)
        self.assertEqual(self.rows(self.officer2, pid, 'action_needed').get().resolution_note,
                         'Sudah ditolak oleh off')

    # final states
    def test_full_approval_tells_the_creator_and_the_coordinators(self):
        pid = self.permit('R')
        self.submit(pid)
        self.approve(self.sup, pid)
        self.assertEqual(self.who_has(pid, 'approved'), {'sub', 'coord', 'coord2'})
        self.assertEqual(self.rows(self.sub, pid, 'approved').get().title, 'Disetujui sepenuhnya')

    def test_the_mk_decision_notifies_like_any_final_approval(self):
        pid = self.permit('T')
        self.submit(pid)
        mail.outbox.clear()
        self.approve(self.sup, pid)
        self.approve(self.officer, pid)
        with self.captureOnCommitCallbacks(execute=True):
            self.call(self.pm, 'post', f'{PERMITS}{pid}/approve/', {})
        token = re.search(r'/mk/([\w-]+)', mail.outbox[-1].body).group(1)
        public = APIClient()
        mail.outbox.clear()
        self.assertEqual(public.post(f'/api/mk/{token}/send-otp/').status_code, 200)
        code = re.search(r'Kode verifikasi persetujuan Izin Kerja: (\d{6})', mail.outbox[-1].body).group(1)
        r = public.post(f'/api/mk/{token}/approve/', {'otp': code, 'full_name': 'Pak MK'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.who_has(pid, 'approved'), {'sub', 'coord', 'coord2'})
        self.assertFalse(Notification.objects.filter(resource_id=pid, needs_action=True).exists())

    def test_closing_pings_officers_and_then_tells_the_creator(self):
        pid = self.permit('R')
        self.submit(pid)
        self.approve(self.sup, pid)
        base.add_photo(pid, 'after')
        self.call(self.sub, 'post', f'{PERMITS}{pid}/request-close/', {'post_work_evaluation': 'Selesai'})
        waiting = self.rows(self.officer, pid, 'action_needed').filter(status='pending_close').get()
        self.assertEqual(waiting.title, 'Menunggu konfirmasi penutupan Anda')
        self.assertEqual(self.who_has(pid, 'action_needed') - {'sup', 'sup2'}, {'off', 'off2'})
        self.call(self.officer, 'post', f'{PERMITS}{pid}/confirm-close/')
        waiting.refresh_from_db()
        self.assertEqual(waiting.resolution_note, 'Penutupan sudah dikonfirmasi oleh off')
        self.assertEqual(self.who_has(pid, 'closed'), {'sub'})

    def test_cancelling_clears_open_notices_and_tells_the_creator(self):
        pid = self.permit('R')
        self.submit(pid)
        self.call(self.coord, 'post', f'{PERMITS}{pid}/cancel/', {'reason': 'Salah lokasi'})
        n = self.rows(self.sup, pid, 'action_needed').get()
        self.assertEqual((n.needs_action, n.resolution_note), (False, 'Dibatalkan oleh coord'))
        c = self.rows(self.sub, pid, 'cancelled').get()
        self.assertIn('Salah lokasi', c.body)

    def test_cancelling_a_hira_notifies_the_creator_for_each_cancelled_permit(self):
        first, second = self.permit('R'), self.permit('M')
        self.submit(first)
        self.call(self.coord, 'post', f'/api/hira/documents/{self.doc.id}/cancel/', {'reason': 'Salah data'})
        self.assertEqual(self.who_has(first, 'cancelled'), {'sub'})
        self.assertEqual(self.who_has(second, 'cancelled'), {'sub'})
        self.assertEqual(self.who_has(self.doc.id, 'cancelled', 'hira'), {'sub'})
        self.assertFalse(Notification.objects.filter(resource_id=first, resource_type='permit', needs_action=True).exists())

    def test_hira_documents_follow_the_same_rules(self):
        draft, _ = self.make_doc([['R']], status=H.DRAFT)
        self.assertEqual(self.call(self.sub, 'post', f'/api/hira/documents/{draft.id}/submit/').status_code, 200)
        self.assertEqual(self.who_has(draft.id, 'action_needed', 'hira'), {'off', 'off2'})
        self.assertEqual(self.who_has(draft.id, 'submitted', 'hira'), {'coord', 'coord2'})
        self.call(self.officer, 'post', f'/api/hira/documents/{draft.id}/approve/', {})
        self.assertEqual(self.who_has(draft.id, 'action_needed', 'hira') - {'off', 'off2'}, {'pm'})
        self.assertEqual(self.rows(self.officer2, draft.id, 'action_needed', 'hira').get().resolution_note,
                         'Sudah disetujui oleh off')
        self.call(self.pm, 'post', f'/api/hira/documents/{draft.id}/approve/', {})
        self.assertEqual(self.who_has(draft.id, 'approved', 'hira'), {'sub', 'coord', 'coord2'})
        self.assertEqual(self.call(self.sub, 'get', URL).data['results'][0]['category'], 'HIRA')

    # it agrees with the dashboard
    def test_a_notice_that_needs_action_is_always_in_that_users_dashboard_queue(self):
        pid = self.permit('R', 'M')
        self.submit(pid)
        self.approve(self.sup, pid)
        for user in self.everyone:
            queued = {(t['type'], t['id']) for t in
                      self.call(user, 'get', '/api/dashboard/tasks/?limit=50').data}
            open_notices = {(n.resource_type, n.resource_id) for n in self.rows(user, kind='action_needed')
                            if n.needs_action}
            self.assertLessEqual(open_notices, queued, user.email)
        self.assertIn(('permit', pid), {(t['type'], t['id']) for t in
                                        self.call(self.officer, 'get', '/api/dashboard/tasks/').data})

    # safety
    def test_a_notification_failure_never_blocks_the_approval(self):
        pid = self.permit('R')
        base.add_photo(pid)
        with mock.patch('notifications.receivers.handle', side_effect=RuntimeError('boom')):
            with self.assertLogs('notifications.receivers', level='ERROR') as logs:
                r = self.call(self.sub, 'post', f'{PERMITS}{pid}/submit/', LOC)
        self.assertEqual((r.status_code, r.data['status']), (200, 'pending_supervisor'))
        self.assertIn('Could not create notifications', logs.output[0])
        self.assertEqual(Notification.objects.count(), 0)

    # the endpoints
    def test_the_endpoints_only_show_and_change_the_callers_own_rows(self):
        pid = self.permit('R')
        self.submit(pid)
        self.assertEqual(APIClient().get(URL).status_code, 401)
        data = self.call(self.sup, 'get', URL).data
        self.assertEqual((data['count'], len(data['results'])), (1, 1))
        row = data['results'][0]
        self.assertEqual(set(row), {'id', 'kind', 'title', 'body', 'type', 'resource_id', 'category', 'project',
                                    'project_name', 'status', 'created_at', 'read_at', 'needs_action',
                                    'resolved_at', 'resolved_by_name', 'resolution_note'})
        self.assertEqual((row['type'], row['resource_id'], row['category'], row['project_name']),
                         ('permit', pid, 'Izin Kerja', 'P1'))
        self.assertEqual(self.call(self.sup_other, 'post', f'{URL}{row["id"]}/read/').status_code, 404)
        r = self.call(self.sup, 'post', f'{URL}{row["id"]}/read/')
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.data['read_at'])
        first = r.data['read_at']
        self.assertEqual(self.call(self.sup, 'post', f'{URL}{row["id"]}/read/').data['read_at'], first)  # idempotent
        self.assertEqual(self.call(self.sup, 'get', URL + '?unread=true').data['count'], 0)
        self.assertEqual(self.call(self.sup, 'get', URL).data['count'], 1)

    def test_read_all_and_pagination(self):
        for _ in range(3):
            self.submit(self.permit('R'))
        self.assertEqual(self.call(self.sup, 'get', URL + '?page_size=2').data['count'], 3)
        self.assertEqual(len(self.call(self.sup, 'get', URL + '?page_size=2').data['results']), 2)
        self.assertEqual(self.call(self.sup, 'post', URL + 'read-all/').data, {'updated': 3})
        self.assertEqual(self.call(self.sup, 'get', URL + 'unread-count/').data, {'unread_count': 0})
        self.assertEqual(self.call(self.sup2, 'get', URL + 'unread-count/').data, {'unread_count': 3})  # not theirs
        self.assertEqual(self.call(self.sup, 'post', URL + 'read-all/').data, {'updated': 0})

    def test_inactive_users_are_not_notified(self):
        self.sup2.is_active = False
        self.sup2.save()
        pid = self.permit('R')
        self.submit(pid)
        self.assertEqual(self.who_has(pid, 'action_needed'), {'sup'})
