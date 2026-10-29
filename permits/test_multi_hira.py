import re

from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from audit.models import AuditLog
from hira.models import HIRAActivity, HIRADocument, HIRAGroup
from . import routing
from . import tests as base
from .models import Permit
from .tests import LOC, LS, MEMORY

URL = '/api/permits/'


@MEMORY
@override_settings(FRONTEND_BASE_URL='https://app.test')
class MultiHIRATests(TestCase):
    # Borrow the workflow fixtures without inheriting (and re-running) its tests.
    setUpFixtures = base.PermitWorkflowTests.setUp
    call = base.PermitWorkflowTests.call
    make_hira = base.PermitWorkflowTests.make_hira

    def setUp(self):
        cache.clear()
        self.setUpFixtures()

    # helpers
    def make_doc(self, layout, owner=None, status=HIRADocument.Status.APPROVED):
        """layout: [['R', 'M'], ['T']] = two groups, with activities of those initial categories.
        Returns (document, [[activity, ...], ...])."""
        doc = HIRADocument.objects.create(project=self.p1, work_package='WP', work_area='WA',
                                          created_by=owner or self.sub, status=status)
        groups = []
        for gi, cats in enumerate(layout):
            group = HIRAGroup.objects.create(hira_document=doc, location_function=f'G{gi}', order=gi)
            acts = []
            for ai, cat in enumerate(cats):
                l, s = LS[cat]
                acts.append(HIRAActivity.objects.create(
                    group=group, order=ai, activity_name=f'{cat}-{gi}.{ai}', activity_type='R',
                    potential_hazard='x', risk_impact='y', initial_likelihood=l, initial_severity=s,
                    residual_likelihood=1, residual_severity=1))
            groups.append(acts)
        return doc, groups

    def header(self, **over):
        data = {
            'project': self.p1.id, 'permit_type': 'working_at_height',
            'work_date': '2026-10-20', 'work_start': '08:00', 'work_end': '16:00',
            'work_location': 'Lantai 3', 'work_description': 'Pasang bekisting',
            'equipment_used': 'Tangga', 'person_in_charge': 'Budi', 'worker_count': 4,
            'safety_areas': ['A'], 'checklist': {str(i): True for i in range(1, 15)},
            'ppe': ['body_harness'], 'ppe_other': [],
        }
        data.update(over)
        return data

    def make_permit(self, doc=None, acts=None, **over):
        body = self.header(**over)
        if doc is not None:
            body['hira_document'] = doc.id
        if acts is not None:
            body['hira_activity_ids'] = [a.id for a in acts]
        r = self.call(self.sub, 'post', URL, body)
        self.assertEqual(r.status_code, 201, r.data)
        return r.data['id']

    def link(self, pid, doc, acts, user=None):
        return self.call(user or self.sub, 'patch', f'{URL}{pid}/',
                         {'hira_document': doc.id, 'hira_activity_ids': [a.id for a in acts]})

    def submit(self, pid):
        base.add_photo(pid)
        return self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)

    # linking
    def test_create_with_several_activities_from_different_groups_of_one_document(self):
        doc, groups = self.make_doc([['R', 'M'], ['T', 'R']])
        chosen = [groups[0][1], groups[1][0]]                    # M from group 0, T from group 1
        pid = self.make_permit(doc, chosen)
        d = self.call(self.sub, 'get', f'{URL}{pid}/').data
        self.assertEqual(d['hira_document'], doc.id)
        self.assertEqual(sorted(d['hira_activity_ids']), sorted(a.id for a in chosen))
        self.assertEqual(d['highest_initial_category'], 'T')
        self.assertEqual(d['risk_category'], '')                 # only fixed at submit
        self.assertEqual([g['location_function'] for g in d['hira']['groups']], ['G0', 'G1'])
        self.assertEqual([[a['activity_name'] for a in g['activities']] for g in d['hira']['groups']],
                         [['M-0.1'], ['T-1.0']])                 # only the selected activities
        self.assertEqual(d['hira']['document']['id'], doc.id)

    def test_a_permit_can_be_created_first_and_linked_afterwards(self):
        doc, groups = self.make_doc([['R', 'M']])
        pid = self.make_permit()
        d = self.call(self.sub, 'get', f'{URL}{pid}/').data
        self.assertEqual((d['hira_document'], d['hira_activity_ids'], d['hira'], d['highest_initial_category']),
                         (None, [], None, None))
        base.add_photo(pid)
        r = self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)       # nothing linked yet
        self.assertEqual(r.status_code, 400)
        self.assertIn('hira_activity_ids', r.data)
        r = self.link(pid, doc, groups[0])
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC).status_code, 200)

    def test_picking_a_document_but_no_activity_is_a_valid_draft_that_cannot_submit(self):
        doc, _ = self.make_doc([['R']])
        pid = self.make_permit(doc)
        base.add_photo(pid)
        r = self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)
        self.assertEqual(r.status_code, 400)
        self.assertIn('hira_activity_ids', r.data)

    def test_activities_must_come_from_the_chosen_document(self):
        doc, groups = self.make_doc([['R']])
        other, other_groups = self.make_doc([['M']])
        r = self.call(self.sub, 'post', URL, self.header(hira_document=doc.id,
                                                         hira_activity_ids=[groups[0][0].id, other_groups[0][0].id]))
        self.assertEqual(r.status_code, 400)
        self.assertIn('hira_activity_ids', r.data)
        self.assertEqual(Permit.objects.count(), 0)

    def test_activities_without_a_document_are_refused(self):
        _, groups = self.make_doc([['R']])
        r = self.call(self.sub, 'post', URL, self.header(hira_activity_ids=[groups[0][0].id]))
        self.assertEqual(r.status_code, 400)
        self.assertIn('hira_document', r.data)

    def test_at_most_ten_activities(self):
        doc, groups = self.make_doc([['R'] * 6, ['R'] * 5])
        every = groups[0] + groups[1]                              # 11
        r = self.call(self.sub, 'post', URL, self.header(hira_document=doc.id,
                                                         hira_activity_ids=[a.id for a in every]))
        self.assertEqual(r.status_code, 400)
        self.assertIn('At most 10', str(r.data))
        self.assertEqual(self.call(self.sub, 'post', URL, self.header(
            hira_document=doc.id, hira_activity_ids=[a.id for a in every[:10]])).status_code, 201)

    def test_duplicates_are_ignored(self):
        doc, groups = self.make_doc([['R', 'M']])
        a = groups[0][0]
        pid = self.make_permit(doc, [a, a, a])
        self.assertEqual(self.call(self.sub, 'get', f'{URL}{pid}/').data['hira_activity_ids'], [a.id])

    def test_the_document_must_be_approved_in_this_project_and_the_creators_own(self):
        draft, dg = self.make_doc([['R']], status=HIRADocument.Status.DRAFT)
        theirs, tg = self.make_doc([['R']], owner=self.sub2)
        for doc, acts in ((draft, dg[0]), (theirs, tg[0])):
            r = self.call(self.sub, 'post', URL, self.header(hira_document=doc.id,
                                                             hira_activity_ids=[a.id for a in acts]))
            self.assertEqual(r.status_code, 400)
            self.assertIn('hira_document', r.data)
        abroad = HIRADocument.objects.create(project=self.p2, work_package='W', work_area='A',
                                             created_by=self.sub, status=HIRADocument.Status.APPROVED)
        self.assertEqual(self.call(self.sub, 'post', URL, self.header(hira_document=abroad.id)).status_code, 400)

    def test_changing_the_document_starts_the_ticks_over(self):
        a_doc, ag = self.make_doc([['R', 'M']])
        b_doc, bg = self.make_doc([['T']])
        pid = self.make_permit(a_doc, ag[0])
        r = self.call(self.sub, 'patch', f'{URL}{pid}/', {'hira_document': b_doc.id})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['hira_document'], r.data['hira_activity_ids']), (b_doc.id, []))
        r = self.call(self.sub, 'patch', f'{URL}{pid}/', {'hira_document': None})
        self.assertEqual((r.status_code, r.data['hira_document'], r.data['hira_activity_ids']), (200, None, []))

    def test_replacing_the_ticked_activities_within_the_same_document(self):
        doc, groups = self.make_doc([['R', 'M', 'T']])
        pid = self.make_permit(doc, [groups[0][0]])
        r = self.call(self.sub, 'patch', f'{URL}{pid}/', {'hira_activity_ids': [groups[0][1].id, groups[0][2].id]})
        self.assertEqual((r.status_code, sorted(r.data['hira_activity_ids'])),
                         (200, sorted([groups[0][1].id, groups[0][2].id])))
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{pid}/', {'hira_activity_ids': []}).data['hira_activity_ids'], [])

    def test_an_edit_that_does_not_mention_the_hira_leaves_the_links_alone(self):
        doc, groups = self.make_doc([['R', 'M']])
        pid = self.make_permit(doc, groups[0])
        r = self.call(self.sub, 'patch', f'{URL}{pid}/', {'worker_count': 9})
        self.assertEqual((r.status_code, len(r.data['hira_activity_ids'])), (200, 2))

    def test_links_are_locked_once_submitted(self):
        doc, groups = self.make_doc([['R', 'M']])
        pid = self.make_permit(doc, [groups[0][0]])
        self.assertEqual(self.submit(pid).status_code, 200)
        self.assertEqual(self.link(pid, doc, groups[0]).status_code, 409)

    # the rating
    def test_the_category_order_is_not_alphabetical(self):
        self.assertEqual(routing.highest(['R', 'M']), 'M')
        self.assertEqual(routing.highest(['R', 'T']), 'T')
        self.assertEqual(routing.highest(['M', 'E', 'T', 'R']), 'E')
        self.assertEqual(routing.highest(['E', 'T']), 'E')
        self.assertEqual(routing.highest(['R']), 'R')

    def test_the_highest_initial_category_decides_the_chain(self):
        for chosen, risk, first in (('RM', 'M', 'pending_supervisor'), ('RT', 'T', 'pending_supervisor'),
                                    ('RME', 'E', 'pending_supervisor'), ('RR', 'R', 'pending_supervisor')):
            doc, groups = self.make_doc([list(chosen)])
            pid = self.make_permit(doc, groups[0])
            self.assertEqual(self.submit(pid).status_code, 200)
            d = self.call(self.sub, 'get', f'{URL}{pid}/').data
            self.assertEqual((d['risk_category'], d['status']), (risk, first), chosen)
            self.assertEqual(d['approval_chain'][-1]['step'], 'mk' if risk in 'TE' else
                             {'R': 'supervisor', 'M': 'hse_officer'}[risk])

    def test_a_high_activity_among_low_ones_needs_the_mk(self):
        doc, groups = self.make_doc([['R', 'R'], ['T']])
        pid = self.make_permit(doc, groups[0] + groups[1])
        self.submit(pid)
        mail.outbox.clear()
        for user in (self.sup, self.officer):
            self.call(user, 'post', f'{URL}{pid}/approve/', LOC)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.call(self.pm, 'post', f'{URL}{pid}/approve/', {})
        self.assertEqual((r.status_code, r.data['status']), (200, 'pending_mk'))
        self.assertEqual(len(mail.outbox), 1)

    def test_the_category_is_recalculated_when_a_rejected_permit_changes_its_activities(self):
        doc, groups = self.make_doc([['R', 'T']])
        pid = self.make_permit(doc, groups[0])                     # R + T -> T
        self.submit(pid)
        self.assertEqual(Permit.objects.get(pk=pid).risk_category, 'T')
        self.call(self.sup, 'post', f'{URL}{pid}/reject/', {'remarks': 'Terlalu luas'})
        self.assertEqual(self.link(pid, doc, [groups[0][0]]).status_code, 200)     # now only R
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC).status_code, 200)
        p = Permit.objects.get(pk=pid)
        self.assertEqual((p.risk_category, p.status, p.submission_round), ('R', 'pending_supervisor', 2))
        self.assertEqual([c['step'] for c in self.call(self.sub, 'get', f'{URL}{pid}/').data['approval_chain']],
                         ['supervisor'])

    def test_a_t_or_e_among_the_activities_still_needs_an_mk_contact_to_submit(self):
        from projects.models import ProjectMKContact
        ProjectMKContact.objects.all().delete()
        doc, groups = self.make_doc([['R', 'E']])
        pid = self.make_permit(doc, groups[0])
        base.add_photo(pid)
        r = self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)
        self.assertEqual(r.status_code, 400)
        self.assertIn('mk', r.data)
        low_doc, low = self.make_doc([['R', 'M']])
        pid2 = self.make_permit(low_doc, low[0])
        self.assertEqual(self.submit(pid2).status_code, 200)

    # reuse, cancelling, audit
    def test_the_same_activity_can_be_used_by_several_permits(self):
        doc, groups = self.make_doc([['R', 'M']])
        first = self.make_permit(doc, groups[0])
        second = self.make_permit(doc, [groups[0][0]])
        self.assertEqual(self.submit(first).status_code, 200)
        self.assertEqual(self.submit(second).status_code, 200)
        self.assertEqual(groups[0][0].permits.count(), 2)

    def test_cancelling_the_hira_cancels_a_permit_linked_to_any_of_its_activities(self):
        doc, groups = self.make_doc([['R'], ['M']])
        multi = self.make_permit(doc, groups[0] + groups[1])
        one = self.make_permit(doc, [groups[1][0]])
        draft_only = self.make_permit(doc)                         # picked the document, nothing ticked
        unrelated_doc, ug = self.make_doc([['R']])
        unrelated = self.make_permit(unrelated_doc, ug[0])
        for pid in (multi, one):
            self.submit(pid)
        r = self.call(self.coord, 'post', f'/api/hira/documents/{doc.id}/cancel/', {'reason': 'Salah data'})
        self.assertEqual(r.status_code, 200, r.data)
        status = lambda pid: Permit.objects.get(pk=pid).status
        self.assertEqual([status(multi), status(one), status(draft_only)], ['cancelled'] * 3)
        self.assertEqual(status(unrelated), 'draft')
        self.assertEqual(AuditLog.objects.filter(resource_type='permits.permit', resource_id=multi,
                                                 action='cancelled').count(), 1)    # once, not once per activity

    def test_the_audit_history_records_which_activities_were_linked(self):
        doc, groups = self.make_doc([['R', 'M']])
        pid = self.make_permit(doc, [groups[0][0]])
        self.link(pid, doc, groups[0])
        self.submit(pid)
        entries = AuditLog.objects.filter(resource_type='permits.permit', resource_id=pid)
        sub = entries.get(action='submitted')
        self.assertEqual(sub.snapshot['hira_activity_ids'], sorted(a.id for a in groups[0]))
        upd = entries.filter(action='updated').first()
        self.assertEqual(upd.snapshot['hira_activity_ids'], sorted(a.id for a in groups[0]))
        created = entries.get(action='created')
        self.assertEqual(created.snapshot['hira_activity_ids'], [groups[0][0].id])   # linked at creation

    def test_the_mk_page_shows_only_the_selected_activities_in_document_layout(self):
        doc, groups = self.make_doc([['R', 'T'], ['M']])
        pid = self.make_permit(doc, [groups[0][1], groups[1][0]])
        self.submit(pid)
        mail.outbox.clear()
        for user in (self.sup, self.officer):
            self.call(user, 'post', f'{URL}{pid}/approve/', LOC)
        with self.captureOnCommitCallbacks(execute=True):
            self.call(self.pm, 'post', f'{URL}{pid}/approve/', {})
        token = re.search(r'/mk/([\w-]+)', mail.outbox[-1].body).group(1)
        data = APIClient().get(f'/api/mk/{token}/').data['permit']
        self.assertEqual([[a['activity_name'] for a in g['activities']] for g in data['hira']['groups']],
                         [['T-0.1'], ['M-1.0']])
        self.assertNotIn('hira_activity', data)


class MultiHIRAMigrationTests(TransactionTestCase):
    """0004-0006 turn each permit's single activity into one link plus its document, and back."""
    # Other apps stay at their latest state so the historical models match the real tables.
    def targets(self, permits_migration):
        leaves = {app: name for app, name in MigrationExecutor(connection).loader.graph.leaf_nodes()
                  if app in ('hira', 'projects', 'users')}
        return [('permits', permits_migration)] + list(leaves.items())

    @property
    def before(self):
        return self.targets('0003_mk_approval')

    @property
    def after(self):
        return self.targets('0006_remove_single_hira_activity')

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        executor = MigrationExecutor(connection)
        return executor.loader.project_state(targets).apps

    def test_existing_permits_keep_their_activity_as_a_link(self):
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes()))
        old = self.migrate(self.before)
        user = old.get_model('users', 'User').objects.create(email='m@t.com', full_name='m', role='subcontractor')
        project = old.get_model('projects', 'Project').objects.create(
            name='P', location='L', client_name='C', start_date='2026-10-01')
        doc = old.get_model('hira', 'HIRADocument').objects.create(
            project=project, work_package='WP', work_area='WA', created_by=user, status='approved')
        group = old.get_model('hira', 'HIRAGroup').objects.create(hira_document=doc, location_function='G')
        scores = dict(initial_severity=1, initial_likelihood=1, initial_score=1, initial_category='R',
                      initial_acceptable=True, residual_severity=1, residual_likelihood=1,
                      residual_score=1, residual_category='R', residual_acceptable=True)
        Activity = old.get_model('hira', 'HIRAActivity')
        act_a = Activity.objects.create(group=group, order=1, activity_name='a', activity_type='R',
                                        potential_hazard='x', risk_impact='y', **scores)
        act_b = Activity.objects.create(group=group, order=2, activity_name='b', activity_type='R',
                                        potential_hazard='x', risk_impact='y', **scores)
        Permit = old.get_model('permits', 'Permit')
        fields = dict(project=project, permit_type='working_at_height', work_date='2026-10-20',
                      work_start='08:00', work_end='16:00', work_location='L', work_description='D',
                      person_in_charge='P', worker_count=2, created_by=user)
        p1 = Permit.objects.create(hira_activity=act_a, **fields)
        p2 = Permit.objects.create(hira_activity=act_a, **fields)      # same activity, second permit
        p3 = Permit.objects.create(hira_activity=act_b, **fields)

        new = self.migrate(self.after)
        NewPermit = new.get_model('permits', 'Permit')
        for permit, activity in ((p1, act_a), (p2, act_a), (p3, act_b)):
            row = NewPermit.objects.get(pk=permit.pk)
            self.assertEqual(row.hira_document_id, doc.pk)
            self.assertEqual(list(row.hira_activities.values_list('pk', flat=True)), [activity.pk])
        self.assertFalse(hasattr(NewPermit, 'hira_activity'))

        back = self.migrate(self.before)
        BackPermit = back.get_model('permits', 'Permit')
        self.assertEqual([BackPermit.objects.get(pk=p.pk).hira_activity_id for p in (p1, p2, p3)],
                         [act_a.pk, act_a.pk, act_b.pk])
