"""Email delivery through Resend's HTTPS API.

Render's free tier blocks SMTP ports, so mail goes out over HTTPS. Used automatically when
RESEND_API_KEY is set; otherwise Django's console backend prints emails (local development),
and tests use Django's in-memory backend. Swapping provider later means writing another
backend class and pointing EMAIL_BACKEND at it.
"""
import json
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class EmailDeliveryError(Exception):
    """Raised when the provider refuses or cannot be reached. The text is safe to store
    and show to staff: it never contains the API key."""


class ResendBackend(BaseEmailBackend):
    URL = 'https://api.resend.com/emails'
    TIMEOUT = 15   # seconds

    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            payload = {'from': message.from_email, 'to': list(message.to),
                       'subject': message.subject, 'text': message.body}
            html = next((content for content, mime in getattr(message, 'alternatives', ())
                         if mime == 'text/html'), None)
            if html:
                payload['html'] = html
            # An explicit User-Agent: some edge networks refuse Python's default one.
            request = urllib.request.Request(
                self.URL, data=json.dumps(payload).encode(), method='POST',
                headers={'Authorization': f'Bearer {settings.RESEND_API_KEY}',
                         'Content-Type': 'application/json', 'User-Agent': 'hse-backend/1.0'})
            try:
                with urllib.request.urlopen(request, timeout=self.TIMEOUT):
                    pass
            except urllib.error.HTTPError as exc:
                if not self.fail_silently:
                    detail = exc.read().decode('utf-8', 'replace')[:160].replace('\n', ' ')
                    raise EmailDeliveryError(f'Resend refused the email ({exc.code}): {detail}') from exc
                continue
            except (urllib.error.URLError, OSError) as exc:
                if not self.fail_silently:
                    raise EmailDeliveryError('Resend could not be reached.') from exc
                continue
            sent += 1
        return sent
