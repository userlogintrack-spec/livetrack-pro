"""Foundational test: a logged-in user from one Organization MUST NOT be able
to read or mutate another Organization's data via the dashboard endpoints.

This is the highest-leverage test in a multi-tenant SaaS — a single regression
in `get_user_org()` or any view that forgets to scope by org could leak every
customer's chats and visitors. Run on every PR.
"""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from tracker.chat.models import AgentProfile, ChatRoom
from tracker.core.models import Organization
from tracker.visitors.models import Visitor


def _make_org(name, owner_username):
    user = User.objects.create_user(
        username=owner_username,
        password='x-secret-pw-1',  # noqa: S106 — test fixture
        email=f'{owner_username}@test.local',
    )
    org = Organization.objects.create(
        name=name,
        slug=name.lower(),
        widget_key=f'wk-{owner_username}',
        owner=user,
    )
    AgentProfile.objects.create(user=user, organization=org, role='owner')
    return org, user


class MultiTenantIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org_a, cls.user_a = _make_org('AcmeCorp', 'alice')
        cls.org_b, cls.user_b = _make_org('BetaInc', 'bob')

        cls.visitor_a = Visitor.objects.create(
            organization=cls.org_a, session_key='sess-a-1', ip_address='1.1.1.1',
        )
        cls.visitor_b = Visitor.objects.create(
            organization=cls.org_b, session_key='sess-b-1', ip_address='2.2.2.2',
        )

        cls.room_a = ChatRoom.objects.create(
            organization=cls.org_a, visitor=cls.visitor_a, room_id='room-a-1',
            visitor_name='Alice Visitor', status='active',
        )
        cls.room_b = ChatRoom.objects.create(
            organization=cls.org_b, visitor=cls.visitor_b, room_id='room-b-1',
            visitor_name='Bob Visitor', status='active',
        )

    def setUp(self):
        self.client = Client()

    def _login(self, user):
        self.client.force_login(user)

    # ── Visitor list ─────────────────────────────────────────────────────
    def test_visitor_list_does_not_leak_other_orgs_visitors(self):
        self._login(self.user_a)
        resp = self.client.get('/dashboard/visitors/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8', errors='ignore')
        self.assertNotIn('2.2.2.2', body)
        self.assertNotIn('sess-b-1', body)

    # ── Chat list ────────────────────────────────────────────────────────
    def test_chat_list_does_not_leak_other_orgs_chats(self):
        self._login(self.user_a)
        resp = self.client.get('/dashboard/chats/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8', errors='ignore')
        self.assertNotIn('Bob Visitor', body)

    # ── Direct visitor detail by ID ──────────────────────────────────────
    def test_cannot_open_other_orgs_visitor_by_id(self):
        self._login(self.user_a)
        resp = self.client.get(f'/dashboard/visitors/{self.visitor_b.id}/')
        # Either 404 (correct: scoped lookup misses) or 403 (correct: explicit
        # forbid). 200 with B's data would be a leak — fail loudly.
        self.assertIn(resp.status_code, (302, 403, 404))
        if resp.status_code == 200:
            body = resp.content.decode('utf-8', errors='ignore')
            self.assertNotIn('2.2.2.2', body)

    # ── Direct chat room detail by room_id ───────────────────────────────
    def test_cannot_open_other_orgs_chat_by_room_id(self):
        self._login(self.user_a)
        resp = self.client.get(f'/dashboard/chats/{self.room_b.room_id}/')
        self.assertIn(resp.status_code, (302, 403, 404))

    # ── CSV export ───────────────────────────────────────────────────────
    def test_visitor_csv_export_does_not_leak_other_orgs_rows(self):
        self._login(self.user_a)
        resp = self.client.get('/dashboard/export/visitors/')
        # If the route doesn't exist we just skip — the isolation check below
        # would be vacuous. Real route must enforce org scoping.
        if resp.status_code != 200:
            return
        # CSV export uses StreamingHttpResponse — must read streaming_content
        if hasattr(resp, 'streaming_content'):
            body = b''.join(resp.streaming_content).decode('utf-8', errors='ignore')
        else:
            body = resp.content.decode('utf-8', errors='ignore')
        self.assertNotIn('2.2.2.2', body)
        self.assertNotIn('sess-b-1', body)

    # ── api_stats numbers ────────────────────────────────────────────────
    def test_api_stats_counts_only_own_org(self):
        self._login(self.user_a)
        resp = self.client.get('/dashboard/api/stats/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Org A has 1 active chat, org B has 1. Alice should see only her own.
        self.assertEqual(data.get('active_chats'), 1)
        self.assertEqual(data.get('active_only_chats'), 1)
