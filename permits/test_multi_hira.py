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

    def make_permit(self, acts=None, user=None, **over):
        body = self.header(**over)
        if acts is not None:
            body['hira_activity_ids'] = [a.id for a in acts]
        r = self.call(user or self.sub, 'post', URL, body)
        self.assertEqual(r.status_code, 201, r.data)
        return r.data['id']

    def link(self, pid, acts, user=None):
        return self.call(user or self.sub, 'patch', f'{URL}{pid}/', {'hira_activity_ids': [a.id for a in acts]})

    def submit(self, pid):
        base.add_photo(pid)
        return self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)

    # linking
    def test_create_with_several_activities_from_different_groups_of_one_document(self):
        doc, groups = self.make_doc([['R', 'M'], ['T', 'R']])
        chosen = [groups[0][1], groups[1][0]]                    # M from group 0, T from group 1
        pid = self.make_permit(chosen)
        d = self.call(self.sub, 'get', f'{URL}{pid}/').data
        self.assertEqual(d['hira_document_ids'], [doc.id])
        self.assertEqual(sorted(d['hira_activity_ids']), sorted(a.id for a in chosen))
        self.assertEqual(d['highest_initial_category'], 'T')
        self.assertEqual(d['risk_category'], '')                 # only fixed at submit
        (document,) = d['hira']['documents']
        self.assertEqual(document['document']['id'], doc.id)
        self.assertEqual([g['location_function'] for g in document['groups']], ['G0', 'G1'])
        self.assertEqual([[a['activity_name'] for a in g['activities']] for g in document['groups']],
                         [['M-0.1'], ['T-1.0']])                 # only the selected activities

    def test_a_permit_can_mix_documents_and_subcontractors_of_the_same_project(self):
        doc_a, ga = self.make_doc([['R', 'M']])
        doc_b, gb = self.make_doc([['T', 'R']], owner=self.sub2)
        chosen = [ga[0][1], gb[0][0]]                            # M from sub's document, T from sub2's
        pid = self.make_permit(chosen)
        d = self.call(self.sub, 'get', f'{URL}{pid}/').data
        self.assertEqual(d['hira_document_ids'], sorted([doc_a.id, doc_b.id]))
        self.assertEqual(d['highest_initial_category'], 'T')
        docs = d['hira']['documents']
        self.assertEqual([x['document']['id'] for x in docs], sorted([doc_a.id, doc_b.id]))
        by_id = {x['document']['id']: x for x in docs}
        self.assertEqual(by_id[doc_b.id]['document']['created_by_name'], 'sub2')
        self.assertEqual([[a['activity_name'] for a in g['activities']] for g in by_id[doc_a.id]['groups']], [['M-0.1']])
        self.assertEqual([[a['activity_name'] for a in g['activities']] for g in by_id[doc_b.id]['groups']], [['T-0.0']])
        self.assertEqual(self.submit(pid).status_code, 200)
        self.assertEqual(Permit.objects.get(pk=pid).risk_category, 'T')

    def test_screens_built_before_the_change_still_read_what_they_expect(self):
        """hira_document and hira.document/groups stay for one-document permits, exactly as before."""
        doc, groups = self.make_doc([['R', 'M'], ['T']])
        pid = self.make_permit([groups[0][1], groups[1][0]])
        d = self.call(self.sub, 'get', f'{URL}{pid}/').data
        self.assertEqual(d['hira_document'], doc.id)
        self.assertEqual(d['hira']['document']['id'], doc.id)
        self.assertEqual(d['hira']['document']['work_package'], 'WP')
        self.assertEqual([g['location_function'] for g in d['hira']['groups']], ['G0', 'G1'])
        self.assertEqual([[a['activity_name'] for a in g['activities']] for g in d['hira']['groups']],
                         [['M-0.1'], ['T-1.0']])
        self.assertEqual([g['document_id'] for g in d['hira']['groups']], [doc.id, doc.id])
        self.assertEqual([g['id'] for g in d['hira']['groups']],
                         [g['id'] for g in d['hira']['documents'][0]['groups']])

    def test_a_mixed_permit_shows_nothing_ambiguous_in_the_old_fields(self):
        doc_a, ga = self.make_doc([['R']])
        doc_b, gb = self.make_doc([['M']], owner=self.sub2)
        pid = self.make_permit([ga[0][0], gb[0][0]])
        d = self.call(self.sub, 'get', f'{URL}{pid}/').data
        self.assertIsNone(d['hira_document'])                                   # more than one document
        self.assertEqual(d['hira']['document']['id'], doc_a.id)                 # fallback: the first one
        self.assertEqual([(g['document_id'], g['activities'][0]['activity_name']) for g in d['hira']['groups']],
                         [(doc_a.id, 'R-0.0'), (doc_b.id, 'M-0.0')])           # nothing silently dropped
        self.assertEqual(len(d['hira']['documents']), 2)

    def test_the_old_fields_cannot_be_written(self):
        doc, groups = self.make_doc([['R']])
        pid = self.make_permit([groups[0][0]])
        other, og = self.make_doc([['M']])
        r = self.call(self.sub, 'patch', f'{URL}{pid}/', {'hira_document': other.id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data['hira_document'], r.data['hira_activity_ids']), (doc.id, [groups[0][0].id]))

    def test_an_old_client_that_still_sends_hira_document_is_not_broken(self):
        doc, groups = self.make_doc([['R']])
        r = self.call(self.sub, 'post', URL, {**self.header(), 'hira_document': doc.id,
                                              'hira_activity_ids': [groups[0][0].id]})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['hira_activity_ids'], [groups[0][0].id])
        self.assertEqual(r.data['hira_document'], doc.id)          # deprecated, read-only, derived from the activities

    def test_a_permit_can_be_created_first_and_linked_afterwards(self):
        doc, groups = self.make_doc([['R', 'M']])
        pid = self.make_permit()
        d = self.call(self.sub, 'get', f'{URL}{pid}/').data
        self.assertEqual((d['hira_document_ids'], d['hira_activity_ids'], d['hira'], d['highest_initial_category']),
                         ([], [], None, None))
        base.add_photo(pid)
        r = self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)       # nothing linked yet
        self.assertEqual(r.status_code, 400)
        self.assertIn('hira_activity_ids', r.data)
        r = self.link(pid, groups[0])
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC).status_code, 200)

    def test_activities_of_another_project_are_refused(self):
        _, groups = self.make_doc([['R']])
        abroad = HIRADocument.objects.create(project=self.p2, work_package='W', work_area='A',
                                             created_by=self.sub, status=HIRADocument.Status.APPROVED)
        g = HIRAGroup.objects.create(hira_document=abroad, location_function='X', order=0)
        l, sv = LS['R']
        other = HIRAActivity.objects.create(group=g, order=0, activity_name='x', activity_type='R',
                                            potential_hazard='x', risk_impact='y', initial_likelihood=l,
                                            initial_severity=sv, residual_likelihood=1, residual_severity=1)
        r = self.call(self.sub, 'post', URL, self.header(hira_activity_ids=[groups[0][0].id, other.id]))
        self.assertEqual(r.status_code, 400)
        self.assertIn('different project', str(r.data['hira_activity_ids']))
        self.assertEqual(Permit.objects.count(), 0)

    def test_at_most_ten_activities_counted_across_documents(self):
        doc, groups = self.make_doc([['R'] * 6])
        other, og = self.make_doc([['R'] * 5], owner=self.sub2)
        every = groups[0] + og[0]                                  # 11 in total
        r = self.call(self.sub, 'post', URL, self.header(hira_activity_ids=[a.id for a in every]))
        self.assertEqual(r.status_code, 400)
        self.assertIn('At most 10', str(r.data))
        self.assertEqual(self.call(self.sub, 'post', URL, self.header(
            hira_activity_ids=[a.id for a in every[:10]])).status_code, 201)

    def test_duplicates_are_ignored(self):
        doc, groups = self.make_doc([['R', 'M']])
        a = groups[0][0]
        pid = self.make_permit([a, a, a])
        self.assertEqual(self.call(self.sub, 'get', f'{URL}{pid}/').data['hira_activity_ids'], [a.id])

    def test_every_activity_must_come_from_an_approved_document(self):
        draft, dg = self.make_doc([['R']], status=HIRADocument.Status.DRAFT)
        approved, ag = self.make_doc([['R']])
        r = self.call(self.sub, 'post', URL, self.header(hira_activity_ids=[ag[0][0].id, dg[0][0].id]))
        self.assertEqual(r.status_code, 400)
        self.assertIn(f'#{draft.id}', str(r.data['hira_activity_ids']))
        self.assertIn('not approved', str(r.data['hira_activity_ids']))
        self.assertEqual(Permit.objects.count(), 0)

    def test_another_subcontractors_approved_hira_can_be_used(self):
        theirs, tg = self.make_doc([['R', 'M']], owner=self.sub2)
        pid = self.make_permit([tg[0][1]])
        self.assertEqual(self.call(self.sub, 'get', f'{URL}{pid}/').data['hira_activity_ids'], [tg[0][1].id])
        self.assertEqual(self.submit(pid).status_code, 200)

    def test_the_ticks_can_move_to_another_document(self):
        a_doc, ag = self.make_doc([['R', 'M']])
        b_doc, bg = self.make_doc([['T']], owner=self.sub2)
        pid = self.make_permit(ag[0])
        r = self.link(pid, bg[0])
        self.assertEqual((r.status_code, r.data['hira_document_ids']), (200, [b_doc.id]), r.data)
        r = self.link(pid, [ag[0][0], bg[0][0]])
        self.assertEqual(r.data['hira_document_ids'], sorted([a_doc.id, b_doc.id]))

    def test_replacing_the_ticked_activities_within_the_same_document(self):
        doc, groups = self.make_doc([['R', 'M', 'T']])
        pid = self.make_permit([groups[0][0]])
        r = self.call(self.sub, 'patch', f'{URL}{pid}/', {'hira_activity_ids': [groups[0][1].id, groups[0][2].id]})
        self.assertEqual((r.status_code, sorted(r.data['hira_activity_ids'])),
                         (200, sorted([groups[0][1].id, groups[0][2].id])))
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{pid}/', {'hira_activity_ids': []}).data['hira_activity_ids'], [])

    def test_an_edit_that_does_not_mention_the_hira_leaves_the_links_alone(self):
        doc, groups = self.make_doc([['R', 'M']])
        pid = self.make_permit(groups[0])
        r = self.call(self.sub, 'patch', f'{URL}{pid}/', {'worker_count': 9})
        self.assertEqual((r.status_code, len(r.data['hira_activity_ids'])), (200, 2))

    def test_links_are_locked_once_submitted(self):
        doc, groups = self.make_doc([['R', 'M']])
        pid = self.make_permit([groups[0][0]])
        self.assertEqual(self.submit(pid).status_code, 200)
        self.assertEqual(self.link(pid, groups[0]).status_code, 409)

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
            pid = self.make_permit(groups[0])
            self.assertEqual(self.submit(pid).status_code, 200)
            d = self.call(self.sub, 'get', f'{URL}{pid}/').data
            self.assertEqual((d['risk_category'], d['status']), (risk, first), chosen)
            self.assertEqual(d['approval_chain'][-1]['step'], 'mk' if risk in 'TE' else
                             {'R': 'supervisor', 'M': 'hse_officer'}[risk])

    def test_a_high_activity_among_low_ones_needs_the_mk(self):
        doc, groups = self.make_doc([['R', 'R'], ['T']])
        pid = self.make_permit(groups[0] + groups[1])
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
        pid = self.make_permit(groups[0])                     # R + T -> T
        self.submit(pid)
        self.assertEqual(Permit.objects.get(pk=pid).risk_category, 'T')
        self.call(self.sup, 'post', f'{URL}{pid}/reject/', {'remarks': 'Terlalu luas'})
        self.assertEqual(self.link(pid, [groups[0][0]]).status_code, 200)     # now only R
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC).status_code, 200)
        p = Permit.objects.get(pk=pid)
        self.assertEqual((p.risk_category, p.status, p.submission_round), ('R', 'pending_supervisor', 2))
        self.assertEqual([c['step'] for c in self.call(self.sub, 'get', f'{URL}{pid}/').data['approval_chain']],
                         ['supervisor'])

    def test_a_t_or_e_among_the_activities_still_needs_an_mk_contact_to_submit(self):
        from projects.models import ProjectMKContact
        ProjectMKContact.objects.all().delete()
        doc, groups = self.make_doc([['R', 'E']])
        pid = self.make_permit(groups[0])
        base.add_photo(pid)
        r = self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)
        self.assertEqual(r.status_code, 400)
        self.assertIn('mk', r.data)
        low_doc, low = self.make_doc([['R', 'M']])
        pid2 = self.make_permit(low[0])
        self.assertEqual(self.submit(pid2).status_code, 200)

    # reuse, cancelling, audit
    def test_the_same_activity_can_be_used_by_several_permits(self):
        doc, groups = self.make_doc([['R', 'M']])
        first = self.make_permit(groups[0])
        second = self.make_permit([groups[0][0]])
        self.assertEqual(self.submit(first).status_code, 200)
        self.assertEqual(self.submit(second).status_code, 200)
        self.assertEqual(groups[0][0].permits.count(), 2)

    def test_cancelling_the_hira_cancels_a_permit_linked_to_any_of_its_activities(self):
        doc, groups = self.make_doc([['R'], ['M']])
        multi = self.make_permit(groups[0] + groups[1])
        one = self.make_permit([groups[1][0]])
        header_only = self.make_permit()                           # nothing linked: unaffected
        unrelated_doc, ug = self.make_doc([['R']])
        unrelated = self.make_permit(ug[0])
        for pid in (multi, one):
            self.submit(pid)
        r = self.call(self.coord, 'post', f'/api/hira/documents/{doc.id}/cancel/', {'reason': 'Salah data'})
        self.assertEqual(r.status_code, 200, r.data)
        status = lambda pid: Permit.objects.get(pk=pid).status
        self.assertEqual([status(multi), status(one)], ['cancelled'] * 2)
        self.assertEqual([status(header_only), status(unrelated)], ['draft'] * 2)
        self.assertEqual(AuditLog.objects.filter(resource_type='permits.permit', resource_id=multi,
                                                 action='cancelled').count(), 1)    # once, not once per activity

    def test_cancelling_one_linked_hira_cancels_the_whole_permit_and_other_subcontractors_permits(self):
        doc_a, ga = self.make_doc([['R']])
        doc_b, gb = self.make_doc([['M']], owner=self.sub2)
        both = self.make_permit([ga[0][0], gb[0][0]])                          # by sub, spans two documents
        theirs = self.make_permit([ga[0][0]], user=self.sub2)                  # by sub2, uses sub's HIRA
        self.submit(both)
        r = self.call(self.coord, 'post', f'/api/hira/documents/{doc_b.id}/cancel/', {'reason': 'Salah data'})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Permit.objects.get(pk=both).status, 'cancelled')       # one cancelled HIRA is enough
        self.assertEqual(Permit.objects.get(pk=theirs).status, 'draft')         # does not use doc_b
        self.call(self.coord, 'post', f'/api/hira/documents/{doc_a.id}/cancel/', {'reason': 'Salah data'})
        self.assertEqual(Permit.objects.get(pk=theirs).status, 'cancelled')     # another subcontractor's permit

    def test_a_linked_hira_that_stops_being_approved_blocks_submit_and_approval(self):
        doc_a, ga = self.make_doc([['R']])
        doc_b, gb = self.make_doc([['R']], owner=self.sub2)
        pid = self.make_permit([ga[0][0], gb[0][0]])
        HIRADocument.objects.filter(pk=doc_b.pk).update(status=HIRADocument.Status.REJECTED)
        base.add_photo(pid)
        r = self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)
        self.assertEqual(r.status_code, 400)
        self.assertIn(f'#{doc_b.id}', str(r.data['hira_activity_ids']))
        HIRADocument.objects.filter(pk=doc_b.pk).update(status=HIRADocument.Status.APPROVED)
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC).status_code, 200)
        HIRADocument.objects.filter(pk=doc_b.pk).update(status=HIRADocument.Status.REJECTED)
        r = self.call(self.sup, 'post', f'{URL}{pid}/approve/', LOC)
        self.assertEqual(r.status_code, 409)
        self.assertIn('no longer approved', str(r.data))

    def test_the_audit_history_records_which_activities_were_linked(self):
        doc, groups = self.make_doc([['R', 'M']])
        pid = self.make_permit([groups[0][0]])
        self.link(pid, groups[0])
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
        pid = self.make_permit([groups[0][1], groups[1][0]])
        self.submit(pid)
        mail.outbox.clear()
        for user in (self.sup, self.officer):
            self.call(user, 'post', f'{URL}{pid}/approve/', LOC)
        with self.captureOnCommitCallbacks(execute=True):
            self.call(self.pm, 'post', f'{URL}{pid}/approve/', {})
        token = re.search(r'/mk/([\w-]+)', mail.outbox[-1].body).group(1)
        data = APIClient().get(f'/api/mk/{token}/').data['permit']
        (document,) = data['hira']['documents']
        self.assertEqual([[a['activity_name'] for a in g['activities']] for g in document['groups']],
                         [['T-0.1'], ['M-1.0']])
        self.assertNotIn('hira_activity', data)

    # the picker
    def options(self, user, project=None):
        return self.call(user, 'get', f'{URL}hira-options/' + (f'?project={project}' if project else ''))

    def test_the_picker_lists_every_approved_hira_of_the_project_whoever_made_it(self):
        mine, mg = self.make_doc([['R', 'M'], ['T']])
        theirs, tg = self.make_doc([['R']], owner=self.sub2)
        self.make_doc([['R']], status=HIRADocument.Status.DRAFT)                  # not approved
        self.make_doc([['R']], owner=self.sub2, status=HIRADocument.Status.CANCELLED)
        abroad = HIRADocument.objects.create(project=self.p2, work_package='W', work_area='A',
                                             created_by=self.sub, status=HIRADocument.Status.APPROVED)
        r = self.options(self.sub, self.p1.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([d['id'] for d in r.data], sorted([mine.id, theirs.id], reverse=True))
        by_id = {d['id']: d for d in r.data}
        self.assertEqual((by_id[mine.id]['mine'], by_id[theirs.id]['mine']), (True, False))
        self.assertEqual(by_id[theirs.id]['created_by_name'], 'sub2')
        self.assertNotIn(abroad.id, by_id)
        groups = by_id[mine.id]['groups']
        self.assertEqual([[a['activity_name'] for a in g['activities']] for g in groups], [['R-0.0', 'M-0.1'], ['T-1.0']])
        first = groups[0]['activities'][0]
        self.assertTrue({'id', 'activity_name', 'potential_hazard', 'initial_category'} <= set(first))
        self.assertEqual(first['id'], mg[0][0].id)
        # another subcontractor sees the same list, with `mine` flipped
        r2 = self.options(self.sub2, self.p1.id)
        self.assertEqual({d['id']: d['mine'] for d in r2.data}, {mine.id: False, theirs.id: True})

    def test_what_the_picker_returns_can_be_linked(self):
        theirs, tg = self.make_doc([['M']], owner=self.sub2)
        offered = self.options(self.sub, self.p1.id).data[0]['groups'][0]['activities'][0]['id']
        pid = self.make_permit()
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{pid}/', {'hira_activity_ids': [offered]}).status_code, 200)

    def test_the_picker_needs_a_project_you_belong_to_and_the_right_role(self):
        self.make_doc([['R']])
        self.assertEqual(self.options(self.sub).status_code, 400)
        self.assertEqual(self.options(self.sub, 'abc').status_code, 400)
        self.assertEqual(self.options(self.sub, self.p2.id).status_code, 403)       # not a member of project 2
        self.assertEqual(self.options(self.sup, self.p1.id).status_code, 403)       # only subcontractors link permits
        self.assertEqual(APIClient().get(f'{URL}hira-options/?project={self.p1.id}').status_code, 401)



class MigrationHelpers:
    # Other apps stay at their latest state so the historical models match the real tables.
    def targets(self, permits_migration):
        leaves = {app: name for app, name in MigrationExecutor(connection).loader.graph.leaf_nodes()
                  if app in ('hira', 'projects', 'users')}
        return [('permits', permits_migration)] + list(leaves.items())

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        executor = MigrationExecutor(connection)
        return executor.loader.project_state(targets).apps

    def restore_latest(self):
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def seed_hira(self, apps, n_docs=2):
        """A project with `n_docs` approved HIRA documents of one activity each. Returns (user, project, activities)."""
        user = apps.get_model('users', 'User').objects.create(email='m@t.com', full_name='m', role='subcontractor')
        project = apps.get_model('projects', 'Project').objects.create(
            name='P', location='L', client_name='C', start_date='2026-10-01')
        scores = dict(initial_severity=1, initial_likelihood=1, initial_score=1, initial_category='R',
                      initial_acceptable=True, residual_severity=1, residual_likelihood=1,
                      residual_score=1, residual_category='R', residual_acceptable=True)
        acts = []
        for i in range(n_docs):
            doc = apps.get_model('hira', 'HIRADocument').objects.create(
                project=project, work_package=f'WP{i}', work_area='WA', created_by=user, status='approved')
            group = apps.get_model('hira', 'HIRAGroup').objects.create(hira_document=doc, location_function='G')
            acts.append(apps.get_model('hira', 'HIRAActivity').objects.create(
                group=group, order=1, activity_name=f'a{i}', activity_type='R',
                potential_hazard='x', risk_impact='y', **scores))
        return user, project, acts


class MultiHIRAMigrationTests(MigrationHelpers, TransactionTestCase):
    """0004-0006 turn each permit's single activity into one link plus its document, and back."""

    @property
    def before(self):
        return self.targets('0003_mk_approval')

    @property
    def after(self):
        return self.targets('0006_remove_single_hira_activity')

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


class DropPermitDocumentMigrationTests(MigrationHelpers, TransactionTestCase):
    """0007 drops Permit.hira_document (activities from several documents are allowed now);
    going back restores it from the first linked activity."""

    def test_forward_drops_the_column_and_back_restores_it_from_the_first_link(self):
        self.addCleanup(self.restore_latest)
        old = self.migrate(self.targets('0006_remove_single_hira_activity'))
        user, project, (act_a, act_b) = self.seed_hira(old)
        Permit = old.get_model('permits', 'Permit')
        Link = old.get_model('permits', 'PermitHIRAActivity')
        fields = dict(project=project, permit_type='working_at_height', work_date='2026-10-20',
                      work_start='08:00', work_end='16:00', work_location='L', work_description='D',
                      person_in_charge='P', worker_count=2, created_by=user)
        mixed = Permit.objects.create(hira_document=None, **fields)          # stale value must not matter
        Link.objects.create(permit=mixed, hira_activity=act_b)               # first link: document B
        Link.objects.create(permit=mixed, hira_activity=act_a)
        empty = Permit.objects.create(hira_document=None, **fields)

        new = self.migrate(self.targets('0007_drop_permit_hira_document'))
        NewPermit = new.get_model('permits', 'Permit')
        self.assertNotIn('hira_document', [f.name for f in NewPermit._meta.get_fields()])
        self.assertEqual(NewPermit.objects.get(pk=mixed.pk).hira_activities.count(), 2)   # links untouched

        back = self.migrate(self.targets('0006_remove_single_hira_activity'))
        BackPermit = back.get_model('permits', 'Permit')
        self.assertEqual(BackPermit.objects.get(pk=mixed.pk).hira_document_id, act_b.group.hira_document_id)
        self.assertIsNone(BackPermit.objects.get(pk=empty.pk).hira_document_id)
