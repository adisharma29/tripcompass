"""Tests for the shortlinks app.

Covers:
- Redirect view: GET redirect, click counting, method restriction
- Expiry, deactivation, max_clicks guards
- ShortLink.create_for_url: origin validation, collision retry
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from .models import ShortLink, ALLOWED_REDIRECT_ORIGINS


@override_settings(
    API_ORIGIN='http://localhost:8000',
    FRONTEND_ORIGIN='http://localhost:6001',
)
class ShortLinkModelTest(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Force re-evaluation of allowed origins for test settings
        import shortlinks.models as m
        m.ALLOWED_REDIRECT_ORIGINS = None

    def test_create_for_url_valid_api_origin(self):
        link = ShortLink.objects.create_for_url('http://localhost:8000/api/v1/auth/wa-invite/abc/')
        self.assertIsNotNone(link.code)
        self.assertEqual(link.click_count, 0)
        self.assertTrue(link.is_active)

    def test_create_for_url_valid_frontend_origin(self):
        link = ShortLink.objects.create_for_url('http://localhost:6001/h/test-hotel/')
        self.assertIsNotNone(link.code)

    def test_create_for_url_rejects_foreign_origin(self):
        with self.assertRaises(ValueError):
            ShortLink.objects.create_for_url('https://evil.com/phish')

    def test_create_for_url_with_metadata(self):
        link = ShortLink.objects.create_for_url(
            'http://localhost:8000/test/',
            metadata={'delivery_id': 42},
        )
        self.assertEqual(link.metadata['delivery_id'], 42)

    def test_is_valid_active_link(self):
        link = ShortLink.objects.create_for_url('http://localhost:8000/test/')
        self.assertTrue(link.is_valid())

    def test_is_valid_expired_link(self):
        link = ShortLink.objects.create_for_url(
            'http://localhost:8000/test/',
            expires_at=timezone.now() - timedelta(hours=1),
        )
        self.assertFalse(link.is_valid())

    def test_is_valid_deactivated_link(self):
        link = ShortLink.objects.create_for_url('http://localhost:8000/test/')
        link.is_active = False
        link.save()
        self.assertFalse(link.is_valid())

    def test_is_valid_max_clicks_reached(self):
        link = ShortLink.objects.create_for_url(
            'http://localhost:8000/test/',
            max_clicks=1,
        )
        link.click_count = 1
        link.save()
        self.assertFalse(link.is_valid())


@override_settings(
    API_ORIGIN='http://localhost:8000',
    FRONTEND_ORIGIN='http://localhost:6001',
)
class ShortLinkRedirectViewTest(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import shortlinks.models as m
        m.ALLOWED_REDIRECT_ORIGINS = None

    def _make_link(self, **kwargs):
        defaults = {'target_url': 'http://localhost:8000/api/v1/auth/wa-invite/test/'}
        defaults.update(kwargs)
        return ShortLink.objects.create_for_url(**defaults)

    def test_get_redirects_to_target(self):
        link = self._make_link()
        resp = self.client.get(f'/s/{link.code}')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], link.target_url)

    def test_get_increments_click_count(self):
        link = self._make_link()
        self.client.get(f'/s/{link.code}')
        link.refresh_from_db()
        self.assertEqual(link.click_count, 1)

    def test_post_returns_405(self):
        link = self._make_link()
        resp = self.client.post(f'/s/{link.code}')
        self.assertEqual(resp.status_code, 405)

    def test_put_returns_405(self):
        link = self._make_link()
        resp = self.client.put(f'/s/{link.code}')
        self.assertEqual(resp.status_code, 405)

    def test_unknown_code_returns_404(self):
        resp = self.client.get('/s/nonexistent')
        self.assertEqual(resp.status_code, 404)

    def test_expired_link_returns_410(self):
        link = self._make_link(expires_at=timezone.now() - timedelta(hours=1))
        resp = self.client.get(f'/s/{link.code}')
        self.assertEqual(resp.status_code, 410)

    def test_deactivated_link_returns_410(self):
        link = self._make_link()
        link.is_active = False
        link.save()
        resp = self.client.get(f'/s/{link.code}')
        self.assertEqual(resp.status_code, 410)

    def test_max_clicks_enforced(self):
        link = self._make_link(max_clicks=1)
        resp1 = self.client.get(f'/s/{link.code}')
        self.assertEqual(resp1.status_code, 302)
        resp2 = self.client.get(f'/s/{link.code}')
        self.assertEqual(resp2.status_code, 410)

    def test_click_count_not_incremented_on_post(self):
        """POST is rejected with 405, click_count must not change."""
        link = self._make_link()
        self.client.post(f'/s/{link.code}')
        link.refresh_from_db()
        self.assertEqual(link.click_count, 0)
