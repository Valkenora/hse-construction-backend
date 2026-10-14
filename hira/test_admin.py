from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings

from audit.models import AuditLog
from projects.models import Project
from users.models import User
from .models import HIRAActivity, HIRADocument, HIRAGroup

R = User.Role

# The real settings use a manifest-based static storage that needs `collectstatic`.
PLAIN_STATIC = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
PAGES = ('hiradocument', 'hiragroup', 'hiraactivity', 'hiraapproval')


@override_settings(STORAGES=PLAIN_STATIC)
class HIRAAdminTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='P1', location='L', client_name='C',
                                              start_date='2026-10-01')
        self.sub = User.objects.create_user('sub@t.com', 'pass12345', full_name='sub',
                                            role=R.SUBCONTRACTOR)
        self.coord = User.objects.create_superuser('coord@t.com', 'pass12345', full_name='coord')
        self.officer = User.objects.create_user('off@t.com', 'pass12345', full_name='off',
                                                role=R.HSE_OFFICER, is_staff=True)
        self.officer.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='hira',
            codename__in=['view_hiradocument', 'change_hiradocument', 'view_hiragroup',
                          'view_hiraactivity', 'view_hiraapproval']))
        self.draft = self.make_doc('Draft doc')
        self.pending = self.make_doc('Pending doc', status=HIRADocument.Status.PENDING_OFFICER)

    def make_doc(self, work_package, **kw):
        return HIRADocument.objects.create(project=self.project, work_package=work_package,
                                           work_area='WA', created_by=self.sub, **kw)

    @staticmethod
    def management_data(page):
        """Management-form fields of the (read-only) inlines, needed to POST the change form."""
        data = {}
        for fs in page.context['inline_admin_formsets']:
            for key, value in fs.formset.management_form.initial.items():
                data[f'{fs.formset.prefix}-{key}'] = value
        return data

    def test_all_four_pages_and_their_detail_views_load(self):
        group = HIRAGroup.objects.create(hira_document=self.pending, location_function='G')
        activity = HIRAActivity.objects.create(
            group=group, activity_name='A', activity_type='R', potential_hazard='x', risk_impact='y',
            initial_likelihood=2, initial_severity=2, residual_likelihood=1, residual_severity=1)
        self.client.force_login(self.coord)
        for page in PAGES:
            self.assertEqual(self.client.get(f'/admin/hira/{page}/').status_code, 200, page)
        self.assertEqual(self.client.get(f'/admin/hira/hiragroup/{group.pk}/change/').status_code, 200)
        self.assertEqual(self.client.get(f'/admin/hira/hiraactivity/{activity.pk}/change/').status_code, 200)

    def test_nothing_can_be_added_or_deleted(self):
        self.client.force_login(self.coord)
        for page in PAGES:
            self.assertEqual(self.client.get(f'/admin/hira/{page}/add/').status_code, 403, page)
        url = f'/admin/hira/hiradocument/{self.pending.pk}/delete/'
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_only_document_number_is_editable_and_the_change_is_audited(self):
        self.client.force_login(self.coord)
        url = f'/admin/hira/hiradocument/{self.pending.pk}/change/'
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(list(page.context['adminform'].form.fields), ['document_number'])

        r = self.client.post(url, {**self.management_data(page),
                                   'document_number': 'F.01/P.01/HSE/KPT-007',
                                   'work_package': 'HACKED'})
        self.assertEqual(r.status_code, 302)
        self.pending.refresh_from_db()
        self.assertEqual((self.pending.document_number, self.pending.work_package),
                         ('F.01/P.01/HSE/KPT-007', 'Pending doc'))
        entry = AuditLog.objects.get(resource_type='hira.hiradocument', resource_id=self.pending.pk,
                                     action='updated')
        self.assertIn('KPT-007', entry.remarks)
        self.assertEqual((entry.user, entry.project), (self.coord, self.project))

    def test_cancelled_document_is_fully_read_only(self):
        doc = self.make_doc('Cancelled', status=HIRADocument.Status.CANCELLED)
        self.client.force_login(self.coord)
        page = self.client.get(f'/admin/hira/hiradocument/{doc.pk}/change/')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(list(page.context['adminform'].form.fields), [])

    def test_officer_does_not_see_drafts(self):
        self.client.force_login(self.officer)
        changelist = self.client.get('/admin/hira/hiradocument/')
        self.assertEqual(changelist.status_code, 200)
        self.assertEqual([d.pk for d in changelist.context['cl'].result_list], [self.pending.pk])
        self.assertEqual(
            self.client.get(f'/admin/hira/hiradocument/{self.pending.pk}/change/').status_code, 200)
        # a draft is "not found" for the officer: Django redirects back to the index
        self.assertEqual(
            self.client.get(f'/admin/hira/hiradocument/{self.draft.pk}/change/').status_code, 302)
