"""Private object storage for evidence photos.

Production: a private Supabase Storage bucket, reached over HTTPS with the service key
(Render's disk is wiped on every deploy, so photos cannot live there). Tests use the
in-memory twin. The class is chosen by settings.EVIDENCE_STORAGE.

Photos are never public: the API hands out short-lived signed URLs.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

from django.conf import settings
from django.utils.module_loading import import_string
from rest_framework.exceptions import APIException


class StorageError(APIException):
    """Raised when the bucket is unreachable or not configured. DRF answers 503."""
    status_code = 503
    default_detail = 'Photo storage is unavailable. Please try again later.'
    default_code = 'storage_unavailable'


class SupabaseStorage:
    TIMEOUT = 20   # seconds

    def __init__(self):
        self.base = (settings.SUPABASE_URL or '').rstrip('/')
        self.key = settings.SUPABASE_SERVICE_KEY
        self.bucket = settings.SUPABASE_BUCKET
        if not (self.base and self.key and self.bucket):
            raise StorageError('Photo storage is not configured on the server.')

    def _call(self, method, path, body=None, content_type='application/json'):
        request = urllib.request.Request(
            f'{self.base}/storage/v1{path}', data=body, method=method,
            headers={'Authorization': f'Bearer {self.key}', 'apikey': self.key,
                     'Content-Type': content_type})
        try:
            with urllib.request.urlopen(request, timeout=self.TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Never echo the storage response body: it can name internal paths.
            raise StorageError(f'Photo storage rejected the request ({exc.code}).') from exc
        except (urllib.error.URLError, OSError) as exc:     # includes timeouts
            raise StorageError() from exc
        return json.loads(raw) if raw else None

    def upload(self, path, data, content_type):
        self._call('POST', f'/object/{self.bucket}/{urllib.parse.quote(path)}', data, content_type)

    def signed_urls(self, paths, expires_in=3600):
        """{path: url} for every path, in one request."""
        if not paths:
            return {}
        body = json.dumps({'expiresIn': expires_in, 'paths': list(paths)}).encode()
        rows = self._call('POST', f'/object/sign/{self.bucket}', body) or []
        return {row['path']: f'{self.base}/storage/v1{row["signedURL"]}'
                for row in rows if row.get('signedURL') and not row.get('error')}

    def delete(self, paths):
        if paths:
            self._call('DELETE', f'/object/{self.bucket}',
                       json.dumps({'prefixes': list(paths)}).encode())


class MemoryStorage:
    """Test double. Same interface, nothing leaves the process."""
    objects = {}

    def upload(self, path, data, content_type):
        self.objects[path] = (data, content_type)

    def signed_urls(self, paths, expires_in=3600):
        return {p: f'https://storage.test/{p}?token={uuid.uuid4().hex[:8]}' for p in paths if p in self.objects}

    def delete(self, paths):
        for p in paths:
            self.objects.pop(p, None)


def get_storage():
    return import_string(settings.EVIDENCE_STORAGE)()
