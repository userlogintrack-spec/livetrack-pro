"""Verify the multi-website selector — owners can pick a subset of their
websites and dashboard filters scope to just those, never to other orgs'
websites even if a stale session ID points there."""
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from tracker.chat.models import AgentProfile
from tracker.core.models import Organization, Website
from tracker.dashboard.views import _read_selected_ids, get_website_filter


def _make_org(name, owner_username):
    user = User.objects.create_user(
        username=owner_username,
        password='x-secret-pw-1',  # noqa: S106 — test fixture
        email=f'{owner_username}@test.local',
    )
    org = Organization.objects.create(
        name=name, slug=name.lower(),
        widget_key=f'wk-{owner_username}', owner=user,
    )
    AgentProfile.objects.create(user=user, organization=org, role='owner')
    return org, user


class MultiWebsiteFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org_a, cls.user_a = _make_org('AcmeCorp', 'alice')
        cls.org_b, cls.user_b = _make_org('BetaInc', 'bob')

        cls.w1 = Website.objects.create(organization=cls.org_a, name='Acme One',
                                        domain='one.acme.test', tracking_key='tk-acme-1')
        cls.w2 = Website.objects.create(organization=cls.org_a, name='Acme Two',
                                        domain='two.acme.test', tracking_key='tk-acme-2')
        cls.w3 = Website.objects.create(organization=cls.org_a, name='Acme Three',
                                        domain='three.acme.test', tracking_key='tk-acme-3')
        # Beta-owned website — alice must never be able to filter into it
        cls.wb = Website.objects.create(organization=cls.org_b, name='Beta Site',
                                        domain='beta.test', tracking_key='tk-beta-1')

    def setUp(self):
        self.factory = RequestFactory()

    def _request_for(self, user, session_data=None):
        request = self.factory.get('/')
        request.user = user
        request.session = session_data or {}
        return request

    def test_no_selection_means_all(self):
        r = self._request_for(self.user_a, {})
        self.assertEqual(get_website_filter(r, self.org_a), {})

    def test_single_int_legacy_session_still_works(self):
        r = self._request_for(self.user_a, {'selected_website_id': self.w1.id})
        self.assertEqual(get_website_filter(r, self.org_a), {'website_id': self.w1.id})

    def test_multi_select_returns_in_filter(self):
        r = self._request_for(
            self.user_a,
            {'selected_website_ids': [self.w1.id, self.w2.id]},
        )
        f = get_website_filter(r, self.org_a)
        self.assertIn('website_id__in', f)
        self.assertEqual(sorted(f['website_id__in']), sorted([self.w1.id, self.w2.id]))

    def test_single_in_list_collapses_to_eq(self):
        # 1-item list should produce {'website_id': X}, not {'website_id__in': [X]}
        r = self._request_for(self.user_a, {'selected_website_ids': [self.w3.id]})
        self.assertEqual(get_website_filter(r, self.org_a), {'website_id': self.w3.id})

    def test_other_orgs_website_id_is_dropped_silently(self):
        # Stale session pointing at Beta's website while alice is logged in —
        # filter must NOT leak it into alice's queries.
        r = self._request_for(self.user_a, {'selected_website_ids': [self.wb.id]})
        f = get_website_filter(r, self.org_a)
        # No valid IDs left after the org-scope check → returns {} (all-of-mine)
        self.assertEqual(f, {})

    def test_mixed_valid_and_other_org_keeps_only_valid(self):
        r = self._request_for(
            self.user_a,
            {'selected_website_ids': [self.w1.id, self.wb.id]},
        )
        # Only w1 survives the org-scope filter
        self.assertEqual(get_website_filter(r, self.org_a), {'website_id': self.w1.id})

    def test_read_selected_ids_handles_garbage(self):
        r = self._request_for(self.user_a, {'selected_website_ids': ['abc', None, 7]})
        self.assertEqual(_read_selected_ids(r), [7])
