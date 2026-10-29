from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from audit.models import AuditLog
from core import storage as storage_module
from . import tests as base
from .models import Permit, PermitAttachment, PermitPhoto
from .tests import LOC, MEMORY

URL = '/api/permits/'
PDF = b'%PDF-1.4\n%fake but well-formed enough for the header check\n'


def pdf(name='doc.pdf', data=PDF):
    return SimpleUploadedFile(name, data, content_type='application/pdf')


@MEMORY
@override_settings(FRONTEND_BASE_URL='https://app.test')
class AttachmentTests(TestCase):
    # Borrow the workflow fixtures without inheriting (and re-running) its tests.
    setUpFixtures = base.PermitWorkflowTests.setUp
    call = base.PermitWorkflowTests.call
    make_hira = base.PermitWorkflowTests.make_hira
    payload = base.PermitWorkflowTests.payload
    create = base.PermitWorkflowTests.create

    def setUp(self):
        cache.clear()
        self.setUpFixtures()
        storage_module.MemoryStorage.objects.clear()
        self.pid = self.create('R')
        # a before photo only: the two PDFs are what these tests are about
        PermitPhoto.objects.create(permit_id=self.pid, stage='before', storage_path='permits/x/before/p.jpg',
                                   content_type='image/jpeg', size=10, uploaded_by=self.sub)

    def upload(self, kind, files, pid=None, user=None):
        c = APIClient()
        c.force_authenticate(user or self.sub)
        return c.post(f'{URL}{pid or self.pid}/attachments/', {'kind': kind, 'files': files}, format='multipart')

    def submit(self, pid=None):
        return self.call(self.sub, 'post', f'{URL}{pid or self.pid}/submit/', LOC)

    # uploading
    def test_upload_stores_the_file_the_row_and_the_audit(self):
        r = self.upload('medical', [pdf('fit.pdf'), pdf('fit2.pdf')])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual([a['kind'] for a in r.data['attachments']], ['medical', 'medical'])
        self.assertTrue(all(a['url'].startswith('https://storage.test/') for a in r.data['attachments']))
        self.assertNotIn('storage_path', r.data['attachments'][0])
        self.assertEqual(len(storage_module.MemoryStorage.objects), 2)
        paths = list(storage_module.MemoryStorage.objects)
        self.assertTrue(all(p.startswith(f'permits/{self.pid}/attachments/medical/') and p.endswith('.pdf')
                            for p in paths))
        self.assertEqual(storage_module.MemoryStorage.objects[paths[0]][1], 'application/pdf')
        self.assertEqual(AuditLog.objects.filter(action='attachment_added', resource_id=self.pid).count(), 1)

    def test_only_real_pdfs_under_the_size_limit(self):
        self.assertEqual(self.upload('medical', [pdf('x.pdf', b'<html>not a pdf</html>')]).status_code, 400)
        self.assertEqual(self.upload('medical', [pdf('x.pdf', b'%PDF-' + b'0' * (3 * 1024 * 1024))]).status_code, 400)
        r = self.upload('medical', [pdf('scan.pdf', b'%PDF-' + b'0' * (3 * 1024 * 1024 - 5))])   # exactly the limit
        self.assertEqual(r.status_code, 201, r.data)
        r = self.upload('certificate', [pdf('x.jpg.pdf', b'\xff\xd8\xff' + b'0' * 50)])           # a JPEG renamed
        self.assertEqual(r.status_code, 400)
        self.assertIn('PDF', str(r.data))
        self.assertIn('Compress', str(self.upload('medical', [pdf('b.pdf', b'%PDF-' + b'0' * (4 * 1024 * 1024))]).data))

    def test_up_to_ten_per_kind_and_the_kinds_count_separately(self):
        self.assertEqual(self.upload('medical', [pdf(f'{i}.pdf') for i in range(10)]).status_code, 201)
        r = self.upload('medical', [pdf('11.pdf')])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.upload('certificate', [pdf('c.pdf')]).status_code, 201)
        self.assertEqual(PermitAttachment.objects.count(), 11)

    def test_bad_kind_and_no_file(self):
        self.assertEqual(self.upload('passport', [pdf()]).status_code, 400)
        self.assertEqual(self.upload('medical', []).status_code, 400)

    def test_one_bad_file_uploads_nothing_and_a_storage_failure_leaves_no_trace(self):
        r = self.upload('medical', [pdf('ok.pdf'), pdf('bad.pdf', b'nope' * 10)])
        self.assertEqual(r.status_code, 400)
        self.assertEqual((PermitAttachment.objects.count(), len(storage_module.MemoryStorage.objects)), (0, 0))
        real, calls = storage_module.MemoryStorage.upload, []

        def flaky(self_, path, data, content_type):
            calls.append(path)
            if len(calls) == 2:
                raise storage_module.StorageError()
            return real(self_, path, data, content_type)

        with mock.patch.object(storage_module.MemoryStorage, 'upload', flaky):
            r = self.upload('medical', [pdf('1.pdf'), pdf('2.pdf')])
        self.assertEqual(r.status_code, 503)
        self.assertEqual((PermitAttachment.objects.count(), len(storage_module.MemoryStorage.objects)), (0, 0))

    def test_only_the_creator_and_only_while_editable(self):
        self.assertEqual(self.upload('medical', [pdf()], user=self.sub2).status_code, 404)   # cannot even see the draft
        self.assertEqual(self.upload('medical', [pdf()], user=self.sup).status_code, 403)
        self.upload('medical', [pdf()])
        self.upload('certificate', [pdf()])
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.upload('medical', [pdf()]).status_code, 409)       # locked while in the chain
        att = PermitAttachment.objects.first()
        self.assertEqual(self.call(self.sub, 'delete', f'{URL}{self.pid}/attachments/{att.pk}/').status_code, 409)
        # rejected: editable again, files kept
        self.call(self.sup, 'post', f'{URL}{self.pid}/reject/', {'remarks': 'Lengkapi'})
        self.assertEqual(PermitAttachment.objects.count(), 2)
        self.assertEqual(self.upload('medical', [pdf('new.pdf')]).status_code, 201)

    def test_removing_one_deletes_the_file_and_is_audited(self):
        self.upload('medical', [pdf()])
        att = PermitAttachment.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.call(self.sub, 'delete', f'{URL}{self.pid}/attachments/{att.pk}/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((PermitAttachment.objects.count(), len(storage_module.MemoryStorage.objects)), (0, 0))
        self.assertEqual(self.call(self.sub, 'delete', f'{URL}{self.pid}/attachments/999/').status_code, 404)
        self.assertEqual(AuditLog.objects.filter(action='attachment_removed').count(), 1)

    def test_deleting_a_draft_removes_its_pdfs_too(self):
        self.upload('medical', [pdf()])
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.call(self.sub, 'delete', f'{URL}{self.pid}/').status_code, 204)
        self.assertEqual(len(storage_module.MemoryStorage.objects), 0)

    # the submit gate
    def test_submit_needs_at_least_one_of_each_kind(self):
        r = self.submit()
        self.assertEqual(r.status_code, 400)
        self.assertIn('medical', r.data['attachments'])
        self.assertIn('certificate', r.data['attachments'])
        self.upload('medical', [pdf()])
        r = self.submit()
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('medical', r.data['attachments'])
        self.assertIn('certificate', r.data['attachments'])
        self.assertEqual(Permit.objects.get(pk=self.pid).status, 'draft')
        self.upload('certificate', [pdf()])
        self.assertEqual(self.submit().status_code, 200)

    def test_the_number_of_workers_does_not_change_the_rule(self):
        pid = self.create('R', worker_count=30)
        PermitPhoto.objects.create(permit_id=pid, stage='before', storage_path='permits/y/before/p.jpg',
                                   content_type='image/jpeg', size=10, uploaded_by=self.sub)
        self.upload('medical', [pdf()], pid=pid)
        self.upload('certificate', [pdf()], pid=pid)
        self.assertEqual(self.submit(pid).status_code, 200)

    def test_every_error_is_reported_together(self):
        pid = self.create('R')                      # no photo, no pdfs
        r = self.submit(pid)
        self.assertEqual(r.status_code, 400)
        self.assertTrue({'photos', 'attachments'} <= set(r.data))

    # who sees them
    def test_approvers_and_the_creator_see_them_in_the_detail(self):
        self.upload('medical', [pdf('fit.pdf')])
        self.upload('certificate', [pdf('sertifikat.pdf')])
        self.submit()
        for user in (self.sub, self.sup, self.officer, self.coord):
            rows = self.call(user, 'get', f'{URL}{self.pid}/').data['attachments']
            self.assertEqual(sorted(a['kind'] for a in rows), ['certificate', 'medical'], user.role)
        actions = self.call(self.sub, 'get', f'{URL}{self.pid}/').data['allowed_actions']
        self.assertFalse(actions['can_manage_attachments'])      # locked once submitted

    def test_the_draft_owner_can_manage_attachments(self):
        self.assertTrue(self.call(self.sub, 'get', f'{URL}{self.pid}/').data['allowed_actions']['can_manage_attachments'])

    def test_the_mk_page_shows_them_without_any_storage_path(self):
        import re
        pid = self.create('T')
        PermitPhoto.objects.create(permit_id=pid, stage='before', storage_path='permits/z/before/p.jpg',
                                   content_type='image/jpeg', size=10, uploaded_by=self.sub)
        self.upload('medical', [pdf('fit.pdf')], pid=pid)
        self.upload('certificate', [pdf('sertifikat.pdf')], pid=pid)
        self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)
        mail.outbox.clear()
        for user in (self.sup, self.officer):
            self.call(user, 'post', f'{URL}{pid}/approve/', LOC)
        with self.captureOnCommitCallbacks(execute=True):
            self.call(self.pm, 'post', f'{URL}{pid}/approve/', {})
        token = re.search(r'/mk/([\w-]+)', mail.outbox[-1].body).group(1)
        data = APIClient().get(f'/api/mk/{token}/').data['permit']
        self.assertEqual(sorted(a['kind'] for a in data['attachments']), ['certificate', 'medical'])
        self.assertTrue(all(a['url'].startswith('https://storage.test/') for a in data['attachments']))
        self.assertNotIn('storage_path', str(data))
