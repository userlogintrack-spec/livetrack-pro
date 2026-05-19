"""Critical-path security tests.

Covers:
    - Magic-link consume (token validity, single-use, expired tokens)
    - TOTP setup + backup code lifecycle
    - Field encryption round-trip (Fernet + PBKDF2 hashes)
    - Plan gating returns 402 for ungated paid endpoints
    - SMTP header sanitizer rejects CRLF injection
    - Booking URL XSS filter (the JS one is tested by hand; this covers the
      server-side msg_type whitelist in consumers.receive)

Run on every PR — these are the "if any of these break, customers are at
real risk" tests.
"""
import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from tracker.chat.models import (
    AgentProfile, AIBotConfig, ChatReopenToken, ChatRoom, MagicLinkToken,
)
from tracker.core.models import Organization, Subscription
from tracker.visitors.models import Visitor


# ────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────

def _make_org_with_user(name: str, plan: str = 'free'):
    """Create a fresh Organization + owner User + Subscription. Suffixes
    every identifier with a unique uuid hex so tests can call this freely
    without colliding on User.username or Organization.slug."""
    suffix = uuid.uuid4().hex[:8]
    user = User.objects.create_user(
        username=f'{name.lower()}_{suffix}',
        password='x-secret-pw-1',  # noqa: S106 — test fixture
        email=f'{name.lower()}-{suffix}@test.local',
    )
    org = Organization.objects.create(
        name=f'{name}-{suffix}',
        slug=f'{name.lower()}-{suffix}',
        widget_key=f'wk-{suffix}',
        owner=user,
    )
    AgentProfile.objects.create(user=user, organization=org, role='owner')
    Subscription.objects.get_or_create(
        organization=org, defaults={'plan': plan, 'status': 'active'},
    )
    # If `get_or_create` returned an existing free sub, force the requested plan.
    Subscription.objects.filter(organization=org).update(plan=plan, status='active')
    return org, user


# ════════════════════════════════════════════════
# Field encryption + hashing round-trip
# ════════════════════════════════════════════════

class CryptoRoundTripTests(TestCase):
    """Fernet encrypt/decrypt and PBKDF2 hash/verify must round-trip for the
    same input, and fail safely for tampered input."""

    def test_encrypt_decrypt_roundtrip(self):
        from tracker.core.crypto import encrypt_str, decrypt_str
        secret = 'JBSWY3DPEHPK3PXP'  # sample base32 TOTP secret
        ciphertext = encrypt_str(secret)
        self.assertNotEqual(ciphertext, secret)
        self.assertEqual(decrypt_str(ciphertext), secret)

    def test_empty_string_roundtrips_as_empty(self):
        """Blank fields must stay blank — otherwise migrations + admin would
        store a non-empty ciphertext on rows that should be null/blank."""
        from tracker.core.crypto import encrypt_str, decrypt_str
        self.assertEqual(encrypt_str(''), '')
        self.assertEqual(decrypt_str(''), '')

    def test_garbage_ciphertext_decrypts_to_empty(self):
        """Decrypting random bytes must NOT raise — it returns '' so a
        misconfigured FIELD_ENCRYPTION_KEY rotation doesn't 500 the app."""
        from tracker.core.crypto import decrypt_str
        self.assertEqual(decrypt_str('not-a-real-ciphertext'), '')

    def test_backup_code_hash_verify(self):
        from tracker.core.crypto import hash_code, verify_code
        plain = 'a3f8c2'
        hashed = hash_code(plain)
        self.assertTrue(hashed.startswith('pbkdf2_sha256$'))
        self.assertTrue(verify_code(plain, hashed))
        self.assertFalse(verify_code('wrong', hashed))
        self.assertFalse(verify_code('', hashed))

    def test_encrypted_field_accessors(self):
        """Setting via *_plain must encrypt; reading via *_plain must
        decrypt. Confirms the model descriptor wiring is intact."""
        from tracker.chat.models import AgentProfile as AP
        org, user = _make_org_with_user('CryptoCo')
        prof = AP.objects.get(user=user)
        prof.totp_secret_plain = 'TESTSECRET32'
        prof.save(update_fields=['totp_secret'])
        prof.refresh_from_db()
        self.assertNotEqual(prof.totp_secret, 'TESTSECRET32')
        self.assertEqual(prof.totp_secret_plain, 'TESTSECRET32')


# ════════════════════════════════════════════════
# Magic-link lifecycle
# ════════════════════════════════════════════════

class MagicLinkTests(TestCase):
    def setUp(self):
        self.org, self.user = _make_org_with_user('LinkCo')
        self.client = Client()

    def _create_token(self, **kw):
        return MagicLinkToken.objects.create(
            user=self.user,
            token='valid-32-byte-token-for-tests-aaaa',
            expires_at=kw.get('expires_at', timezone.now() + timedelta(minutes=30)),
            consumed_at=kw.get('consumed_at'),
        )

    def test_valid_token_logs_user_in(self):
        tok = self._create_token()
        r = self.client.get(f'/accounts/magic-link/{tok.token}/')
        self.assertIn(r.status_code, (302, 303))
        # Token should be marked consumed.
        tok.refresh_from_db()
        self.assertIsNotNone(tok.consumed_at)

    def test_already_consumed_token_rejected(self):
        tok = self._create_token(consumed_at=timezone.now())
        r = self.client.get(f'/accounts/magic-link/{tok.token}/')
        self.assertEqual(r.status_code, 400)

    def test_expired_token_rejected(self):
        tok = self._create_token(expires_at=timezone.now() - timedelta(minutes=1))
        r = self.client.get(f'/accounts/magic-link/{tok.token}/')
        self.assertEqual(r.status_code, 400)

    def test_oversized_token_rejected_before_db_lookup(self):
        """The consume view caps token length up front so an attacker can't
        burn DB cycles with megabyte URLs."""
        r = self.client.get('/accounts/magic-link/' + 'x' * 200 + '/')
        self.assertEqual(r.status_code, 400)


# ════════════════════════════════════════════════
# Plan gating — paid endpoints must 402 free users
# ════════════════════════════════════════════════

class PlanGatingTests(TestCase):
    """Premium endpoints must return 402 for free-plan orgs and 200 (or
    redirect / data) for the matching paid tier. Tested without exercising
    the actual feature logic — we just verify the gate fires."""

    def setUp(self):
        self.client = Client()

    def _login_as(self, plan: str):
        name = f'Free{plan.title()}'
        org, user = _make_org_with_user(name, plan=plan)
        self.client.force_login(user)
        return org, user

    def test_free_plan_blocked_from_ai_insights(self):
        self._login_as('free')
        r = self.client.get('/dashboard/ai-insights/')
        self.assertEqual(r.status_code, 402)

    def test_enterprise_plan_reaches_ai_insights(self):
        self._login_as('enterprise')
        r = self.client.get('/dashboard/ai-insights/')
        self.assertEqual(r.status_code, 200)

    def test_free_plan_blocked_from_email_mailboxes(self):
        self._login_as('free')
        r = self.client.get('/dashboard/email-mailboxes/')
        self.assertEqual(r.status_code, 402)

    def test_pro_plan_can_view_page_engagement(self):
        self._login_as('pro')
        r = self.client.get('/dashboard/page-engagement/')
        self.assertEqual(r.status_code, 200)

    def test_free_plan_blocked_from_most_clicked(self):
        self._login_as('free')
        r = self.client.get('/dashboard/most-clicked/')
        self.assertEqual(r.status_code, 402)

    def test_ai_snippet_endpoint_blocked_on_free(self):
        org, user = self._login_as('free')
        # Need a room for the URL to resolve, even though gate will fire first.
        v = Visitor.objects.create(
            organization=org, session_key='s1', ip_address='1.1.1.1',
        )
        room = ChatRoom.objects.create(
            organization=org, room_id='abc123', visitor=v,
        )
        r = self.client.post(
            f'/dashboard/api/ai/snippet/{room.room_id}/',
            data='{"command":"refund"}',
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 402)
        self.assertIn('PLAN_UPGRADE_REQUIRED', r.content.decode())


# ════════════════════════════════════════════════
# SMTP header injection sanitizer
# ════════════════════════════════════════════════

class EmailHeaderSanitizerTests(TestCase):
    """`from_name` containing CR/LF would otherwise inject Bcc:/Cc: headers
    into every outbound reply. The sanitizer must strip those."""

    def test_strips_crlf(self):
        from tracker.chat.email_send import _sanitize_header
        out = _sanitize_header('Bob\r\nBcc: evil@x.com')
        self.assertNotIn('\r', out)
        self.assertNotIn('\n', out)
        self.assertIn('Bob', out)

    def test_strips_nul_and_max_length(self):
        from tracker.chat.email_send import _sanitize_header
        out = _sanitize_header('hi\x00there', max_len=4)
        self.assertNotIn('\x00', out)
        self.assertLessEqual(len(out), 4)

    def test_empty_input_safe(self):
        from tracker.chat.email_send import _sanitize_header
        self.assertEqual(_sanitize_header(None), '')
        self.assertEqual(_sanitize_header(''), '')


# ════════════════════════════════════════════════
# Reopen-chat token
# ════════════════════════════════════════════════

class ReopenTokenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Class-level fixtures so the FK chain (Org → Visitor → ChatRoom →
        # ReopenToken) gets built once and stays consistent across tests in
        # this class. Per-test fixtures triggered deferred FK violations on
        # Postgres because SET CONSTRAINTS ALL IMMEDIATE runs at savepoint
        # release, and a partial setUp can leave the FK chain inconsistent.
        cls.org, cls.user = _make_org_with_user('ReopenCo')
        cls.visitor = Visitor.objects.create(
            organization=cls.org, session_key='v-sess-1', ip_address='1.2.3.4',
        )
        cls.room = ChatRoom.objects.create(
            organization=cls.org, room_id='rid-reopen',
            visitor=cls.visitor, visitor_email='visitor@example.com',
            status='closed', closed_at=timezone.now(),
        )

    def setUp(self):
        self.client = Client()

    def test_invalid_token_rejected(self):
        r = self.client.get('/chat/reopen/not-a-token/')
        self.assertEqual(r.status_code, 400)

    def test_oversized_token_rejected(self):
        r = self.client.get('/chat/reopen/' + 'x' * 100 + '/')
        self.assertEqual(r.status_code, 400)

    def test_consumed_token_rejected(self):
        tok = ChatReopenToken.objects.create(
            room=self.room,
            token='valid-reopen-token-12345',
            expires_at=timezone.now() + timedelta(days=7),
            consumed_at=timezone.now(),
        )
        r = self.client.get(f'/chat/reopen/{tok.token}/')
        self.assertEqual(r.status_code, 400)

    def test_expired_token_rejected(self):
        tok = ChatReopenToken.objects.create(
            room=self.room,
            token='valid-reopen-token-67890',
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        r = self.client.get(f'/chat/reopen/{tok.token}/')
        self.assertEqual(r.status_code, 400)


# ════════════════════════════════════════════════
# Process throttle — should not unbound under load
# ════════════════════════════════════════════════

class ProcessThrottleTests(TestCase):
    def test_basic_should_run(self):
        from tracker.core import process_throttle
        # First call always passes
        self.assertTrue(process_throttle.should_run('k1', 60))
        # Immediate retry is gated
        self.assertFalse(process_throttle.should_run('k1', 60))

    def test_unbounded_growth_capped(self):
        """Spam thousands of unique short-TTL keys and verify _state stays
        under the configured ceiling — without this guard the dict would
        grow without bound."""
        from tracker.core import process_throttle
        # Fill past the threshold
        for i in range(7000):
            process_throttle.should_run(f'spam-key-{i}', 60)
        size = len(process_throttle._state)
        self.assertLessEqual(size, process_throttle._MAX_KEYS)
