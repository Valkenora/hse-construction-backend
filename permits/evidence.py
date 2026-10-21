"""Rules for evidence photos: what is accepted, and how a file is recognised."""
from rest_framework.exceptions import ValidationError

MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTOS_PER_STAGE = 5


def sniff(head):
    """(content_type, extension) from the file's first bytes, or None.
    The client-declared type and file name are not trusted."""
    if head[:3] == b'\xff\xd8\xff':
        return 'image/jpeg', 'jpg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png', 'png'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp', 'webp'
    return None


def inspect(upload):
    """Validate one uploaded file. Returns (bytes, content_type, extension, clean_name)."""
    name = (upload.name or 'photo').rsplit('/', 1)[-1].rsplit('\\', 1)[-1][:200]
    if upload.size > MAX_PHOTO_BYTES:
        raise ValidationError({'photos': f'{name}: larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB.'})
    data = upload.read()
    kind = sniff(data[:12])
    if kind is None:
        raise ValidationError({'photos': f'{name}: only JPEG, PNG or WebP images are accepted.'})
    return data, kind[0], kind[1], name
