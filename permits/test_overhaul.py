from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from audit.models import AuditLog
from hira.models import HIRAActivity, HIRADocument, HIRAGroup
from projects.models import Project, ProjectMember
from users.models import User
from .models import Permit
from .tests import LOC, MEMORY, add_photo, ensure_before_photo

R = User.Role
URL = '/api/permits/'
PLAIN_STATIC = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


@MEMORY
class Base(TestCase):
    def setUp(self):
        mk = lambda n, r, **kw: User.objects.create_user(f'{n}@t.com', 'pass12345', full_name=n, role=r, **kw)
        self.p1 = Project.objects.create(name='P1', location='L', client_name='C', start_date='2026-10-01')
        self.sub, self.sub2 = mk('sub', R.SUBCONTRACTOR), mk('sub2', R.SUBCONTRACTOR)
        self.sup, self.pm = mk('sup', R.SUPERVISOR), mk('pm', R.PROJECT_MANAGER)
        self.officer = mk('off', R.HSE_OFFICER, is_staff=True)
        self.coord = User.objects.create_superuser('coord@t.com', 'pass12345', full_name='coord',
                                                   role=R.HSE_COORDINATOR)
        for u in (self.sub, self.sub2, self.sup, self.pm):
            ProjectMember.objects.create(project=self.p1, user=u)

    def call(self, user, method, url, data=None):
        c = APIClient()
        c.force_authenticate(user)
        return getattr(c, method)(url, data or {}, format='json')

    def make_hira(self, owner=None, status=HIRADocument.Status.APPROVED):
        doc = HIRADocument.objects.create(project=self.p1, work_package='WP', work_area='WA',
                                          created_by=owner or self.sub, status=status)
        grp = HIRAGroup.objects.create(hira_document=doc, location_function='G')
        return HIRAActivity.objects.create(
            group=grp, activity_name='Kerja', activity_type='R', potential_hazard='x', risk_impact='y',
            initial_likelihood=1, initial_severity=1,      # Rt 1 -> R: Supervisor-only chain
            residual_likelihood=1, residual_severity=1)

    def payload(self, act, **over):
        data = {
            'project': self.p1.id, 'hira_document': act.group.hira_document_id, 'hira_activity_ids': [act.id],
            'permit_type': 'working_at_height',
            'work_date': '2026-10-20', 'work_start': '08:00', 'work_end': '16:00',
            'work_location': 'Lantai 3', 'work_description': 'Pasang bekisting',
            'equipment_used': 'Tangga', 'person_in_charge': 'Budi', 'worker_count': 4,
            'safety_areas': ['A'], 'checklist': {str(i): True for i in range(1, 15)},
            'ppe': ['body_harness'], 'ppe_other': [],
        }
        data.update(over)
        return data

    def create(self, act=None, user=None, **over):
        act = act or self.make_hira()
        r = self.call(user or self.sub, 'post', URL, self.payload(act, **over))
        self.assertEqual(r.status_code, 201, r.data)
        return r.data['id']

    def submit(self, pid):
        ensure_before_photo(pid)
        return self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)

    def approve(self, user, pid):
        return self.call(user, 'post', f'{URL}{pid}/approve/', LOC)


class OwnershipAndLockTests(Base):
    def test_a_permit_can_only_use_a_hira_its_creator_made(self):
        theirs = self.make_hira(owner=self.sub)
        r = self.call(self.sub2, 'post', URL, self.payload(theirs))
        self.assertEqual(r.status_code, 400)
        self.assertIn('hira_document', r.data)
        self.assertEqual(Permit.objects.count(), 0)
        self.create(act=self.make_hira(owner=self.sub2), user=self.sub2)    # their own is fine

    def test_submit_rechecks_ownership_even_if_the_row_was_made_another_way(self):
        pid = self.create(act=self.make_hira(owner=self.sub2), user=self.sub2)
        stolen = self.make_hira(owner=self.sub)                    # someone else's HIRA, linked behind the API's back
        Permit.objects.filter(pk=pid).update(hira_document=stolen.group.hira_document)
        Permit.objects.get(pk=pid).hira_activities.set([stolen])
        add_photo(pid)
        r = self.call(self.sub2, 'post', f'{URL}{pid}/submit/', LOC)
        self.assertEqual(r.status_code, 400)
        self.assertIn('hira_document', r.data)

    def test_patch_is_refused_by_the_lock_before_the_body_is_validated(self):
        pid = self.create()
        self.assertEqual(self.submit(pid).status_code, 200)
        # invalid body AND locked permit: the lock rule answers first (409, not 400)
        r = self.call(self.sub, 'patch', f'{URL}{pid}/', {'worker_count': -1})
        self.assertEqual(r.status_code, 409)

    def test_editing_without_touching_the_hira_does_not_recheck_it(self):
        act = self.make_hira()
        pid = self.create(act=act)
        HIRADocument.objects.filter(pk=act.group.hira_document_id).update(
            status=HIRADocument.Status.REJECTED)                    # simulate a HIRA no longer approved
        ok = self.call(self.sub, 'patch', f'{URL}{pid}/', {'work_description': 'Fixed typo'})
        self.assertEqual((ok.status_code, ok.data['work_description']), (200, 'Fixed typo'))
        # but choosing a HIRA is checked: not approved -> 400, someone else's -> 400, valid -> 200
        same = self.call(self.sub, 'patch', f'{URL}{pid}/', {'hira_document': act.group.hira_document_id,
                                                              'hira_activity_ids': [act.id]})
        self.assertEqual(same.status_code, 400)                     # explicitly choosing it again IS checked
        other = self.make_hira(status=HIRADocument.Status.DRAFT)
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{pid}/', {
            'hira_document': other.group.hira_document_id, 'hira_activity_ids': [other.id]}).status_code, 400)
        foreign = self.make_hira(owner=self.sub2)
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{pid}/', {
            'hira_document': foreign.group.hira_document_id, 'hira_activity_ids': [foreign.id]}).status_code, 400)
        good = self.make_hira()
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{pid}/', {
            'hira_document': good.group.hira_document_id, 'hira_activity_ids': [good.id]}).status_code, 200)

    def test_patch_still_writes_one_audit_entry(self):
        pid = self.create()
        self.call(self.sub, 'patch', f'{URL}{pid}/', {'worker_count': 6})
        self.assertEqual(AuditLog.objects.filter(resource_type='permits.permit', resource_id=pid,
                                                 action='updated').count(), 1)


class CancelCascadeTests(Base):
    def test_cancelling_a_hira_cancels_its_open_permits_only(self):
        act = self.make_hira()
        hira = act.group.hira_document
        draft = self.create(act=act)
        pending = self.create(act=act)
        self.submit(pending)
        approved = self.create(act=act)
        self.submit(approved)
        self.approve(self.sup, approved)
        closed = self.create(act=act)
        self.submit(closed)
        self.approve(self.sup, closed)
        add_photo(closed, 'after')
        self.call(self.sub, 'post', f'{URL}{closed}/request-close/', {'post_work_evaluation': 'ok'})
        self.call(self.officer, 'post', f'{URL}{closed}/confirm-close/')
        unrelated = self.create()                                   # a different HIRA

        r = self.call(self.coord, 'post', f'/api/hira/documents/{hira.pk}/cancel/', {'reason': 'Salah scope'})
        self.assertEqual(r.status_code, 200, r.data)

        status = lambda pk: Permit.objects.get(pk=pk).status
        self.assertEqual([status(p) for p in (draft, pending, approved)], ['cancelled'] * 3)
        self.assertEqual(status(closed), 'closed')                  # history is kept
        self.assertEqual(status(unrelated), 'draft')
        for pk in (draft, pending, approved):
            entry = AuditLog.objects.get(resource_type='permits.permit', resource_id=pk, action='cancelled')
            self.assertIn(f'HIRA #{hira.pk}', entry.remarks)
            self.assertEqual((entry.project, entry.user), (self.p1, self.coord))
        hira_entry = AuditLog.objects.get(resource_type='hira.hiradocument', resource_id=hira.pk,
                                          action='cancelled')
        self.assertIn('3 permit(s) cancelled', hira_entry.remarks)

    def test_a_hira_with_no_permits_cancels_normally(self):
        act = self.make_hira()
        r = self.call(self.coord, 'post', f'/api/hira/documents/{act.group.hira_document_id}/cancel/',
                      {'reason': 'x'})
        self.assertEqual(r.status_code, 200)
        entry = AuditLog.objects.get(resource_type='hira.hiradocument', action='cancelled')
        self.assertNotIn('permit(s)', entry.remarks)


class ProjectGuardTests(Base):
    def test_inactive_project_is_read_only_except_coordinator_cancel(self):
        draft, pending = self.create(), self.create()
        self.submit(pending)
        Project.objects.filter(pk=self.p1.pk).update(is_active=False)

        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{draft}/', {'worker_count': 5}).status_code, 409)
        self.assertEqual(self.submit(draft).status_code, 409)
        self.assertEqual(self.call(self.sub, 'delete', f'{URL}{draft}/').status_code, 409)
        self.assertEqual(self.approve(self.sup, pending).status_code, 409)
        self.assertEqual(self.call(self.officer, 'post', f'{URL}{pending}/set-number/',
                                   {'document_number': 'X'}).status_code, 409)
        self.assertEqual(self.call(self.sub, 'get', f'{URL}{draft}/').status_code, 200)   # still readable

        Project.objects.filter(pk=self.p1.pk).update(is_active=True)                       # comes back as it was
        self.assertEqual(self.approve(self.sup, pending).status_code, 200)

        Project.objects.filter(pk=self.p1.pk).update(is_active=False)
        self.assertEqual(self.call(self.coord, 'post', f'{URL}{draft}/cancel/', {'reason': 'x'}).status_code, 200)

    def test_inactive_project_blocks_closing(self):
        pid = self.create()
        self.submit(pid)
        self.approve(self.sup, pid)
        Project.objects.filter(pk=self.p1.pk).update(is_active=False)
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{pid}/request-close/',
                                   {'post_work_evaluation': 'ok'}).status_code, 409)

    def test_a_removed_member_can_no_longer_change_their_permit(self):
        pid = self.create()
        ProjectMember.objects.filter(project=self.p1, user=self.sub).delete()
        self.assertEqual(self.call(self.sub, 'patch', f'{URL}{pid}/', {'worker_count': 5}).status_code, 403)
        self.assertEqual(self.submit(pid).status_code, 403)
        self.assertEqual(self.call(self.sub, 'delete', f'{URL}{pid}/').status_code, 403)
        self.assertEqual(self.call(self.sub, 'get', f'{URL}{pid}/').status_code, 200)     # can still read it


@override_settings(STORAGES=PLAIN_STATIC)
class PermitAdminTests(Base):
    def setUp(self):
        super().setUp()
        self.officer.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='permits',
            codename__in=['view_permit', 'change_permit', 'view_permitapproval']))
        self.draft, self.pending = self.create(), self.create()
        self.submit(self.pending)

    @staticmethod
    def management_data(page):
        data = {}
        for fs in page.context['inline_admin_formsets']:
            for key, value in fs.formset.management_form.initial.items():
                data[f'{fs.formset.prefix}-{key}'] = value
        return data

    def test_pages_load_and_nothing_can_be_added_or_deleted(self):
        self.client.force_login(self.coord)
        for page in ('permit', 'permitapproval'):
            self.assertEqual(self.client.get(f'/admin/permits/{page}/').status_code, 200, page)
            self.assertEqual(self.client.get(f'/admin/permits/{page}/add/').status_code, 403, page)
        self.assertEqual(self.client.get(f'/admin/permits/permit/{self.pending}/delete/').status_code, 403)

    def test_only_document_number_is_editable_and_the_change_is_audited(self):
        self.client.force_login(self.coord)
        url = f'/admin/permits/permit/{self.pending}/change/'
        page = self.client.get(url)
        self.assertEqual(list(page.context['adminform'].form.fields), ['document_number'])
        r = self.client.post(url, {**self.management_data(page), 'document_number': 'F.02/KPT-009',
                                   'work_location': 'HACKED'})
        self.assertEqual(r.status_code, 302)
        permit = Permit.objects.get(pk=self.pending)
        self.assertEqual((permit.document_number, permit.work_location), ('F.02/KPT-009', 'Lantai 3'))
        entry = AuditLog.objects.get(resource_type='permits.permit', resource_id=self.pending, action='updated')
        self.assertIn('KPT-009', entry.remarks)
        self.assertEqual((entry.user, entry.project), (self.coord, self.p1))

    def test_cancelled_permit_and_inactive_project_are_fully_read_only(self):
        self.call(self.coord, 'post', f'{URL}{self.draft}/cancel/', {'reason': 'x'})
        self.client.force_login(self.coord)
        page = self.client.get(f'/admin/permits/permit/{self.draft}/change/')
        self.assertEqual(list(page.context['adminform'].form.fields), [])
        Project.objects.filter(pk=self.p1.pk).update(is_active=False)
        page = self.client.get(f'/admin/permits/permit/{self.pending}/change/')
        self.assertEqual(list(page.context['adminform'].form.fields), [])

    def test_officer_does_not_see_drafts(self):
        self.client.force_login(self.officer)
        changelist = self.client.get('/admin/permits/permit/')
        self.assertEqual([p.pk for p in changelist.context['cl'].result_list], [self.pending])
        self.assertEqual(self.client.get(f'/admin/permits/permit/{self.draft}/change/').status_code, 302)
