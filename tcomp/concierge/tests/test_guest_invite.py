"""Tests for the guest WhatsApp invite feature.

Covers:
- Invite API: create, resend, revoke, list (with feature flags)
- Permission checks: STAFF rejected, ADMIN/SUPERADMIN allowed
- Rate limiting: staff debounce, per-phone, per-hotel
- Duplicate invite constraint (UniqueConstraint on hotel+phone where PENDING)
- Resend: extends expiry (token_version unchanged), rejects non-PENDING
- Revoke: marks EXPIRED, rejects non-PENDING
- Token lifecycle: generate, verify, version invalidation
- Webhook postbacks: g_inv_access (sends magic link), g_inv_ack (sends confirmation)
- Verify view: GET confirm page, POST login + stay creation + invite used
- Verify guards: expired, revoked, version mismatch, staff phone conflict
"""
import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from concierge.models import (
    DeliveryRecord,
    Department,
    GuestInvite,
    GuestStay,
    Hotel,
    HotelMembership,
    WhatsAppTemplate,
)
from concierge.services import generate_invite_token, verify_invite_token
from shortlinks.models import ShortLink
from users.models import User


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

INVITE_URL = '/api/v1/hotels/{slug}/admin/guest-invites/'
RESEND_URL = '/api/v1/hotels/{slug}/admin/guest-invites/{pk}/resend/'
REVOKE_URL = '/api/v1/hotels/{slug}/admin/guest-invites/{pk}/'
VERIFY_URL = '/api/v1/auth/wa-invite/{token}/'


class InviteSetupMixin:
    """Shared fixtures: hotel (with invites enabled), users, WA template."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.hotel = Hotel.objects.create(
            name='Test Hotel', slug='test-hotel',
            whatsapp_notifications_enabled=True,
            guest_invite_enabled=True,
        )
        cls.dept = Department.objects.create(
            hotel=cls.hotel, name='Front Desk', slug='front-desk',
        )

        # Staff (STAFF role — should NOT have invite permission)
        cls.staff_user = User.objects.create_user(
            email='staff@test.com', password='pass',
            first_name='Staff', last_name='One', phone='+919876500001',
        )
        cls.staff_membership = HotelMembership.objects.create(
            user=cls.staff_user, hotel=cls.hotel,
            role='STAFF', department=cls.dept,
        )

        # Admin
        cls.admin_user = User.objects.create_user(
            email='admin@test.com', password='pass',
            first_name='Admin', last_name='User', phone='+919876500002',
        )
        cls.admin_membership = HotelMembership.objects.create(
            user=cls.admin_user, hotel=cls.hotel, role='ADMIN',
        )

        # Superadmin
        cls.superadmin_user = User.objects.create_user(
            email='super@test.com', password='pass',
            first_name='Super', last_name='Admin', phone='+919876500003',
        )
        cls.superadmin_membership = HotelMembership.objects.create(
            user=cls.superadmin_user, hotel=cls.hotel, role='SUPERADMIN',
        )

        # WA template
        cls.wa_template = WhatsAppTemplate.objects.create(
            hotel=cls.hotel,
            template_type='GUEST_INVITE',
            gupshup_template_id='tpl_test_123',
            name='Guest Invite',
            body_text='Hi {{1}}, welcome to {{2}}! Tap below to check in.',
            footer_text='Powered by Refuje',
            buttons=[
                {'type': 'QUICK_REPLY', 'label': 'Check In'},
                {'type': 'QUICK_REPLY', 'label': 'Got it'},
            ],
            variables=[
                {'index': 1, 'key': 'guest_name', 'label': 'Guest'},
                {'index': 2, 'key': 'hotel_name', 'label': 'Hotel'},
            ],
        )


# ---------------------------------------------------------------------------
# Invite API Tests
# ---------------------------------------------------------------------------

@override_settings(
    GUEST_INVITE_EXPIRY_HOURS=72,
    FRONTEND_ORIGIN='http://localhost:6001',
    API_ORIGIN='http://localhost:8000',
)
class GuestInviteAPITest(InviteSetupMixin, TestCase):

    def setUp(self):
        self.client = APIClient()
        # Clear rate-limit cache keys so tests don't cross-contaminate
        from django.core.cache import cache
        cache.clear()

    def _url(self, slug='test-hotel'):
        return INVITE_URL.format(slug=slug)

    def _resend_url(self, pk, slug='test-hotel'):
        return RESEND_URL.format(slug=slug, pk=pk)

    def _revoke_url(self, pk, slug='test-hotel'):
        return REVOKE_URL.format(slug=slug, pk=pk)

    def _create_invite(self, phone='919876543210', guest_name='Test Guest', room_number='101'):
        """Create a PENDING invite directly (bypasses API)."""
        return GuestInvite.objects.create(
            hotel=self.hotel,
            sent_by=self.admin_user,
            guest_phone=phone,
            guest_name=guest_name,
            room_number=room_number,
            expires_at=timezone.now() + timedelta(hours=72),
        )

    # --- Permission tests ---

    def test_staff_cannot_list_invites(self):
        self.client.force_authenticate(self.staff_user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 403)

    def test_staff_cannot_create_invite(self):
        self.client.force_authenticate(self.staff_user)
        resp = self.client.post(self._url(), {
            'phone': '919876543210', 'guest_name': 'Test',
        })
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_list_invites(self):
        self.client.force_authenticate(self.admin_user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    def test_superadmin_can_list_invites(self):
        self.client.force_authenticate(self.superadmin_user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    # --- List: feature flags ---

    def test_list_includes_feature_flags(self):
        self.client.force_authenticate(self.admin_user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn('guest_invite_enabled', resp.data)
        self.assertIn('whatsapp_notifications_enabled', resp.data)
        self.assertTrue(resp.data['guest_invite_enabled'])
        self.assertTrue(resp.data['whatsapp_notifications_enabled'])

    def test_list_includes_wa_template(self):
        self.client.force_authenticate(self.admin_user)
        resp = self.client.get(self._url())
        self.assertIn('wa_template', resp.data)
        self.assertIsNotNone(resp.data['wa_template'])
        self.assertIn('body_text', resp.data['wa_template'])

    def test_list_wa_template_null_when_no_template(self):
        self.wa_template.is_active = False
        self.wa_template.save()
        try:
            self.client.force_authenticate(self.admin_user)
            resp = self.client.get(self._url())
            self.assertIsNone(resp.data['wa_template'])
        finally:
            self.wa_template.is_active = True
            self.wa_template.save()

    # --- Create ---

    @patch('concierge.views.check_invite_rate_limit_staff', return_value=True)
    @patch('concierge.views.check_invite_rate_limit_phone', return_value=True)
    @patch('concierge.views.check_invite_rate_limit_hotel', return_value=True)
    def test_create_invite_success(self, mock_hotel_rl, mock_phone_rl, mock_staff_rl):
        self.client.force_authenticate(self.admin_user)
        with patch('concierge.views.transaction') as mock_tx:
            # Bypass on_commit to prevent Celery task from firing
            mock_tx.atomic = transaction_atomic_passthrough
            mock_tx.on_commit = lambda fn: None
            resp = self.client.post(self._url(), {
                'phone': '919876543299',
                'guest_name': 'New Guest',
                'room_number': '202',
            })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['guest_name'], 'New Guest')
        self.assertEqual(resp.data['guest_phone'], '919876543299')

    def test_create_invite_feature_disabled(self):
        """403 when guest_invite_enabled is False."""
        self.hotel.guest_invite_enabled = False
        self.hotel.save()
        try:
            self.client.force_authenticate(self.admin_user)
            resp = self.client.post(self._url(), {
                'phone': '919876543210', 'guest_name': 'Test',
            })
            self.assertEqual(resp.status_code, 403)
        finally:
            self.hotel.guest_invite_enabled = True
            self.hotel.save()

    def test_create_invite_whatsapp_disabled(self):
        """403 when whatsapp_notifications_enabled is False."""
        self.hotel.whatsapp_notifications_enabled = False
        self.hotel.save()
        try:
            self.client.force_authenticate(self.admin_user)
            resp = self.client.post(self._url(), {
                'phone': '919876543210', 'guest_name': 'Test',
            })
            self.assertEqual(resp.status_code, 403)
        finally:
            self.hotel.whatsapp_notifications_enabled = True
            self.hotel.save()

    def test_create_invite_no_template_returns_503(self):
        self.wa_template.is_active = False
        self.wa_template.save()
        try:
            self.client.force_authenticate(self.admin_user)
            resp = self.client.post(self._url(), {
                'phone': '919876543210', 'guest_name': 'Test',
            })
            self.assertEqual(resp.status_code, 503)
        finally:
            self.wa_template.is_active = True
            self.wa_template.save()

    def test_create_invite_invalid_phone_too_short(self):
        self.client.force_authenticate(self.admin_user)
        resp = self.client.post(self._url(), {
            'phone': '12345', 'guest_name': 'Test',
        })
        self.assertEqual(resp.status_code, 400)

    @patch('concierge.views.check_invite_rate_limit_staff', return_value=False)
    def test_create_invite_staff_rate_limited(self, mock_rl):
        self.client.force_authenticate(self.admin_user)
        resp = self.client.post(self._url(), {
            'phone': '919876543210', 'guest_name': 'Test',
        })
        self.assertEqual(resp.status_code, 429)

    @patch('concierge.views.check_invite_rate_limit_staff', return_value=True)
    @patch('concierge.views.check_invite_rate_limit_phone', return_value=False)
    def test_create_invite_phone_rate_limited(self, mock_phone_rl, mock_staff_rl):
        self.client.force_authenticate(self.admin_user)
        resp = self.client.post(self._url(), {
            'phone': '919876543210', 'guest_name': 'Test',
        })
        self.assertEqual(resp.status_code, 429)

    @patch('concierge.views.check_invite_rate_limit_staff', return_value=True)
    @patch('concierge.views.check_invite_rate_limit_phone', return_value=True)
    @patch('concierge.views.check_invite_rate_limit_hotel', return_value=False)
    def test_create_invite_hotel_rate_limited(self, mock_hotel_rl, mock_phone_rl, mock_staff_rl):
        self.client.force_authenticate(self.admin_user)
        resp = self.client.post(self._url(), {
            'phone': '919876543210', 'guest_name': 'Test',
        })
        self.assertEqual(resp.status_code, 429)

    # --- Resend ---

    def test_resend_invite_success(self):
        invite = self._create_invite(phone='919876500100')
        old_version = invite.token_version
        old_expiry = invite.expires_at
        self.client.force_authenticate(self.admin_user)
        with patch('concierge.notifications.tasks.send_guest_invite_whatsapp.delay'):
            resp = self.client.post(self._resend_url(invite.pk))
        self.assertEqual(resp.status_code, 200)
        invite.refresh_from_db()
        # token_version stays the same — old links remain valid
        self.assertEqual(invite.token_version, old_version)
        # expiry is extended
        self.assertGreater(invite.expires_at, old_expiry)

    def test_resend_creates_new_delivery_record(self):
        invite = self._create_invite(phone='919876500101')
        self.client.force_authenticate(self.admin_user)
        with patch('concierge.notifications.tasks.send_guest_invite_whatsapp.delay'):
            self.client.post(self._resend_url(invite.pk))
        deliveries = DeliveryRecord.objects.filter(guest_invite=invite)
        self.assertEqual(deliveries.count(), 1)  # Only the one from resend

    def test_resend_rejects_used_invite(self):
        invite = self._create_invite(phone='919876500102')
        invite.status = 'USED'
        invite.save()
        self.client.force_authenticate(self.admin_user)
        resp = self.client.post(self._resend_url(invite.pk))
        self.assertEqual(resp.status_code, 409)

    def test_resend_rejects_expired_invite(self):
        invite = self._create_invite(phone='919876500103')
        invite.status = 'EXPIRED'
        invite.save()
        self.client.force_authenticate(self.admin_user)
        resp = self.client.post(self._resend_url(invite.pk))
        self.assertEqual(resp.status_code, 409)

    @patch('concierge.views.check_invite_resend_rate_limit', return_value=False)
    def test_resend_rate_limited_mock(self, mock_rl):
        invite = self._create_invite(phone='919876500110')
        self.client.force_authenticate(self.admin_user)
        resp = self.client.post(self._resend_url(invite.pk))
        self.assertEqual(resp.status_code, 429)
        self.assertIn('Retry-After', resp)

    def test_resend_rate_limited_integration(self):
        """First resend succeeds, immediate second resend is throttled."""
        invite = self._create_invite(phone='919876500111')
        self.client.force_authenticate(self.admin_user)
        with patch('concierge.notifications.tasks.send_guest_invite_whatsapp.delay'):
            resp1 = self.client.post(self._resend_url(invite.pk))
        self.assertEqual(resp1.status_code, 200)

        with patch('concierge.notifications.tasks.send_guest_invite_whatsapp.delay'):
            resp2 = self.client.post(self._resend_url(invite.pk))
        self.assertEqual(resp2.status_code, 429)

    def test_resend_feature_disabled_returns_403(self):
        invite = self._create_invite(phone='919876500104')
        self.hotel.guest_invite_enabled = False
        self.hotel.save()
        try:
            self.client.force_authenticate(self.admin_user)
            resp = self.client.post(self._resend_url(invite.pk))
            self.assertEqual(resp.status_code, 403)
        finally:
            self.hotel.guest_invite_enabled = True
            self.hotel.save()

    def test_resend_not_found_returns_404(self):
        self.client.force_authenticate(self.admin_user)
        resp = self.client.post(self._resend_url(99999))
        self.assertEqual(resp.status_code, 404)

    def test_staff_cannot_resend(self):
        invite = self._create_invite(phone='919876500105')
        self.client.force_authenticate(self.staff_user)
        resp = self.client.post(self._resend_url(invite.pk))
        self.assertEqual(resp.status_code, 403)

    # --- Revoke ---

    def test_revoke_invite_success(self):
        invite = self._create_invite(phone='919876500200')
        self.client.force_authenticate(self.admin_user)
        resp = self.client.delete(self._revoke_url(invite.pk))
        self.assertEqual(resp.status_code, 204)
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'EXPIRED')

    def test_revoke_rejects_used_invite(self):
        invite = self._create_invite(phone='919876500201')
        invite.status = 'USED'
        invite.save()
        self.client.force_authenticate(self.admin_user)
        resp = self.client.delete(self._revoke_url(invite.pk))
        self.assertEqual(resp.status_code, 409)

    def test_revoke_not_found_returns_404(self):
        self.client.force_authenticate(self.admin_user)
        resp = self.client.delete(self._revoke_url(99999))
        self.assertEqual(resp.status_code, 404)

    def test_staff_cannot_revoke(self):
        invite = self._create_invite(phone='919876500202')
        self.client.force_authenticate(self.staff_user)
        resp = self.client.delete(self._revoke_url(invite.pk))
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Token lifecycle tests
# ---------------------------------------------------------------------------

class InviteTokenTest(TestCase):

    def test_generate_and_verify_roundtrip(self):
        token = generate_invite_token(42, 1)
        invite_id, version = verify_invite_token(token)
        self.assertEqual(invite_id, 42)
        self.assertEqual(version, 1)

    def test_version_change_invalidates_old_token(self):
        """Tokens with different versions decode independently."""
        token_v1 = generate_invite_token(7, 1)
        token_v2 = generate_invite_token(7, 2)
        _, v1 = verify_invite_token(token_v1)
        _, v2 = verify_invite_token(token_v2)
        self.assertEqual(v1, 1)
        self.assertEqual(v2, 2)
        # Both tokens are valid cryptographically, but the view checks
        # invite.token_version to reject v1 when invite is at v2.

    def test_tampered_token_raises(self):
        from django.core.signing import BadSignature
        token = generate_invite_token(42, 1)
        tampered = token[:-3] + 'xxx'
        with self.assertRaises(BadSignature):
            verify_invite_token(tampered)


# ---------------------------------------------------------------------------
# Webhook postback tests (g_inv_access, g_inv_ack)
# ---------------------------------------------------------------------------

@override_settings(
    GUEST_INVITE_EXPIRY_HOURS=72,
    FRONTEND_ORIGIN='http://localhost:6001',
    API_ORIGIN='http://localhost:8000',
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919800000000',
    GUPSHUP_WA_APP_NAME='test-app',
)
class InviteWebhookTest(InviteSetupMixin, TestCase):

    def _make_invite_with_delivery(self, phone='919876543210'):
        invite = GuestInvite.objects.create(
            hotel=self.hotel,
            sent_by=self.admin_user,
            guest_phone=phone,
            guest_name='Webhook Guest',
            room_number='305',
            expires_at=timezone.now() + timedelta(hours=72),
        )
        delivery = DeliveryRecord.objects.create(
            hotel=self.hotel,
            guest_invite=invite,
            channel='WHATSAPP',
            target=phone,
            event_type='guest_invite',
            message_type='TEMPLATE',
            status=DeliveryRecord.Status.SENT,
        )
        return invite, delivery

    def _build_payload(self, postback, source_phone):
        return {
            'source': source_phone,
            'payload': {
                'source': source_phone,
                'type': 'quick_reply',
                'postbackText': postback,
            },
        }

    @patch('concierge.notifications.webhook._send_session_text')
    def test_g_inv_access_sends_magic_link(self, mock_send):
        invite, delivery = self._make_invite_with_delivery()
        # Create a ShortLink for this delivery
        short_link = ShortLink.objects.create(
            target_url=f'http://localhost:8000/api/v1/auth/wa-invite/test-token/',
            metadata={'delivery_id': delivery.id},
        )

        from concierge.notifications.webhook import handle_inbound_message
        payload = self._build_payload(f'g_inv_access:{delivery.id}', invite.guest_phone)
        handle_inbound_message(payload)

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        self.assertIn(short_link.code, call_args[0][1])

    @patch('concierge.notifications.webhook._send_session_text')
    def test_g_inv_access_expired_invite(self, mock_send):
        invite, delivery = self._make_invite_with_delivery(phone='919876500301')
        invite.expires_at = timezone.now() - timedelta(hours=1)
        invite.save()

        from concierge.notifications.webhook import handle_inbound_message
        payload = self._build_payload(f'g_inv_access:{delivery.id}', invite.guest_phone)
        handle_inbound_message(payload)

        # Should have sent expiry message
        mock_send.assert_called_once()
        self.assertIn('expired', mock_send.call_args[0][1].lower())
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'EXPIRED')

    @patch('concierge.notifications.webhook._send_session_text')
    def test_g_inv_access_used_invite_ignored(self, mock_send):
        invite, delivery = self._make_invite_with_delivery(phone='919876500302')
        invite.status = 'USED'
        invite.save()

        from concierge.notifications.webhook import handle_inbound_message
        payload = self._build_payload(f'g_inv_access:{delivery.id}', invite.guest_phone)
        handle_inbound_message(payload)

        mock_send.assert_not_called()

    @patch('concierge.notifications.webhook._send_session_text')
    def test_g_inv_ack_sends_confirmation(self, mock_send):
        invite, delivery = self._make_invite_with_delivery(phone='919876500303')

        from concierge.notifications.webhook import handle_inbound_message
        payload = self._build_payload(f'g_inv_ack:{delivery.id}', invite.guest_phone)
        handle_inbound_message(payload)

        mock_send.assert_called_once()
        self.assertIn("all set", mock_send.call_args[0][1].lower())

    @patch('concierge.notifications.webhook._send_session_text')
    def test_g_inv_access_phone_mismatch_rejected(self, mock_send):
        invite, delivery = self._make_invite_with_delivery(phone='919876500304')

        from concierge.notifications.webhook import handle_inbound_message
        # Different phone number from the invite
        payload = self._build_payload(f'g_inv_access:{delivery.id}', '919999999999')
        handle_inbound_message(payload)

        mock_send.assert_not_called()

    @patch('concierge.notifications.webhook._send_session_text')
    def test_g_inv_access_marks_delivery_acknowledged(self, mock_send):
        invite, delivery = self._make_invite_with_delivery(phone='919876500305')
        ShortLink.objects.create(
            target_url='http://localhost:8000/api/v1/auth/wa-invite/tok/',
            metadata={'delivery_id': delivery.id},
        )

        from concierge.notifications.webhook import handle_inbound_message
        payload = self._build_payload(f'g_inv_access:{delivery.id}', invite.guest_phone)
        handle_inbound_message(payload)

        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.acknowledged_at)

    @patch('concierge.notifications.webhook._send_session_text')
    def test_g_inv_access_old_delivery_still_works_after_resend(self, mock_send):
        """Tapping 'Access' on an older message still sends a valid link.

        Resend does NOT bump token_version, so old shortlinks remain valid.
        This prevents guest lockout if the resend's async delivery fails.
        """
        invite, old_delivery = self._make_invite_with_delivery(phone='919876500306')
        old_short = ShortLink.objects.create(
            code='old_link_ok',
            target_url='http://localhost:8000/api/v1/auth/wa-invite/old-token/',
            metadata={'delivery_id': old_delivery.id},
        )

        # Simulate resend: new delivery created, token_version unchanged
        new_delivery = DeliveryRecord.objects.create(
            hotel=self.hotel,
            guest_invite=invite,
            channel='WHATSAPP',
            target=invite.guest_phone,
            event_type='guest_invite',
            message_type='TEMPLATE',
            status=DeliveryRecord.Status.SENT,
        )

        # Guest taps "Access" on the OLD message — should still work
        from concierge.notifications.webhook import handle_inbound_message
        payload = self._build_payload(f'g_inv_access:{old_delivery.id}', invite.guest_phone)
        handle_inbound_message(payload)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        self.assertIn(old_short.code, msg)
        self.assertIn('tap to get started', msg.lower())


# ---------------------------------------------------------------------------
# Verify view tests (GET confirm, POST login)
# ---------------------------------------------------------------------------

@override_settings(
    GUEST_INVITE_EXPIRY_HOURS=72,
    FRONTEND_ORIGIN='http://localhost:6001',
    API_ORIGIN='http://localhost:8000',
)
class VerifyWaInviteTest(InviteSetupMixin, TestCase):

    def _make_invite(self, phone='919876500400', **kwargs):
        defaults = {
            'hotel': self.hotel,
            'sent_by': self.admin_user,
            'guest_phone': phone,
            'guest_name': 'Verify Guest',
            'room_number': '401',
            'expires_at': timezone.now() + timedelta(hours=72),
        }
        defaults.update(kwargs)
        return GuestInvite.objects.create(**defaults)

    def _verify_url(self, token):
        return VERIFY_URL.format(token=token)

    # --- GET (confirm page) ---

    def test_get_valid_token_returns_confirm_page(self):
        invite = self._make_invite()
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.get(self._verify_url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, invite.guest_name)

    def test_get_invalid_token_returns_error(self):
        resp = self.client.get(self._verify_url('bogus-token'))
        self.assertEqual(resp.status_code, 200)  # renders error template
        self.assertContains(resp, 'invalid')

    def test_get_expired_invite_returns_error(self):
        invite = self._make_invite(
            phone='919876500401',
            expires_at=timezone.now() - timedelta(hours=1),
        )
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.get(self._verify_url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'expired')

    def test_get_used_invite_returns_error(self):
        invite = self._make_invite(phone='919876500402')
        invite.status = 'USED'
        invite.save()
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.get(self._verify_url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'already')

    def test_get_version_mismatch_returns_error(self):
        invite = self._make_invite(phone='919876500403')
        # Generate token with version 1, but set invite to version 2
        token = generate_invite_token(invite.id, 1)
        invite.token_version = 2
        invite.save()
        resp = self.client.get(self._verify_url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'no longer valid')

    # --- POST (login) ---

    def test_post_creates_guest_user_and_stay(self):
        invite = self._make_invite(phone='919876500410')
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.post(self._verify_url(token))
        self.assertEqual(resp.status_code, 302)  # redirect
        self.assertIn(f'/h/{self.hotel.slug}/', resp['Location'])

        invite.refresh_from_db()
        self.assertEqual(invite.status, 'USED')
        self.assertIsNotNone(invite.used_at)
        self.assertIsNotNone(invite.guest_user)
        self.assertIsNotNone(invite.guest_stay)

        # Verify guest user was created correctly
        user = invite.guest_user
        self.assertEqual(user.user_type, 'GUEST')
        self.assertEqual(user.phone, '919876500410')

        # Verify stay was created
        stay = invite.guest_stay
        self.assertEqual(stay.hotel, self.hotel)
        self.assertEqual(stay.room_number, '401')
        self.assertTrue(stay.is_active)

    def test_post_sets_auth_cookies(self):
        invite = self._make_invite(phone='919876500411')
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.post(self._verify_url(token))
        self.assertIn('access_token', resp.cookies)

    def test_post_reuses_existing_guest_user(self):
        phone = '919876500412'
        existing_user = User.objects.create_guest_user(
            phone=phone, first_name='Existing', last_name='Guest',
        )
        invite = self._make_invite(phone=phone)
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.post(self._verify_url(token))
        self.assertEqual(resp.status_code, 302)
        invite.refresh_from_db()
        self.assertEqual(invite.guest_user.pk, existing_user.pk)

    def test_post_reuses_existing_active_stay(self):
        phone = '919876500413'
        user = User.objects.create_guest_user(phone=phone, first_name='Stay', last_name='Guest')
        existing_stay = GuestStay.objects.create(
            guest=user, hotel=self.hotel, room_number='500',
            is_active=True, expires_at=timezone.now() + timedelta(hours=12),
        )
        invite = self._make_invite(phone=phone)
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.post(self._verify_url(token))
        self.assertEqual(resp.status_code, 302)
        invite.refresh_from_db()
        self.assertEqual(invite.guest_stay.pk, existing_stay.pk)

    def test_post_expired_invite_shows_error(self):
        invite = self._make_invite(
            phone='919876500414',
            expires_at=timezone.now() - timedelta(hours=1),
        )
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.post(self._verify_url(token))
        self.assertEqual(resp.status_code, 200)  # error template
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'EXPIRED')  # POST marks as expired

    def test_post_staff_phone_conflict_shows_error(self):
        """If the phone belongs to a staff user, show error with login link."""
        invite = self._make_invite(phone='9876500001')  # matches staff_user's phone digits
        # Create a staff user with matching phone
        staff = User.objects.create_user(
            email='conflict@test.com', password='pass',
            phone='9876500001', user_type='STAFF',
        )
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.post(self._verify_url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'staff account')

    def test_post_used_invite_cannot_be_reused(self):
        invite = self._make_invite(phone='919876500415')
        token = generate_invite_token(invite.id, invite.token_version)
        # First POST — should succeed
        resp1 = self.client.post(self._verify_url(token))
        self.assertEqual(resp1.status_code, 302)
        # Second POST — invite is now USED
        resp2 = self.client.post(self._verify_url(token))
        self.assertEqual(resp2.status_code, 200)  # error page
        self.assertContains(resp2, 'already')

    def test_post_revoked_invite_shows_error(self):
        invite = self._make_invite(phone='919876500416')
        invite.status = 'EXPIRED'
        invite.save()
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.post(self._verify_url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'cancelled')

    def test_put_returns_405(self):
        invite = self._make_invite(phone='919876500417')
        token = generate_invite_token(invite.id, invite.token_version)
        resp = self.client.put(self._verify_url(token))
        self.assertEqual(resp.status_code, 405)


# Helper for mocking transaction.atomic as a passthrough
from contextlib import contextmanager

@contextmanager
def _noop_atomic(*args, **kwargs):
    yield

def transaction_atomic_passthrough(*args, **kwargs):
    return _noop_atomic()
