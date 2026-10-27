import re
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from audit.models import AuditLog
from core.mail import EmailDeliveryError, ResendBackend
from hira.models import HIRADocument
from projects.models import ProjectMKContact
from . import mk
from . import tests as base
from .models import MKApprovalRequest, Permit, PermitApproval, PermitPhoto
from .tests import LOC, MEMORY

URL = '/api/permits/'
MK_URL = '/api/mk/'
SITE = override_settings(FRONTEND_BASE_URL='https://app.test')


@MEMORY
@SITE
class MKTests(TestCase):
    # Borrow the workflow fixtures without inheriting (and re-running) its tests.
    setUpFixtures = base.PermitWorkflowTests.setUp
    call = base.PermitWorkflowTests.call
    make_hira = base.PermitWorkflowTests.make_hira
    payload = base.PermitWorkflowTests.payload
    create = base.PermitWorkflowTests.create

    def setUp(self):
        cache.clear()
        self.setUpFixtures()
        self.public = APIClient()           # no credentials at all

    # helpers
    def to_pending_mk_with_emails(self, cat='T'):
        pid = self.create(cat)
        base.add_photo(pid)
        self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)
        mail.outbox.clear()
        for user in (self.sup, self.officer):
            self.call(user, 'post', f'{URL}{pid}/approve/', LOC)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.call(self.pm, 'post', f'{URL}{pid}/approve/', {})
        self.assertEqual((r.status_code, r.data['status']), (200, 'pending_mk'), r.data)
        return pid

    def tokens(self):
        return [re.search(r'/mk/([\w-]+)', m.body).group(1) for m in mail.outbox if '/mk/' in m.body]

    def token_from_outbox(self):
        return self.tokens()[-1]

    def ask_code(self, token):
        mail.outbox.clear()
        r = self.public.post(f'{MK_URL}{token}/send-otp/')
        self.assertEqual(r.status_code, 200, r.data)
        return re.search(r'Kode verifikasi persetujuan Izin Kerja: (\d{6})', mail.outbox[-1].body).group(1)

    def decide(self, token, name, **body):
        return self.public.post(f'{MK_URL}{token}/{name}/', body, format='json')

    def setup_pending(self, cat='T'):
        pid = self.to_pending_mk_with_emails(cat)
        return pid, self.token_from_outbox()

    # ---- reaching MK
    def test_pm_approval_emails_a_link_to_every_active_contact_only(self):
        ProjectMKContact.objects.create(project=self.p1, full_name='Bu MK', email='mk2@kons.test')
        ProjectMKContact.objects.create(project=self.p1, full_name='Off', email='off@kons.test', is_active=False)
        pid = self.to_pending_mk_with_emails()
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['mk2@kons.test', 'mk@kons.test'])
        body = mail.outbox[0].body
        self.assertIn('https://app.test/mk/', body)
        self.assertEqual(len(set(self.tokens())), 2)        # one distinct link each
        reqs = MKApprovalRequest.objects.filter(permit_id=pid)
        self.assertEqual((reqs.count(), reqs.filter(email_sent_at__isnull=False).count()), (2, 2))
        self.assertEqual(AuditLog.objects.filter(action='mk_link_issued', resource_id=pid).count(), 1)

    def test_tokens_are_never_stored_in_clear(self):
        pid, token = self.setup_pending()
        self.assertFalse(MKApprovalRequest.objects.filter(token_hash=token).exists())
        self.assertTrue(MKApprovalRequest.objects.filter(token_hash=mk.hash_token(token)).exists())
        self.assertNotIn(token, str(AuditLog.objects.filter(resource_id=pid).values()))

    def test_te_permit_cannot_be_submitted_or_pass_the_pm_without_an_mk_contact(self):
        ProjectMKContact.objects.all().delete()
        pid = self.create('T')
        base.add_photo(pid)
        r = self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)
        self.assertEqual(r.status_code, 400)
        self.assertIn('mk', r.data)
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{self.create("M")}/submit/', LOC).status_code, 400)  # no photo
        # an M permit never needs MK
        m = self.create('M')
        base.add_photo(m)
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{m}/submit/', LOC).status_code, 200)

    def test_pm_cannot_approve_if_the_contact_disappeared_meanwhile(self):
        pid = self.create('T')
        base.add_photo(pid)
        self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)
        for user in (self.sup, self.officer):
            self.call(user, 'post', f'{URL}{pid}/approve/', LOC)
        ProjectMKContact.objects.all().update(is_active=False)
        r = self.call(self.pm, 'post', f'{URL}{pid}/approve/', {})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Permit.objects.get(pk=pid).status, 'pending_pm')
        self.assertEqual(PermitApproval.objects.filter(permit_id=pid, step='project_manager').count(), 0)

    def test_a_failing_email_does_not_break_the_approval_and_can_be_resent(self):
        pid = self.create('T')
        base.add_photo(pid)
        self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC)
        for user in (self.sup, self.officer):
            self.call(user, 'post', f'{URL}{pid}/approve/', LOC)
        with mock.patch('permits.mk._send', side_effect=EmailDeliveryError('Resend refused the email (403)')):
            with self.captureOnCommitCallbacks(execute=True):
                r = self.call(self.pm, 'post', f'{URL}{pid}/approve/', {})
        self.assertEqual((r.status_code, r.data['status']), (200, 'pending_mk'))
        # the email is attempted after the response is built, so read the permit again
        row = self.call(self.officer, 'get', f'{URL}{pid}/').data['mk_requests'][0]
        self.assertEqual((row['email_sent'], row['state']), (False, 'active'))
        self.assertIn('403', row['email_error'])
        self.assertEqual(row['contact_email'], 'm***@kons.test')
        # staff re-send: the old link dies, a new one arrives
        old = MKApprovalRequest.objects.get(permit_id=pid)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.call(self.officer, 'post', f'{URL}{pid}/mk-resend/')
        self.assertEqual(r.status_code, 200, r.data)
        old.refresh_from_db()
        self.assertEqual(old.status, 'superseded')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.public.get(f'{MK_URL}{self.token_from_outbox()}/').data['state'], 'active')

    def test_only_hse_staff_can_resend_and_only_while_waiting_for_mk(self):
        pid, _ = self.setup_pending()
        for user in (self.sub, self.sup, self.pm):
            self.assertEqual(self.call(user, 'post', f'{URL}{pid}/mk-resend/').status_code, 403)
        self.assertEqual(self.call(self.coord, 'post', f'{URL}{pid}/mk-resend/').status_code, 200)
        other = self.create('R')
        self.assertEqual(self.call(self.coord, 'post', f'{URL}{other}/mk-resend/').status_code, 409)
        detail = self.call(self.officer, 'get', f'{URL}{pid}/').data
        self.assertTrue(detail['allowed_actions']['can_resend_mk_link'])
        self.assertFalse(self.call(self.sub, 'get', f'{URL}{pid}/').data['allowed_actions']['can_resend_mk_link'])

    # ---- the public page
    def test_the_page_needs_no_login_and_ignores_a_bad_authorization_header(self):
        pid, token = self.setup_pending()
        r = self.public.get(f'{MK_URL}{token}/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['state'], r.data['contact_name']), ('active', 'Pak MK'))
        self.assertEqual(r.data['permit']['id'], pid)
        self.assertIn('checklist', r.data['permit']['form'])
        stale = APIClient()
        stale.credentials(HTTP_AUTHORIZATION='Bearer not.a.token')
        self.assertEqual(stale.get(f'{MK_URL}{token}/').status_code, 200)

    def test_the_page_shows_what_mk_needs_and_nothing_private(self):
        pid, token = self.setup_pending()
        text = str(self.public.get(f'{MK_URL}{token}/').data)
        for private in ('sub@t.com', 'sup@t.com', 'storage_path', 'permits/', token, mk.hash_token(token)):
            self.assertNotIn(private, text)
        data = self.public.get(f'{MK_URL}{token}/').data['permit']
        self.assertEqual([a['step'] for a in data['approvals']], ['supervisor', 'hse_officer', 'project_manager'])
        self.assertEqual([g['step'] for g in data['geo_stamps']], ['creator', 'supervisor', 'hse_officer'])
        self.assertEqual(len(data['photos']), 0 if not PermitPhoto.objects.exists() else 1)

    def test_an_unknown_link_is_a_404_for_every_action(self):
        for path, method in (('', 'get'), ('send-otp/', 'post'), ('approve/', 'post'), ('reject/', 'post')):
            r = getattr(self.public, method)(f'{MK_URL}nonsense-token/{path}',
                                             {'otp': '123456', 'full_name': 'X Y', 'remarks': 'r'}, format='json') \
                if method == 'post' else self.public.get(f'{MK_URL}nonsense-token/{path}')
            self.assertEqual(r.status_code, 404, path)

    # ---- the OTP
    def test_the_full_approval(self):
        pid, token = self.setup_pending()
        otp = self.ask_code(token)
        self.assertEqual(mail.outbox[-1].to, ['mk@kons.test'])
        r = self.decide(token, 'approve', otp=otp, full_name='  Budi Santoso ', remarks='Setuju')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['decision'], r.data['permit_status']), ('approved', 'approved'))
        approval = PermitApproval.objects.get(permit_id=pid, step='mk')
        self.assertEqual((approval.decided_by, approval.external_name, approval.external_email, approval.mk_contact),
                         (None, 'Budi Santoso', 'mk@kons.test', self.mk))
        entry = AuditLog.objects.get(resource_id=pid, resource_type='permits.permit', action='approved',
                                     actor_label__startswith='MK:')
        self.assertIsNone(entry.user)
        self.assertIn('Budi Santoso', entry.actor_label)
        self.assertEqual(entry.project, self.p1)
        # staff see the external decider, with no user behind it
        detail = self.call(self.sub, 'get', f'{URL}{pid}/').data
        mk_row = [a for a in detail['approvals'] if a['step'] == 'mk'][0]
        self.assertEqual((mk_row['decided_by'], mk_row['decided_by_external']['name']), (None, 'Budi Santoso'))
        self.assertEqual([c['state'] for c in detail['approval_chain']], ['approved'] * 4)

    def test_rejection_needs_a_reason_and_returns_the_permit_to_the_creator(self):
        pid, token = self.setup_pending()
        otp = self.ask_code(token)
        self.assertEqual(self.decide(token, 'reject', otp=otp, full_name='Budi S').status_code, 400)
        r = self.decide(token, 'reject', otp=otp, full_name='Budi S', remarks='APD kurang')
        self.assertEqual((r.status_code, r.data['permit_status']), (200, 'rejected'))
        # the creator can fix and resubmit; the chain restarts and the old link is dead
        self.assertEqual(self.call(self.sub, 'post', f'{URL}{pid}/submit/', LOC).status_code, 200)
        self.assertEqual(Permit.objects.get(pk=pid).status, 'pending_supervisor')
        self.assertEqual(self.public.get(f'{MK_URL}{token}/').data['state'], 'decided')

    def test_deciding_needs_the_code_the_name_and_a_well_formed_body(self):
        pid, token = self.setup_pending()
        self.assertEqual(self.decide(token, 'approve').status_code, 400)
        self.assertEqual(self.decide(token, 'approve', otp='12345', full_name='Budi').status_code, 400)
        self.assertEqual(self.decide(token, 'approve', otp='abcdef', full_name='Budi').status_code, 400)
        self.assertEqual(self.decide(token, 'approve', otp='123456', full_name='B').status_code, 400)
        r = self.decide(token, 'approve', otp='123456', full_name='Budi')    # no code was requested
        self.assertEqual(r.status_code, 400)
        self.assertIn('Request a code', str(r.data))
        self.assertEqual(Permit.objects.get(pk=pid).status, 'pending_mk')

    def test_a_wrong_code_is_counted_and_five_wrong_tries_burn_the_code(self):
        pid, token = self.setup_pending()
        otp = self.ask_code(token)
        wrong = '000000' if otp != '000000' else '111111'
        for left in (4, 3, 2, 1):
            r = self.decide(token, 'approve', otp=wrong, full_name='Budi')
            self.assertEqual(r.status_code, 400)
            self.assertIn(f'{left} attempt', str(r.data))
        r = self.decide(token, 'approve', otp=wrong, full_name='Budi')
        self.assertIn('Request a new one', str(r.data))
        # the real code no longer works either: it is burnt
        r = self.decide(token, 'approve', otp=otp, full_name='Budi')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Permit.objects.get(pk=pid).status, 'pending_mk')
        self.assertEqual(AuditLog.objects.filter(action='mk_otp_failed', resource_id=pid).count(), 5)
        # a fresh code (after the cooldown) works again
        MKApprovalRequest.objects.update(otp_sent_at=timezone.now() - timedelta(minutes=5))
        fresh = self.ask_code(token)
        self.assertEqual(self.decide(token, 'approve', otp=fresh, full_name='Budi').status_code, 200)

    def test_the_code_works_once_and_expires(self):
        pid, token = self.setup_pending()
        otp = self.ask_code(token)
        MKApprovalRequest.objects.update(otp_expires_at=timezone.now() - timedelta(seconds=1))
        r = self.decide(token, 'approve', otp=otp, full_name='Budi')
        self.assertIn('expired', str(r.data))
        MKApprovalRequest.objects.update(otp_sent_at=timezone.now() - timedelta(minutes=5))
        otp = self.ask_code(token)
        self.assertEqual(self.decide(token, 'approve', otp=otp, full_name='Budi').status_code, 200)
        self.assertEqual(self.decide(token, 'approve', otp=otp, full_name='Budi').status_code, 410)  # already decided

    def test_the_stored_code_is_hashed_and_bound_to_its_request(self):
        pid, token = self.setup_pending()
        otp = self.ask_code(token)
        req = MKApprovalRequest.objects.get()
        self.assertNotIn(otp, req.otp_hash)
        self.assertEqual(req.otp_hash, mk.hash_otp(req.pk, otp))
        self.assertNotEqual(mk.hash_otp(req.pk + 1, otp), req.otp_hash)

    def test_cooldown_and_the_cap_on_codes_per_link(self):
        pid, token = self.setup_pending()
        self.ask_code(token)
        r = self.public.post(f'{MK_URL}{token}/send-otp/')
        self.assertEqual(r.status_code, 429)
        self.assertIn('wait', str(r.data))
        for _ in range(4):      # reach the cap of 5
            MKApprovalRequest.objects.update(otp_sent_at=timezone.now() - timedelta(minutes=5))
            self.ask_code(token)
        MKApprovalRequest.objects.update(otp_sent_at=timezone.now() - timedelta(minutes=5))
        r = self.public.post(f'{MK_URL}{token}/send-otp/')
        self.assertEqual(r.status_code, 429)
        self.assertIn('new link', str(r.data))
        self.assertEqual(MKApprovalRequest.objects.get().otp_sent_count, 5)

    def test_if_the_code_email_fails_nothing_is_used_up(self):
        pid, token = self.setup_pending()
        with mock.patch('permits.mk._send', side_effect=EmailDeliveryError('down')):
            r = self.public.post(f'{MK_URL}{token}/send-otp/')
        self.assertEqual(r.status_code, 503)
        req = MKApprovalRequest.objects.get()
        self.assertEqual((req.otp_sent_count, req.otp_hash), (0, ''))
        self.assertEqual(self.public.post(f'{MK_URL}{token}/send-otp/').status_code, 200)   # no cooldown penalty

    # ---- when a link stops working
    def test_an_expired_link_is_refused_but_still_readable(self):
        pid, token = self.setup_pending()
        MKApprovalRequest.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.public.get(f'{MK_URL}{token}/').data['state'], 'expired')
        self.assertEqual(self.public.post(f'{MK_URL}{token}/send-otp/').status_code, 410)
        self.assertEqual(self.decide(token, 'approve', otp='123456', full_name='Budi').status_code, 410)
        detail = self.call(self.officer, 'get', f'{URL}{pid}/').data
        self.assertEqual(detail['mk_requests'][0]['state'], 'expired')

    def test_cancelling_the_permit_or_its_hira_kills_the_link(self):
        pid, token = self.setup_pending()
        self.call(self.coord, 'post', f'{URL}{pid}/cancel/', {'reason': 'Dibatalkan'})
        self.assertEqual(self.public.get(f'{MK_URL}{token}/').data['state'], 'closed')
        self.assertEqual(self.public.post(f'{MK_URL}{token}/send-otp/').status_code, 410)

    def test_an_inactive_project_blocks_the_decision(self):
        pid, token = self.setup_pending()
        otp = self.ask_code(token)
        self.p1.is_active = False
        self.p1.save()
        self.assertEqual(self.decide(token, 'approve', otp=otp, full_name='Budi').status_code, 409)
        self.assertEqual(Permit.objects.get(pk=pid).status, 'pending_mk')

    def test_two_contacts_the_first_decision_closes_the_other_link(self):
        ProjectMKContact.objects.create(project=self.p1, full_name='Bu MK', email='mk2@kons.test')
        pid = self.to_pending_mk_with_emails()
        first, second = self.tokens()
        self.assertEqual(self.public.get(f'{MK_URL}{second}/').data['state'], 'active')
        otp = self.ask_code(first)
        self.assertEqual(self.decide(first, 'approve', otp=otp, full_name='Budi').status_code, 200)
        r = self.public.get(f'{MK_URL}{second}/')
        self.assertEqual(r.data['state'], 'superseded')
        self.assertEqual(self.public.post(f'{MK_URL}{second}/send-otp/').status_code, 410)
        self.assertEqual(PermitApproval.objects.filter(permit_id=pid, step='mk').count(), 1)

    def test_a_lost_hira_approval_stops_an_mk_approval(self):
        pid, token = self.setup_pending()
        otp = self.ask_code(token)
        HIRADocument.objects.update(status=HIRADocument.Status.DRAFT)    # simulate an inconsistent state
        r = self.decide(token, 'approve', otp=otp, full_name='Budi')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Permit.objects.get(pk=pid).status, 'pending_mk')

    def test_the_mk_cannot_be_reached_through_the_staff_endpoints(self):
        pid, token = self.setup_pending()
        for name in ('approve', 'reject'):
            r = self.call(self.pm, 'post', f'{URL}{pid}/{name}/', {'remarks': 'x'})
            self.assertEqual(r.status_code, 409)       # no staff step is pending: MK is not a user

    def test_the_database_requires_a_user_for_every_step_except_mk(self):
        from django.db import IntegrityError, transaction
        pid, _ = self.setup_pending()
        with self.assertRaises(IntegrityError), transaction.atomic():
            PermitApproval.objects.create(permit_id=pid, step='supervisor', decision='approved',
                                          decided_by=None, submission_round=9)

    def test_a_public_link_is_throttled_per_ip(self):
        pid, token = self.setup_pending()
        codes = [self.public.get(f'{MK_URL}{token}/').status_code for _ in range(62)]
        self.assertIn(429, codes)


class ResendBackendTests(TestCase):
    def message(self):
        from django.core.mail import EmailMultiAlternatives
        m = EmailMultiAlternatives('Subj', 'plain', 'K3 <noreply@mail.test>', ['a@b.test'])
        m.attach_alternative('<b>html</b>', 'text/html')
        return m

    @override_settings(RESEND_API_KEY='re_key')
    def test_request_shape(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with mock.patch('urllib.request.urlopen', return_value=response) as op:
            self.assertEqual(ResendBackend().send_messages([self.message()]), 1)
        req = op.call_args.args[0]
        import json
        self.assertEqual(req.full_url, 'https://api.resend.com/emails')
        self.assertEqual(json.loads(req.data), {'from': 'K3 <noreply@mail.test>', 'to': ['a@b.test'],
                                                'subject': 'Subj', 'text': 'plain', 'html': '<b>html</b>'})
        self.assertEqual(req.get_header('Authorization'), 'Bearer re_key')
        self.assertTrue(req.get_header('User-agent'))

    @override_settings(RESEND_API_KEY='re_key')
    def test_errors_are_readable_and_never_contain_the_key(self):
        import io
        import urllib.error
        boom = urllib.error.HTTPError('u', 403, 'x', {}, io.BytesIO(b'{"message":"The domain is not verified"}'))
        with mock.patch('urllib.request.urlopen', side_effect=boom):
            with self.assertRaises(EmailDeliveryError) as ctx:
                ResendBackend().send_messages([self.message()])
        self.assertIn('403', str(ctx.exception))
        self.assertIn('not verified', str(ctx.exception))
        self.assertNotIn('re_key', str(ctx.exception))
        with mock.patch('urllib.request.urlopen', side_effect=TimeoutError()):
            with self.assertRaises(EmailDeliveryError):
                ResendBackend().send_messages([self.message()])
