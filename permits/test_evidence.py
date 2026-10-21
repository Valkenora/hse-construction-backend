import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from audit.models import AuditLog
from core import storage as storage_module
from .models import Permit, PermitApproval, PermitGeoStamp, PermitPhoto
from . import tests as base
from .tests import LOC, MEMORY

URL = '/api/permits/'

JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 64
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
WEBP = b'RIFF\x00\x00\x00\x00WEBP' + b'\x00' * 64


def upload(name, data=JPEG, content_type='image/jpeg'):
    return SimpleUploadedFile(name, data, content_type=content_type)


@MEMORY
class EvidenceTests(TestCase):
    # Borrow the workflow fixtures (users, projects, permit factory) without inheriting its tests.
    setUpFixtures = base.PermitWorkflowTests.setUp
    call = base.PermitWorkflowTests.call
    make_hira = base.PermitWorkflowTests.make_hira
    payload = base.PermitWorkflowTests.payload
    create = base.PermitWorkflowTests.create

    def setUp(self):
        self.setUpFixtures()
        storage_module.MemoryStorage.objects.clear()
        self.pid = self.create('R')

    # helpers
    def post_photos(self, pid, stage, files, user=None):
        c = APIClient()
        c.force_authenticate(user or self.sub)
        return c.post(f'{URL}{pid}/photos/', {'stage': stage, 'photos': files}, format='multipart')

    def send(self, pid, user=None, data=None):
        return self.call(user or self.sub, 'post', f'{URL}{pid}/submit/', data if data is not None else LOC)

    # photos
    def test_upload_stores_file_row_and_audit(self):
        r = self.post_photos(self.pid, 'before', [upload('a.jpg'), upload('b.png', PNG, 'image/png')])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(r.data['photos']), 2)
        self.assertEqual(PermitPhoto.objects.filter(permit_id=self.pid, stage='before').count(), 2)
        self.assertEqual(len(storage_module.MemoryStorage.objects), 2)
        self.assertTrue(all(p['url'].startswith('https://storage.test/') for p in r.data['photos']))
        self.assertEqual(AuditLog.objects.filter(resource_type='permits.permit', resource_id=self.pid,
                                                 action='photo_added').count(), 1)

    def test_the_file_is_recognised_by_content_not_by_name_or_declared_type(self):
        ok = self.post_photos(self.pid, 'before', [upload('x.exe', WEBP, 'application/octet-stream')])
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertEqual(PermitPhoto.objects.get().content_type, 'image/webp')
        bad = self.post_photos(self.pid, 'before', [upload('x.jpg', b'%PDF-1.4 not an image', 'image/jpeg')])
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(PermitPhoto.objects.count(), 1)

    def test_size_and_count_limits(self):
        big = upload('big.jpg', JPEG + b'0' * (5 * 1024 * 1024))
        self.assertEqual(self.post_photos(self.pid, 'before', [big]).status_code, 400)
        self.assertEqual(self.post_photos(self.pid, 'before', [upload(f'{i}.jpg') for i in range(5)]).status_code, 201)
        self.assertEqual(self.post_photos(self.pid, 'before', [upload('6.jpg')]).status_code, 400)
        self.assertEqual(PermitPhoto.objects.count(), 5)

    def test_one_bad_file_uploads_nothing(self):
        r = self.post_photos(self.pid, 'before', [upload('ok.jpg'), upload('bad.jpg', b'nope' * 10)])
        self.assertEqual(r.status_code, 400)
        self.assertEqual((PermitPhoto.objects.count(), len(storage_module.MemoryStorage.objects)), (0, 0))

    def test_a_storage_failure_midway_leaves_no_rows_and_no_files(self):
        real, calls = storage_module.MemoryStorage.upload, []

        def flaky(self_, path, data, content_type):
            calls.append(path)
            if len(calls) == 2:
                raise storage_module.StorageError()
            return real(self_, path, data, content_type)

        with mock.patch.object(storage_module.MemoryStorage, 'upload', flaky):
            r = self.post_photos(self.pid, 'before', [upload('1.jpg'), upload('2.jpg')])
        self.assertEqual(r.status_code, 503)
        self.assertEqual((PermitPhoto.objects.count(), len(storage_module.MemoryStorage.objects)), (0, 0))

    def test_missing_files_and_bad_stage(self):
        self.assertEqual(self.post_photos(self.pid, 'before', []).status_code, 400)
        self.assertEqual(self.post_photos(self.pid, 'during', [upload('a.jpg')]).status_code, 400)

    def test_only_the_creator_and_only_in_the_right_status(self):
        self.assertEqual(self.post_photos(self.pid, 'before', [upload('a.jpg')], user=self.sub2).status_code, 404)   # another subcontractor cannot even see the draft
        self.assertEqual(self.post_photos(self.pid, 'after', [upload('a.jpg')]).status_code, 409)   # not approved yet
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        self.assertEqual(self.send(self.pid).status_code, 200)
        self.assertEqual(self.post_photos(self.pid, 'before', [upload('b.jpg')]).status_code, 409)  # locked

    def test_removing_a_photo_deletes_the_file_and_is_audited(self):
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        photo = PermitPhoto.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.call(self.sub, 'delete', f'{URL}{self.pid}/photos/{photo.pk}/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((PermitPhoto.objects.count(), len(storage_module.MemoryStorage.objects)), (0, 0))
        self.assertEqual(self.call(self.sub, 'delete', f'{URL}{self.pid}/photos/999/').status_code, 404)
        self.assertEqual(AuditLog.objects.filter(action='photo_removed').count(), 1)

    def test_the_detail_still_loads_when_storage_cannot_sign(self):
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        with mock.patch.object(storage_module.MemoryStorage, 'signed_urls',
                               side_effect=storage_module.StorageError()):
            r = self.call(self.sub, 'get', f'{URL}{self.pid}/')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data['photos'][0]['url'])

    def test_deleting_a_draft_removes_its_files(self):
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.call(self.sub, 'delete', f'{URL}{self.pid}/').status_code, 204)
        self.assertEqual(len(storage_module.MemoryStorage.objects), 0)

    # gates
    def test_submit_needs_a_before_photo_and_a_location(self):
        r = self.send(self.pid)
        self.assertEqual(r.status_code, 400)
        self.assertIn('photos', r.data)
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        self.assertEqual(self.send(self.pid, data={}).status_code, 400)
        partial = {k: LOC[k] for k in ('latitude', 'longitude')}
        self.assertEqual(self.send(self.pid, data=partial).status_code, 400)
        self.assertEqual(Permit.objects.get(pk=self.pid).status, 'draft')
        self.assertEqual(PermitGeoStamp.objects.count(), 0)
        self.assertEqual(self.send(self.pid).status_code, 200)
        stamp = PermitGeoStamp.objects.get()
        self.assertEqual((stamp.step, stamp.submission_round, stamp.recorded_by), ('creator', 1, self.sub))

    def test_location_values_are_range_checked_and_stored_as_given(self):
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        for bad in ({'latitude': 91}, {'longitude': -181}, {'accuracy': -1}, {'captured_at': 'yesterday'}):
            self.assertEqual(self.send(self.pid, data={**LOC, **bad}).status_code, 400, bad)
        self.assertEqual(self.send(self.pid).status_code, 200)
        stamp = PermitGeoStamp.objects.get()
        self.assertEqual((stamp.latitude, stamp.longitude, stamp.accuracy), (-6.917464, 107.619123, 12.5))

    def test_supervisor_and_officer_must_send_a_location_to_approve_but_not_to_reject(self):
        pid = self.create('M')
        self.post_photos(pid, 'before', [upload('a.jpg')])
        self.send(pid)
        approve = f'{URL}{pid}/approve/'
        self.assertEqual(self.call(self.sup, 'post', approve, {}).status_code, 400)
        self.assertEqual(self.call(self.sup, 'post', approve, {'latitude': 1, 'longitude': 1}).status_code, 400)
        self.assertEqual(Permit.objects.get(pk=pid).status, 'pending_supervisor')
        self.assertEqual(self.call(self.sup, 'post', approve, LOC).status_code, 200)
        self.assertEqual(self.call(self.officer, 'post', approve, {}).status_code, 400)
        self.assertEqual(self.call(self.officer, 'post', approve, LOC).status_code, 200)
        steps = list(PermitGeoStamp.objects.order_by('pk').values_list('step', flat=True))
        self.assertEqual(steps, ['creator', 'supervisor', 'hse_officer'])

        other = self.create('M')
        self.post_photos(other, 'before', [upload('a.jpg')])
        self.send(other)
        r = self.call(self.sup, 'post', f'{URL}{other}/reject/', {'remarks': 'Not safe'})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(PermitGeoStamp.objects.filter(permit_id=other).count(), 1)   # creator only

    def test_the_project_manager_needs_no_location(self):
        pid = self.create('T')
        self.post_photos(pid, 'before', [upload('a.jpg')])
        self.send(pid)
        self.call(self.sup, 'post', f'{URL}{pid}/approve/', LOC)
        self.call(self.officer, 'post', f'{URL}{pid}/approve/', LOC)
        r = self.call(self.pm, 'post', f'{URL}{pid}/approve/', {})
        self.assertEqual((r.status_code, r.data['status']), (200, 'pending_mk'))
        self.assertEqual(PermitGeoStamp.objects.filter(permit_id=pid).count(), 3)

    def test_a_failed_approval_leaves_no_decision_or_stamp(self):
        pid = self.create('R')
        self.post_photos(pid, 'before', [upload('a.jpg')])
        self.send(pid)
        before = (PermitApproval.objects.count(), PermitGeoStamp.objects.count())
        self.call(self.sup, 'post', f'{URL}{pid}/approve/', {})
        self.assertEqual((PermitApproval.objects.count(), PermitGeoStamp.objects.count()), before)

    def test_location_is_recorded_in_the_audit_remarks(self):
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        self.send(self.pid)
        entry = AuditLog.objects.get(resource_type='permits.permit', resource_id=self.pid, action='submitted')
        self.assertIn('-6.917464,107.619123', entry.remarks)

    def test_resubmission_after_rejection_needs_a_fresh_location_for_the_new_round(self):
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        self.send(self.pid)
        self.call(self.sup, 'post', f'{URL}{self.pid}/reject/', {'remarks': 'Fix it'})
        self.assertEqual(self.send(self.pid, data={}).status_code, 400)
        self.assertEqual(self.send(self.pid).status_code, 200)
        rounds = list(PermitGeoStamp.objects.filter(step='creator').values_list('submission_round', flat=True))
        self.assertEqual(sorted(rounds), [1, 2])

    def test_closing_needs_an_after_photo(self):
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        self.send(self.pid)
        self.call(self.sup, 'post', f'{URL}{self.pid}/approve/', LOC)
        close = f'{URL}{self.pid}/request-close/'
        r = self.call(self.sub, 'post', close, {'post_work_evaluation': 'Done'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('photos', r.data)
        self.assertEqual(self.post_photos(self.pid, 'after', [upload('z.jpg')]).status_code, 201)
        r = self.call(self.sub, 'post', close, {'post_work_evaluation': 'Done'})
        self.assertEqual((r.status_code, r.data['status']), (200, 'pending_close'))
        self.assertEqual(self.post_photos(self.pid, 'after', [upload('y.jpg')]).status_code, 409)  # locked again

    def test_a_viewer_sees_photos_and_stamps_but_never_storage_paths_it_cannot_use(self):
        self.post_photos(self.pid, 'before', [upload('a.jpg')])
        self.send(self.pid)
        r = self.call(self.sup, 'get', f'{URL}{self.pid}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data['photos']), 1)
        self.assertEqual(r.data['geo_stamps'][0]['step'], 'creator')
        self.assertNotIn('storage_path', r.data['photos'][0])


class SupabaseStorageTests(TestCase):
    """The HTTP client: request shapes, and that failures never leak upstream detail."""

    def make(self, **over):
        values = {'SUPABASE_URL': 'https://proj.supabase.co/', 'SUPABASE_SERVICE_KEY': 'secret',
                  'SUPABASE_BUCKET': 'permit-photos', **over}
        from django.test import override_settings
        with override_settings(**values):
            return storage_module.SupabaseStorage()

    def fake_response(self, body=b''):
        response = mock.MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        return response

    def test_unconfigured_storage_is_a_503_not_a_crash(self):
        with self.assertRaises(storage_module.StorageError):
            self.make(SUPABASE_SERVICE_KEY='')

    def test_upload_request_shape(self):
        s = self.make()
        with mock.patch('urllib.request.urlopen', return_value=self.fake_response(b'{}')) as op:
            s.upload('permits/1/before/a.jpg', b'bytes', 'image/jpeg')
        req = op.call_args.args[0]
        self.assertEqual(req.full_url, 'https://proj.supabase.co/storage/v1/object/permit-photos/permits/1/before/a.jpg')
        self.assertEqual((req.method, req.data), ('POST', b'bytes'))
        self.assertEqual(req.get_header('Authorization'), 'Bearer secret')
        self.assertEqual(req.get_header('Content-type'), 'image/jpeg')

    def test_signed_urls_are_built_from_the_response(self):
        s = self.make()
        body = (b'[{"path":"a.jpg","signedURL":"/object/sign/permit-photos/a.jpg?token=t","error":null},'
                b'{"path":"b.jpg","signedURL":null,"error":"Object not found"}]')
        with mock.patch('urllib.request.urlopen', return_value=self.fake_response(body)):
            urls = s.signed_urls(['a.jpg', 'b.jpg'])
        self.assertEqual(urls, {'a.jpg': 'https://proj.supabase.co/storage/v1/object/sign/permit-photos/a.jpg?token=t'})
        self.assertEqual(s.signed_urls([]), {})

    def test_upstream_errors_become_a_clean_503(self):
        import urllib.error
        s = self.make()
        boom = urllib.error.HTTPError('u', 400, 'bad', {}, io.BytesIO(b'{"secret":"internal path"}'))
        with mock.patch('urllib.request.urlopen', side_effect=boom):
            with self.assertRaises(storage_module.StorageError) as ctx:
                s.upload('a.jpg', b'x', 'image/jpeg')
        self.assertNotIn('internal', str(ctx.exception.detail))
        with mock.patch('urllib.request.urlopen', side_effect=TimeoutError()):
            with self.assertRaises(storage_module.StorageError):
                s.delete(['a.jpg'])
