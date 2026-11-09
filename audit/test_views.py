from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from audit import labels
from audit.models import AuditLog
from hira.models import HIRADocument
from permits import test_multi_hira as mh
from permits import tests as base
from permits.models import Permit
from permits.tests import LOC, MEMORY

URL = '/api/audit/'
PREVIEW = {'id', 'timestamp', 'action', 'action_label', 'resource_type', 'resource_label', 'resource_id',
           'resource_title', 'project', 'project_name', 'actor_name', 'actor_role', 'summary', 'remarks'}
HEAVY = {'snapshot', 'ip_address', 'user_agent', 'user', 'actor_label'}


@MEMORY
@override_settings(FRONTEND_BASE_URL='https://app.test')
class AuditVisibilityTests(TestCase):
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
        self.doc, self.groups = self.make_doc([['R', 'M']])
        self.pid = self.make_permit(self.groups[0])          # a draft by sub
        self.other_doc, og = self.make_doc([['R']], owner=self.sub2)
        self.other_pid = self.call(self.sub2, 'post', '/api/permits/', self.header(
            hira_activity_ids=[og[0][0].id])).data['id']

    def entries(self, user, **params):
        r = self.call(user, 'get', URL + ('?' + '&'.join(f'{k}={v}' for k, v in params.items()) if params else ''))
        self.assertEqual(r.status_code, 200)
        return r.data['results']

    def move_to_rejected(self):
        self.submit(self.pid)
        self.call(self.sup, 'post', f'/api/permits/{self.pid}/reject/', {'remarks': 'Terlalu luas'})

    # the preview shape
    def test_list_rows_are_readable_previews_without_the_heavy_fields(self):
        rows = self.entries(self.coord, resource_type='permits.permit', resource_id=self.pid)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row), PREVIEW)
            self.assertFalse(HEAVY & set(row))
        created = rows[-1]
        self.assertEqual(created['action'], 'created')
        self.assertEqual(created['action_label'], 'Dibuat')
        self.assertEqual(created['resource_label'], 'Izin Kerja')
        self.assertIn('Pasang bekisting', created['resource_title'])
        self.assertEqual(created['project_name'], 'P1')
        self.assertEqual((created['actor_name'], created['actor_role']), ('sub', 'subcontractor'))

    def test_decision_rows_say_who_decided_and_why_without_coordinates(self):
        self.move_to_rejected()
        row = next(r for r in self.entries(self.sub, resource_type='permits.permit', resource_id=self.pid)
                   if r['action'] == 'rejected')
        self.assertEqual(row['action_label'], 'Ditolak')
        self.assertEqual(row['summary'], 'Supervisor: Terlalu luas')
        self.assertEqual(row['actor_name'], 'sup')
        submitted = next(r for r in self.entries(self.sub, resource_type='permits.permit', resource_id=self.pid)
                         if r['action'] == 'submitted')
        self.assertEqual(submitted['summary'], 'Putaran 1, kategori M')
        for r in self.entries(self.sub):
            self.assertNotIn('location=', str(r))
            self.assertNotIn('107.619', str(r))

    def test_approval_rows_hide_the_approvers_coordinates_in_the_preview(self):
        self.submit(self.pid)
        self.call(self.sup, 'post', f'/api/permits/{self.pid}/approve/', {'remarks': 'OK', **LOC})
        row = next(r for r in self.entries(self.officer, resource_type='permits.permit', resource_id=self.pid)
                   if r['action'] == 'approved')
        self.assertEqual(row['summary'], 'Supervisor: OK')
        self.assertNotIn('107.619', str(row))
        raw = self.call(self.officer, 'get', f'{URL}{AuditLog.objects.get(pk=row["id"]).pk}/').data
        self.assertIn('location=', raw['remarks'])             # the HSE roles still get the raw text

    # detail
    def test_only_officer_and_coordinator_get_the_full_record(self):
        self.submit(self.pid)
        entry = AuditLog.objects.filter(resource_type='permits.permit', resource_id=self.pid,
                                        action='submitted').get()
        for user in (self.coord, self.officer):
            d = self.call(user, 'get', f'{URL}{entry.pk}/').data
            self.assertTrue(HEAVY.issubset(d), user.email)
            self.assertEqual(d['snapshot']['work_description'], 'Pasang bekisting')
        for user in (self.pm, self.sup, self.sub):
            r = self.call(user, 'get', f'{URL}{entry.pk}/')
            self.assertEqual(r.status_code, 200, user.email)
            self.assertEqual(set(r.data), PREVIEW)

    # who sees what
    def test_subcontractor_sees_the_history_of_their_own_documents_only(self):
        self.move_to_rejected()
        rows = self.entries(self.sub)
        keys = {(r['resource_type'], r['resource_id']) for r in rows}
        self.assertEqual(keys, {('permits.permit', self.pid), ('hira.hiradocument', self.doc.id)} & keys)
        self.assertIn(('permits.permit', self.pid), keys)
        self.assertNotIn(('permits.permit', self.other_pid), keys)
        self.assertNotIn('hira.hiragroup', {k[0] for k in keys})
        self.assertNotIn('projects.project', {k[0] for k in keys})
        self.assertIn('rejected', {r['action'] for r in rows})             # the approver's decision on their work
        entry = AuditLog.objects.filter(resource_type='permits.permit', resource_id=self.other_pid).first()
        self.assertEqual(self.call(self.sub, 'get', f'{URL}{entry.pk}/').status_code, 404)

    def test_drafts_are_visible_to_the_creator_and_coordinator_only(self):
        for user, sees in ((self.sub, True), (self.coord, True), (self.officer, False),
                           (self.pm, False), (self.sup, False), (self.sub2, False)):
            ids = {r['resource_id'] for r in self.entries(user, resource_type='permits.permit')}
            self.assertEqual(self.pid in ids, sees, user.email)
        self.submit(self.pid)
        for user in (self.officer, self.pm, self.sup):
            ids = {r['resource_id'] for r in self.entries(user, resource_type='permits.permit')}
            self.assertIn(self.pid, ids, user.email)

    def test_pm_and_supervisor_only_see_their_own_projects(self):
        self.submit(self.pid)
        self.assertTrue(self.entries(self.pm))
        self.assertEqual({r['project'] for r in self.entries(self.pm)}, {self.p1.id})
        self.assertEqual(self.entries(self.pm2), [])               # member of P2, nothing submitted there
        self.assertEqual(self.entries(self.officer, project=self.p2.id), [])

    def test_mk_rows_never_show_emails_and_otp_rows_stay_with_the_hse_roles(self):
        doc, groups = self.make_doc([['T']])
        pid = self.make_permit(groups[0])
        self.submit(pid)
        for user in (self.sup, self.officer):
            self.call(user, 'post', f'/api/permits/{pid}/approve/', LOC)
        with self.captureOnCommitCallbacks(execute=True):
            self.call(self.pm, 'post', f'/api/permits/{pid}/approve/', {})
        AuditLog.objects.create(resource_type='permits.permit', resource_id=pid, action='mk_otp_sent',
                                project=self.p1, actor_label='MK: Pak MK <mk@kons.test> (external)',
                                remarks='sent to mk@kons.test')
        link = next(r for r in self.entries(self.sub, resource_type='permits.permit', resource_id=pid)
                    if r['action'] == 'mk_link_issued')
        self.assertEqual(link['summary'], 'Putaran 1')
        self.assertNotIn('kons.test', str(self.entries(self.sub)))
        for user in (self.sub, self.pm, self.sup):
            self.assertNotIn('mk_otp_sent', {r['action'] for r in self.entries(user)})
        otp = next(r for r in self.entries(self.officer) if r['action'] == 'mk_otp_sent')
        self.assertEqual((otp['actor_name'], otp['actor_role']), ('Pak MK (MK)', 'mk'))
        self.assertEqual(otp['remarks'], '')

    def test_removal_rows_do_not_leak_storage_paths(self):
        AuditLog.objects.create(resource_type='permits.permit', resource_id=self.pid, action='photo_removed',
                                project=self.p1, actor_label='sub', remarks='before: permits/5/abc.jpg')
        row = next(r for r in self.entries(self.sub) if r['action'] == 'photo_removed')
        self.assertEqual((row['summary'], row['remarks']), ('before', 'before'))
        self.assertNotIn('abc.jpg', str(row))

    def test_filters_and_pagination_still_work(self):
        self.submit(self.pid)
        r = self.call(self.coord, 'get', URL + f'?resource_type=permits.permit&resource_id={self.pid}&action=submitted&page_size=1')
        self.assertEqual((r.data['count'], len(r.data['results'])), (1, 1))
        self.assertEqual(self.call(self.coord, 'get', URL + '?date_from=2999-01-01').data['count'], 0)


class AuditLabelTests(SimpleTestCase):
    def test_unknown_values_fall_back_to_something_readable(self):
        self.assertEqual(labels.action_label('some_new_action'), 'Some new action')
        self.assertEqual(labels.resource_label('x.y'), 'x.y')
        self.assertEqual(labels.resource_title('x.y', {}, 9), '#9')

    def test_summaries(self):
        S = labels.summary
        self.assertEqual(S('rejected', '[hse_officer] Perbaiki ', {}), 'HSE Officer: Perbaiki')
        self.assertEqual(S('approved', '[project_manager] ', {}), 'Project Manager')
        self.assertEqual(S('submitted', 'Resubmission after rejection', {'submission_round': 2}), 'Putaran 2')
        self.assertEqual(S('cancelled', 'Salah data (2 permit(s) cancelled)', {}), 'Salah data (2 permit(s) cancelled)')
        self.assertEqual(S('updated', 'document_number: "" -> "IK-1"', {}), 'Nomor dokumen: "" -> "IK-1"')
        self.assertEqual(S('created', '', {}), '')

    def test_titles_come_from_the_snapshot(self):
        t = labels.resource_title
        self.assertEqual(t('permits.permit', {'document_number': 'IK-7', 'work_description': 'Pasang'}, 1), 'IK-7 · Pasang')
        self.assertEqual(t('hira.hiradocument', {'work_package': 'Struktur'}, 3), '#3 · Struktur')
