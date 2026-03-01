"""Tests for the notification channels system.

Covers:
- Dispatcher: fan-out to adapters, per-adapter/per-recipient error isolation
- PushAdapter: recipient selection, title/body builders, daily_digest, after_hours_fallback
- WhatsAppAdapter: enabled check, routing, deduplication, service window two-path, idempotency
- EmailAdapter: enabled check, routing, deduplication, idempotency, Celery task
- Webhook: ack/esc_ack/view postback handling, delivery status updates, service window open
- Celery tasks: template send, session send with fallback, response validation, retry logic
- NotificationRoute API: CRUD, pagination=None, channel-specific target validation
"""
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from concierge.models import (
    Department,
    DeliveryRecord,
    Event,
    Experience,
    GuestInvite,
    GuestStay,
    Hotel,
    HotelMembership,
    Notification,
    NotificationRoute,
    RequestActivity,
    ServiceRequest,
    WhatsAppServiceWindow,
)
from concierge.notifications.base import NotificationEvent
from concierge.notifications.dispatcher import dispatch_notification
from concierge.notifications.email import EmailAdapter
from concierge.notifications.push import PushAdapter
from concierge.notifications.whatsapp import WhatsAppAdapter
from users.models import User


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

class NotificationSetupMixin:
    """Shared test data: hotel, department, users, guest stay, service request."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.hotel = Hotel.objects.create(
            name='Test Hotel', slug='test-hotel',
            whatsapp_notifications_enabled=True,
        )
        cls.dept = Department.objects.create(
            hotel=cls.hotel, name='Spa', slug='spa',
        )
        cls.experience = Experience.objects.create(
            department=cls.dept, name='Deep Tissue Massage',
            slug='deep-tissue', category='SPA',
        )

        # Staff in department
        cls.staff_user = User.objects.create_user(
            email='staff@test.com', password='pass',
            first_name='Staff', last_name='One', phone='+919876543210',
        )
        cls.staff_membership = HotelMembership.objects.create(
            user=cls.staff_user, hotel=cls.hotel,
            role='STAFF', department=cls.dept,
        )

        # Admin (no department)
        cls.admin_user = User.objects.create_user(
            email='admin@test.com', password='pass',
            first_name='Admin', last_name='User',
        )
        cls.admin_membership = HotelMembership.objects.create(
            user=cls.admin_user, hotel=cls.hotel, role='ADMIN',
        )

        # Superadmin
        cls.superadmin_user = User.objects.create_user(
            email='super@test.com', password='pass',
            first_name='Super', last_name='Admin',
        )
        cls.superadmin_membership = HotelMembership.objects.create(
            user=cls.superadmin_user, hotel=cls.hotel, role='SUPERADMIN',
        )

        # Guest
        cls.guest_user = User.objects.create_user(
            email='guest@test.com', password='pass',
            first_name='Guest', last_name='User',
        )
        cls.stay = GuestStay.objects.create(
            guest=cls.guest_user, hotel=cls.hotel,
            room_number='101',
            expires_at=timezone.now() + timedelta(days=3),
        )

    def _make_request(self, **kwargs):
        """Create a ServiceRequest with sensible defaults."""
        defaults = {
            'hotel': self.hotel,
            'guest_stay': self.stay,
            'department': self.dept,
            'request_type': 'BOOKING',
        }
        defaults.update(kwargs)
        return ServiceRequest.objects.create(**defaults)

    def _make_event(self, request=None, **kwargs):
        """Create a NotificationEvent with sensible defaults."""
        if request is None:
            request = self._make_request()
        defaults = {
            'event_type': 'request.created',
            'hotel': self.hotel,
            'department': self.dept,
            'request': request,
        }
        defaults.update(kwargs)
        return NotificationEvent(**defaults)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class DispatcherTest(NotificationSetupMixin, TestCase):

    @patch('concierge.notifications.push.PushAdapter.send')
    @patch('concierge.notifications.push.PushAdapter.get_recipients')
    def test_fanout_calls_all_enabled_adapters(self, mock_recipients, mock_send):
        """Dispatcher calls get_recipients + send for each enabled adapter."""
        mock_recipients.return_value = [self.staff_membership]

        event = self._make_event()
        dispatch_notification(event)

        mock_recipients.assert_called_once_with(event)
        mock_send.assert_called_once_with(self.staff_membership, event)

    @patch('concierge.notifications.push.PushAdapter.send')
    @patch('concierge.notifications.push.PushAdapter.get_recipients')
    def test_adapter_error_isolated(self, mock_recipients, mock_send):
        """One adapter raising doesn't block the whole dispatch."""
        mock_recipients.side_effect = RuntimeError("boom")

        event = self._make_event()
        # Should NOT raise — error is logged and swallowed
        dispatch_notification(event)

    @patch('concierge.notifications.push.PushAdapter.send')
    @patch('concierge.notifications.push.PushAdapter.get_recipients')
    def test_per_recipient_error_isolated(self, mock_recipients, mock_send):
        """One recipient failing doesn't block others."""
        mock_recipients.return_value = [self.staff_membership, self.admin_membership]
        mock_send.side_effect = [RuntimeError("boom"), None]

        event = self._make_event()
        dispatch_notification(event)

        # Both recipients attempted
        self.assertEqual(mock_send.call_count, 2)


# ---------------------------------------------------------------------------
# PushAdapter
# ---------------------------------------------------------------------------

class PushAdapterRecipientTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = PushAdapter()

    def test_request_created_includes_dept_staff_and_admins(self):
        event = self._make_event(event_type='request.created')
        recipients = self.adapter.get_recipients(event)
        user_ids = {m.user_id for m in recipients}
        self.assertIn(self.staff_user.id, user_ids)
        self.assertIn(self.admin_user.id, user_ids)
        self.assertIn(self.superadmin_user.id, user_ids)

    def test_daily_digest_only_admins(self):
        event = NotificationEvent(
            event_type='daily_digest',
            hotel=self.hotel,
            extra={'total_requests': 5, 'confirmed': 3, 'pending': 2},
        )
        recipients = self.adapter.get_recipients(event)
        user_ids = {m.user_id for m in recipients}
        self.assertNotIn(self.staff_user.id, user_ids)
        self.assertIn(self.admin_user.id, user_ids)
        self.assertIn(self.superadmin_user.id, user_ids)


class PushAdapterSendTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = PushAdapter()

    @patch('concierge.notifications.push.PushAdapter._build_push_body', return_value='Room 101')
    @patch('concierge.notifications.tasks.send_push_notification_task.delay')
    def test_creates_notification_and_enqueues_push(self, mock_delay, _):
        event = self._make_event()
        notification = self.adapter.send(self.staff_membership, event)

        self.assertIsInstance(notification, Notification)
        self.assertEqual(notification.user, self.staff_user)
        self.assertEqual(notification.hotel, self.hotel)
        self.assertEqual(notification.notification_type, 'NEW_REQUEST')
        mock_delay.assert_called_once()

    @patch('concierge.notifications.tasks.send_push_notification_task.delay')
    def test_daily_digest_skips_web_push(self, mock_delay):
        event = NotificationEvent(
            event_type='daily_digest',
            hotel=self.hotel,
            extra={'total_requests': 5, 'confirmed': 3, 'pending': 2},
        )
        notification = self.adapter.send(self.admin_membership, event)

        self.assertIsInstance(notification, Notification)
        self.assertEqual(notification.notification_type, 'DAILY_DIGEST')
        mock_delay.assert_not_called()


class PushAdapterTitleTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = PushAdapter()

    def test_request_created_title(self):
        event = self._make_event(event_type='request.created')
        self.assertEqual(self.adapter._build_title(event), 'New request: Spa')

    def test_escalation_title(self):
        event = self._make_event(event_type='escalation', escalation_tier=2)
        self.assertEqual(self.adapter._build_title(event), 'Escalation: Spa')

    def test_response_due_title(self):
        event = self._make_event(event_type='response_due')
        self.assertEqual(self.adapter._build_title(event), 'Reminder: Spa')

    def test_after_hours_uses_original_dept_name(self):
        """After-hours title should show the original department, not the fallback."""
        fallback_dept = Department.objects.create(
            hotel=self.hotel, name='Front Desk', slug='front-desk',
        )
        event = self._make_event(
            event_type='after_hours_fallback',
            department=fallback_dept,
            extra={'original_department_name': 'Spa'},
        )
        title = self.adapter._build_title(event)
        self.assertEqual(title, 'After-hours request: Spa')
        self.assertNotIn('Front Desk', title)

    def test_after_hours_falls_back_to_dept_name_if_no_extra(self):
        event = self._make_event(event_type='after_hours_fallback')
        self.assertEqual(self.adapter._build_title(event), 'After-hours request: Spa')

    def test_daily_digest_title(self):
        event = NotificationEvent(
            event_type='daily_digest', hotel=self.hotel,
            extra={'total_requests': 5, 'confirmed': 3, 'pending': 2},
        )
        self.assertEqual(self.adapter._build_title(event), 'Daily Summary')


class PushAdapterBodyTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = PushAdapter()

    def test_request_body_format(self):
        event = self._make_event()
        body = self.adapter._build_notification_body(event)
        self.assertIn('Room 101', body)
        self.assertIn('BOOKING', body)

    def test_daily_digest_body(self):
        event = NotificationEvent(
            event_type='daily_digest', hotel=self.hotel,
            extra={'total_requests': 10, 'confirmed': 7, 'pending': 3},
        )
        body = self.adapter._build_notification_body(event)
        self.assertIn('10 requests today', body)
        self.assertIn('7 confirmed', body)
        self.assertIn('3 pending', body)


# ---------------------------------------------------------------------------
# WhatsAppAdapter
# ---------------------------------------------------------------------------

@override_settings(GUPSHUP_WA_API_KEY='test-key')
class WhatsAppAdapterEnabledTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = WhatsAppAdapter()

    def test_enabled_when_hotel_flag_and_api_key(self):
        self.assertTrue(self.adapter.is_enabled(self.hotel))

    def test_disabled_when_hotel_flag_off(self):
        self.hotel.whatsapp_notifications_enabled = False
        self.assertFalse(self.adapter.is_enabled(self.hotel))
        self.hotel.whatsapp_notifications_enabled = True  # Reset

    @override_settings(GUPSHUP_WA_API_KEY='')
    def test_disabled_when_no_api_key(self):
        self.assertFalse(self.adapter.is_enabled(self.hotel))

    def test_skips_non_request_events(self):
        event = NotificationEvent(
            event_type='daily_digest', hotel=self.hotel,
            extra={'total_requests': 5, 'confirmed': 3, 'pending': 2},
        )
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(recipients, [])


@override_settings(GUPSHUP_WA_API_KEY='test-key')
class WhatsAppAdapterRoutingTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = WhatsAppAdapter()
        self.route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            label='Staff One', created_by=self.admin_user,
        )

    def test_routes_to_department_wide_route(self):
        event = self._make_event()
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].target, '919876543210')

    def test_includes_experience_specific_routes(self):
        exp_route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            experience=self.experience,
            channel='WHATSAPP', target='919111222333',
            label='Exp Staff', created_by=self.admin_user,
        )
        req = self._make_request(experience=self.experience)
        event = self._make_event(request=req)
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertIn('919876543210', targets)
        self.assertIn('919111222333', targets)

    def test_deduplicates_same_target(self):
        """Two routes to the same phone → only one recipient."""
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            experience=self.experience,
            channel='WHATSAPP', target='919876543210',
            label='Same phone, different route',
            created_by=self.admin_user,
        )
        req = self._make_request(experience=self.experience)
        event = self._make_event(request=req)
        recipients = self.adapter.get_recipients(event)
        targets = [r.target for r in recipients]
        self.assertEqual(targets.count('919876543210'), 1)

    def test_inactive_routes_excluded(self):
        self.route.is_active = False
        self.route.save()
        event = self._make_event()
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(len(recipients), 0)
        self.route.is_active = True
        self.route.save()


@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919187551736',
    GUPSHUP_WA_APP_NAME='refuje',
    GUPSHUP_WA_STAFF_REQUEST_TEMPLATE_ID='tmpl-req',
)
class WhatsAppAdapterSendTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = WhatsAppAdapter()
        self.route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            label='Staff One', created_by=self.admin_user,
        )

    @patch('concierge.notifications.tasks.send_whatsapp_template_notification.delay')
    def test_send_template_when_no_service_window(self, mock_delay):
        event = self._make_event()
        record = self.adapter.send(self.route, event)

        self.assertIsInstance(record, DeliveryRecord)
        self.assertEqual(record.status, 'QUEUED')
        self.assertEqual(record.message_type, 'TEMPLATE')
        self.assertEqual(record.channel, 'WHATSAPP')
        mock_delay.assert_called_once()

    @patch('concierge.notifications.tasks.send_whatsapp_session_notification.delay')
    def test_send_session_when_active_window(self, mock_delay):
        WhatsAppServiceWindow.objects.create(
            hotel=self.hotel, phone='919876543210',
            last_inbound_at=timezone.now(),
        )
        event = self._make_event()
        record = self.adapter.send(self.route, event)

        self.assertEqual(record.message_type, 'SESSION')
        mock_delay.assert_called_once()

    @patch('concierge.notifications.tasks.send_whatsapp_template_notification.delay')
    def test_send_template_when_window_expired(self, mock_delay):
        WhatsAppServiceWindow.objects.create(
            hotel=self.hotel, phone='919876543210',
            last_inbound_at=timezone.now() - timedelta(hours=24),
        )
        event = self._make_event()
        record = self.adapter.send(self.route, event)

        self.assertEqual(record.message_type, 'TEMPLATE')
        mock_delay.assert_called_once()

    @patch('concierge.notifications.tasks.send_whatsapp_template_notification.delay')
    def test_idempotency_prevents_duplicate(self, mock_delay):
        event = self._make_event()
        record1 = self.adapter.send(self.route, event)
        record2 = self.adapter.send(self.route, event)

        self.assertEqual(record1.id, record2.id)
        mock_delay.assert_called_once()  # Only one task enqueued

    def test_params_use_original_dept_name_for_after_hours(self):
        event = self._make_event(
            event_type='after_hours_fallback',
            extra={'original_department_name': 'Pool Bar'},
        )
        params = self.adapter._build_params(event)
        self.assertEqual(params['department'], 'Pool Bar')


# ---------------------------------------------------------------------------
# Webhook — inbound message handling
# ---------------------------------------------------------------------------

@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919187551736',
    GUPSHUP_WA_APP_NAME='refuje',
    FRONTEND_ORIGIN='http://localhost:6001',
)
class WebhookAckTest(NotificationSetupMixin, TestCase):

    def _payload(self, postback_text, source='919876543210'):
        return {
            'payload': {
                'source': source,
                'type': 'quick_reply',
                'postbackText': postback_text,
            },
        }

    def test_ack_postback_acknowledges_request(self):
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        # Create a delivery record for this request
        DeliveryRecord.objects.create(
            hotel=self.hotel, request=req,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )

        handle_inbound_message(self._payload(f'ack:{req.public_id}'))

        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')
        self.assertIsNotNone(req.acknowledged_at)

        # Activity log created
        activity = RequestActivity.objects.filter(
            request=req, action='ACKNOWLEDGED',
        ).first()
        self.assertIsNotNone(activity)
        self.assertEqual(activity.details['channel'], 'whatsapp')

    def test_esc_ack_postback_acknowledges_request(self):
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        handle_inbound_message(self._payload(f'esc_ack:{req.public_id}:2'))

        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')

    def test_view_postback_also_acknowledges_request(self):
        """All postback types (incl. view) trigger request-level ack per plan."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()

        with patch('concierge.notifications.webhook._send_session_text'):
            handle_inbound_message(self._payload(f'view:{req.public_id}'))

        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')

    def test_view_postback_sends_dashboard_url(self):
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()

        with patch('concierge.notifications.webhook._send_session_text') as mock_send:
            handle_inbound_message(self._payload(f'view:{req.public_id}'))

        mock_send.assert_called_once()
        url_text = mock_send.call_args[0][1]
        self.assertIn(str(req.public_id), url_text)
        self.assertIn('/dashboard/requests/', url_text)

    def test_already_acknowledged_noop(self):
        """Ack on already-acknowledged request does not change status."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request(status='ACKNOWLEDGED', acknowledged_at=timezone.now())
        handle_inbound_message(self._payload(f'ack:{req.public_id}'))

        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')

    def test_delivery_record_acknowledged(self):
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        record = DeliveryRecord.objects.create(
            hotel=self.hotel, request=req,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )

        handle_inbound_message(self._payload(f'ack:{req.public_id}'))

        record.refresh_from_db()
        self.assertIsNotNone(record.acknowledged_at)

    def test_unknown_request_returns_silently(self):
        from concierge.notifications.webhook import handle_inbound_message

        fake_id = uuid.uuid4()
        # Should not raise
        handle_inbound_message(self._payload(f'ack:{fake_id}'))

    def test_service_window_opened_on_postback(self):
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        handle_inbound_message(self._payload(f'ack:{req.public_id}'))

        window = WhatsAppServiceWindow.objects.filter(
            hotel=self.hotel, phone='919876543210',
        ).first()
        self.assertIsNotNone(window)
        self.assertTrue(window.is_active)


@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
)
class WebhookDeliveryStatusTest(NotificationSetupMixin, TestCase):

    def test_delivered_status_update(self):
        from concierge.notifications.webhook import handle_message_event

        record = DeliveryRecord.objects.create(
            hotel=self.hotel, channel='WHATSAPP',
            target='919876543210', event_type='request.created',
            status='SENT', provider_message_id='gs-msg-123',
        )

        handle_message_event({
            'payload': {
                'gsId': 'gs-msg-123',
                'type': 'delivered',
            },
        })

        record.refresh_from_db()
        self.assertEqual(record.status, 'DELIVERED')
        self.assertIsNotNone(record.delivered_at)

    def test_failed_status_update(self):
        from concierge.notifications.webhook import handle_message_event

        record = DeliveryRecord.objects.create(
            hotel=self.hotel, channel='WHATSAPP',
            target='919876543210', event_type='request.created',
            status='SENT', provider_message_id='gs-msg-456',
        )

        handle_message_event({
            'payload': {
                'gsId': 'gs-msg-456',
                'type': 'failed',
                'code': '470',
                'reason': 'Recipient not on WhatsApp',
            },
        })

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')
        self.assertIn('470', record.error_message)

    def test_unknown_message_id_noop(self):
        from concierge.notifications.webhook import handle_message_event

        # Should not raise
        handle_message_event({
            'payload': {'gsId': 'nonexistent', 'type': 'delivered'},
        })


@override_settings(GUPSHUP_WA_API_KEY='test-key')
class WebhookDeliverySSETest(NotificationSetupMixin, TestCase):
    """Tests for SSE emission and idempotency in delivery status updates."""

    def _make_invite_record(self, status='SENT', msg_id='gs-inv-001'):
        invite = GuestInvite.objects.create(
            hotel=self.hotel, guest_phone='919876543210', guest_name='Test',
            expires_at=timezone.now() + timedelta(hours=48),
        )
        record = DeliveryRecord.objects.create(
            hotel=self.hotel, channel='WHATSAPP', target='919876543210',
            event_type='guest_invite', message_type='TEMPLATE',
            status=status, provider_message_id=msg_id,
            guest_invite=invite,
        )
        return invite, record

    @patch('concierge.notifications.webhook.publish_invite_event')
    @patch('concierge.notifications.webhook.transaction.on_commit', lambda fn: fn())
    def test_invite_delivery_emits_sse(self, mock_publish):
        """Delivery status update for invite-linked record should emit SSE."""
        from concierge.notifications.webhook import handle_message_event

        invite, record = self._make_invite_record()

        handle_message_event({
            'payload': {'gsId': 'gs-inv-001', 'type': 'delivered'},
        })

        record.refresh_from_db()
        self.assertEqual(record.status, 'DELIVERED')
        mock_publish.assert_called_once_with(
            self.hotel.id, record.id, invite.id, 'DELIVERED',
        )

    @patch('concierge.notifications.webhook.publish_invite_event')
    def test_non_invite_delivery_no_sse(self, mock_publish):
        """Delivery status update for non-invite record should NOT emit SSE."""
        from concierge.notifications.webhook import handle_message_event

        DeliveryRecord.objects.create(
            hotel=self.hotel, channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
            provider_message_id='gs-req-001',
        )

        handle_message_event({
            'payload': {'gsId': 'gs-req-001', 'type': 'delivered'},
        })

        mock_publish.assert_not_called()

    @patch('concierge.notifications.webhook.publish_invite_event')
    def test_idempotent_same_status_skips_update_and_sse(self, mock_publish):
        """Repeated webhook with same status should skip update and SSE."""
        from concierge.notifications.webhook import handle_message_event

        _invite, record = self._make_invite_record(status='DELIVERED')

        handle_message_event({
            'payload': {'gsId': 'gs-inv-001', 'type': 'delivered'},
        })

        mock_publish.assert_not_called()

    @patch('concierge.notifications.webhook.publish_invite_event')
    def test_repeated_failure_updates_error_message(self, mock_publish):
        """Repeated FAILED webhook with new error info should update error_message."""
        from concierge.notifications.webhook import handle_message_event

        _invite, record = self._make_invite_record(status='FAILED', msg_id='gs-inv-err')
        record.error_message = '470: Old reason'
        record.save(update_fields=['error_message'])

        handle_message_event({
            'payload': {
                'gsId': 'gs-inv-err', 'type': 'failed',
                'code': '471', 'reason': 'New specific reason',
            },
        })

        record.refresh_from_db()
        self.assertIn('471', record.error_message)
        # Same status — no SSE emit
        mock_publish.assert_not_called()


class WebhookButtonTypeTest(NotificationSetupMixin, TestCase):
    """Tests for type='button' payloads and dict reply fields."""

    def test_button_type_with_string_reply(self):
        """type='button' + reply as string should parse postback."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        handle_inbound_message({
            'payload': {
                'source': '919876543210',
                'type': 'button',
                'reply': f'ack:{req.public_id}',
            },
        })
        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')

    def test_button_type_with_dict_reply(self):
        """type='button' + reply as {"id": "ack:..."} should not crash."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        handle_inbound_message({
            'payload': {
                'source': '919876543210',
                'type': 'button',
                'reply': {'id': f'ack:{req.public_id}', 'title': 'Acknowledge'},
            },
        })
        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')

    def test_button_type_with_dict_reply_view(self):
        """type='button' + reply={"id": "view:..."} sends dashboard URL."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        with patch('concierge.notifications.webhook._send_session_text') as mock_send:
            handle_inbound_message({
                'payload': {
                    'source': '919876543210',
                    'type': 'button',
                    'reply': {'id': f'view:{req.public_id}'},
                },
            })
        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')
        mock_send.assert_called_once()

    def test_button_type_with_empty_dict_reply(self):
        """type='button' + reply={} should not crash (no postback parsed)."""
        from concierge.notifications.webhook import handle_inbound_message

        # Should not raise — no postback, no window, skips silently
        handle_inbound_message({
            'payload': {
                'source': '911111111111',
                'type': 'button',
                'reply': {},
            },
        })


class WebhookTextFallbackTest(NotificationSetupMixin, TestCase):
    """Tests for free-text replies that match button labels."""

    def test_text_acknowledge_via_delivery_fallback(self):
        """Typing 'Acknowledge' should ack the most recent pending request."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        DeliveryRecord.objects.create(
            hotel=self.hotel, request=req,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )
        handle_inbound_message({
            'payload': {
                'source': '919876543210',
                'type': 'text',
                'text': 'Acknowledge',
            },
        })
        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')

    def test_text_view_details_sends_url(self):
        """Typing 'View Details' should send dashboard link."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        DeliveryRecord.objects.create(
            hotel=self.hotel, request=req,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )
        with patch('concierge.notifications.webhook._send_session_text') as mock_send:
            handle_inbound_message({
                'payload': {
                    'source': '919876543210',
                    'type': 'text',
                    'text': 'View Details',
                },
            })
        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')
        mock_send.assert_called_once()

    def test_text_on_it_acknowledges(self):
        """'On it' maps to ack action."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        DeliveryRecord.objects.create(
            hotel=self.hotel, request=req,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )
        handle_inbound_message({
            'payload': {
                'source': '919876543210',
                'type': 'text',
                'text': 'on it',
            },
        })
        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')

    def test_unrecognized_text_no_action(self):
        """Arbitrary text should not acknowledge any request."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        DeliveryRecord.objects.create(
            hotel=self.hotel, request=req,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )
        handle_inbound_message({
            'payload': {
                'source': '919876543210',
                'type': 'text',
                'text': 'hello there',
            },
        })
        req.refresh_from_db()
        self.assertEqual(req.status, 'CREATED')

    def test_no_delivery_record_skips(self):
        """Text from phone with no delivery records should skip silently."""
        from concierge.notifications.webhook import handle_inbound_message

        # Should not raise
        handle_inbound_message({
            'payload': {
                'source': '910000000000',
                'type': 'text',
                'text': 'Acknowledge',
            },
        })

    def test_null_request_delivery_record_skipped(self):
        """DeliveryRecord with request=None should be skipped in fallback."""
        from concierge.notifications.webhook import handle_inbound_message

        req = self._make_request()
        # Older valid record
        DeliveryRecord.objects.create(
            hotel=self.hotel, request=req,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )
        # Newer record with no request (should be skipped by filter)
        DeliveryRecord.objects.create(
            hotel=self.hotel, request=None,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )
        handle_inbound_message({
            'payload': {
                'source': '919876543210',
                'type': 'text',
                'text': 'Acknowledge',
            },
        })
        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')

    def test_delivery_fallback_scoped_to_member_hotels(self):
        """Fallback should only match deliveries from hotels where phone is a member."""
        from concierge.notifications.webhook import handle_inbound_message

        # Create a second hotel where staff_user is NOT a member
        other_hotel = Hotel.objects.create(name='Other Hotel', slug='other-hotel')
        other_dept = Department.objects.create(hotel=other_hotel, name='Bar', slug='bar')
        other_req = ServiceRequest.objects.create(
            hotel=other_hotel, guest_stay=self.stay,
            department=other_dept, request_type='BOOKING',
        )
        # Delivery record on the OTHER hotel (newer)
        DeliveryRecord.objects.create(
            hotel=other_hotel, request=other_req,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )
        # Delivery record on member's hotel (older)
        req = self._make_request()
        DeliveryRecord.objects.create(
            hotel=self.hotel, request=req,
            channel='WHATSAPP', target='919876543210',
            event_type='request.created', status='SENT',
        )

        handle_inbound_message({
            'payload': {
                'source': '919876543210',
                'type': 'text',
                'text': 'Acknowledge',
            },
        })
        # Should ack the member's hotel request, not the other hotel's
        req.refresh_from_db()
        self.assertEqual(req.status, 'ACKNOWLEDGED')
        other_req.refresh_from_db()
        self.assertEqual(other_req.status, 'CREATED')


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------

@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919187551736',
    GUPSHUP_WA_APP_NAME='refuje',
    GUPSHUP_WA_STAFF_REQUEST_TEMPLATE_ID='tmpl-req',
    GUPSHUP_WA_STAFF_ESCALATION_TEMPLATE_ID='tmpl-esc',
    GUPSHUP_WA_STAFF_RESPONSE_DUE_TEMPLATE_ID='tmpl-due',
)
class WhatsAppTemplateTaskTest(NotificationSetupMixin, TestCase):

    def _make_record(self, event_type='request.created', **kwargs):
        defaults = {
            'hotel': self.hotel,
            'channel': 'WHATSAPP',
            'target': '919876543210',
            'event_type': event_type,
            'status': 'QUEUED',
            'message_type': 'TEMPLATE',
        }
        defaults.update(kwargs)
        return DeliveryRecord.objects.create(**defaults)

    @patch('concierge.notifications.tasks.http_requests.post')
    def test_template_send_success(self, mock_post):
        from concierge.notifications.tasks import send_whatsapp_template_notification

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gup-123'}
        mock_post.return_value = mock_resp

        record = self._make_record()
        params = {
            'guest_name': 'Guest User', 'room_number': '101',
            'department': 'Spa', 'subject': 'Deep Tissue Massage',
            'public_id': str(uuid.uuid4()),
        }

        send_whatsapp_template_notification(record.id, params)

        record.refresh_from_db()
        self.assertEqual(record.status, 'SENT')
        self.assertEqual(record.provider_message_id, 'gup-123')

    @patch('concierge.notifications.tasks.http_requests.post')
    def test_template_send_provider_error_in_body(self, mock_post):
        """200 with status=error in body should mark FAILED and NOT retry."""
        from concierge.notifications.tasks import send_whatsapp_template_notification

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'error', 'message': 'Invalid template'}
        mock_post.return_value = mock_resp

        record = self._make_record()
        params = {
            'guest_name': 'Guest', 'room_number': '101',
            'department': 'Spa', 'subject': 'Massage',
            'public_id': str(uuid.uuid4()),
        }

        # Should NOT raise (ValueError is not in _TRANSIENT_ERRORS, so no retry)
        send_whatsapp_template_notification(record.id, params)

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')
        self.assertIn('Gupshup API error', record.error_message)

    @patch('concierge.notifications.tasks.http_requests.post')
    def test_template_send_5xx_retries(self, mock_post):
        """5xx triggers RuntimeError which is retryable (raises Retry in test)."""
        from celery.exceptions import Retry
        from concierge.notifications.tasks import send_whatsapp_template_notification

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp

        record = self._make_record()
        params = {
            'guest_name': 'Guest', 'room_number': '101',
            'department': 'Spa', 'subject': 'Massage',
            'public_id': str(uuid.uuid4()),
        }

        with self.assertRaises(RuntimeError):
            send_whatsapp_template_notification(record.id, params)

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')

    @patch('concierge.notifications.tasks.http_requests.post')
    def test_connection_error_retries(self, mock_post):
        """requests.exceptions.ConnectionError should trigger retry."""
        import requests as http_requests
        from concierge.notifications.tasks import send_whatsapp_template_notification

        mock_post.side_effect = http_requests.exceptions.ConnectionError('timeout')

        record = self._make_record()
        params = {
            'guest_name': 'Guest', 'room_number': '101',
            'department': 'Spa', 'subject': 'Massage',
            'public_id': str(uuid.uuid4()),
        }

        with self.assertRaises(http_requests.exceptions.ConnectionError):
            send_whatsapp_template_notification(record.id, params)

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')
        self.assertIn('timeout', record.error_message)


@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919187551736',
    GUPSHUP_WA_APP_NAME='refuje',
    GUPSHUP_WA_STAFF_REQUEST_TEMPLATE_ID='tmpl-req',
)
class WhatsAppSessionTaskTest(NotificationSetupMixin, TestCase):

    def _make_record(self, **kwargs):
        defaults = {
            'hotel': self.hotel,
            'channel': 'WHATSAPP',
            'target': '919876543210',
            'event_type': 'request.created',
            'status': 'QUEUED',
            'message_type': 'SESSION',
        }
        defaults.update(kwargs)
        return DeliveryRecord.objects.create(**defaults)

    @patch('concierge.notifications.tasks.http_requests.post')
    def test_session_send_success(self, mock_post):
        from concierge.notifications.tasks import send_whatsapp_session_notification

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'gup-456'}
        mock_post.return_value = mock_resp

        record = self._make_record()
        params = {
            'guest_name': 'Guest User', 'room_number': '101',
            'department': 'Spa', 'subject': 'Deep Tissue',
            'public_id': str(uuid.uuid4()),
        }

        send_whatsapp_session_notification(record.id, params)

        record.refresh_from_db()
        self.assertEqual(record.status, 'SENT')
        self.assertEqual(record.provider_message_id, 'gup-456')

    @patch('concierge.notifications.tasks.send_whatsapp_template_notification.delay')
    @patch('concierge.notifications.tasks.http_requests.post')
    def test_session_expired_falls_back_to_template(self, mock_post, mock_template_delay):
        from concierge.notifications.tasks import send_whatsapp_session_notification

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'status': 'error',
            'message': '24 hours have passed since customer last replied',
        }
        mock_post.return_value = mock_resp

        record = self._make_record()
        params = {
            'guest_name': 'Guest', 'room_number': '101',
            'department': 'Spa', 'subject': 'Massage',
            'public_id': str(uuid.uuid4()),
        }

        send_whatsapp_session_notification(record.id, params)

        record.refresh_from_db()
        self.assertEqual(record.message_type, 'TEMPLATE')
        # Status still QUEUED (template task will update it)
        mock_template_delay.assert_called_once_with(record.id, params)


# ---------------------------------------------------------------------------
# NotificationRoute API
# ---------------------------------------------------------------------------

class NotificationRouteAPITest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin_user)
        self.base_url = '/api/v1/hotels/test-hotel/admin/notification-routes/'

    def test_list_returns_array_not_paginated(self):
        """Ensure the response is a plain array (pagination_class = None)."""
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            created_by=self.admin_user,
        )
        resp = self.client.get(self.base_url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_create_whatsapp_route(self):
        resp = self.client.post(self.base_url, {
            'department': self.dept.id,
            'channel': 'WHATSAPP',
            'target': '+91 9876543210',
            'label': 'Staff Phone',
        }, format='json')
        self.assertEqual(resp.status_code, 201)

    def test_create_rejects_short_phone(self):
        resp = self.client.post(self.base_url, {
            'department': self.dept.id,
            'channel': 'WHATSAPP',
            'target': '12345',
            'label': 'Bad Phone',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('target', resp.json())

    def test_create_rejects_invalid_email(self):
        resp = self.client.post(self.base_url, {
            'department': self.dept.id,
            'channel': 'EMAIL',
            'target': 'not-an-email',
            'label': 'Bad Email',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('target', resp.json())

    def test_create_accepts_valid_email(self):
        resp = self.client.post(self.base_url, {
            'department': self.dept.id,
            'channel': 'EMAIL',
            'target': 'staff@hotel.com',
            'label': 'Good Email',
        }, format='json')
        self.assertEqual(resp.status_code, 201)

    def test_delete_route(self):
        route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            created_by=self.admin_user,
        )
        resp = self.client.delete(f'{self.base_url}{route.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(NotificationRoute.objects.filter(id=route.id).exists())

    def test_staff_denied(self):
        staff_client = APIClient()
        staff_client.force_authenticate(self.staff_user)
        resp = staff_client.get(self.base_url)
        self.assertEqual(resp.status_code, 403)

    def test_cross_hotel_department_rejected(self):
        other_hotel = Hotel.objects.create(name='Other', slug='other')
        other_dept = Department.objects.create(
            hotel=other_hotel, name='Bar', slug='bar',
        )
        resp = self.client.post(self.base_url, {
            'department': other_dept.id,
            'channel': 'WHATSAPP',
            'target': '919876543210',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_filter_by_department(self):
        """?department= filters server-side."""
        dept2 = Department.objects.create(
            hotel=self.hotel, name='Pool', slug='pool',
        )
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            created_by=self.admin_user,
        )
        NotificationRoute.objects.create(
            hotel=self.hotel, department=dept2,
            channel='WHATSAPP', target='919111222333',
            created_by=self.admin_user,
        )

        # All routes
        resp = self.client.get(self.base_url)
        self.assertEqual(len(resp.json()), 2)

        # Filtered to dept
        resp = self.client.get(f'{self.base_url}?department={self.dept.id}')
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['department'], self.dept.id)

    def test_filter_by_department_invalid_returns_empty(self):
        """?department=abc should return empty list, not 500."""
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            created_by=self.admin_user,
        )
        resp = self.client.get(f'{self.base_url}?department=abc')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_duplicate_route_returns_400(self):
        """Duplicate (dept, channel, target) must return 400, not 500."""
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            created_by=self.admin_user,
        )
        resp = self.client.post(self.base_url, {
            'department': self.dept.id,
            'channel': 'WHATSAPP',
            'target': '919876543210',
            'label': 'Dup',
        }, format='json')
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# Task HTTP status handling
# ---------------------------------------------------------------------------

@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919187551736',
    GUPSHUP_WA_APP_NAME='refuje',
    GUPSHUP_WA_STAFF_REQUEST_TEMPLATE_ID='tmpl-req',
)
class WhatsAppTaskHTTPStatusTest(NotificationSetupMixin, TestCase):
    """Tests for HTTP status branching in WhatsApp tasks."""

    def _make_record(self, message_type='TEMPLATE', **kwargs):
        defaults = {
            'hotel': self.hotel,
            'channel': 'WHATSAPP',
            'target': '919876543210',
            'event_type': 'request.created',
            'status': 'QUEUED',
            'message_type': message_type,
        }
        defaults.update(kwargs)
        return DeliveryRecord.objects.create(**defaults)

    def _params(self):
        return {
            'guest_name': 'Guest', 'room_number': '101',
            'department': 'Spa', 'subject': 'Massage',
            'public_id': str(uuid.uuid4()),
        }

    @patch('concierge.notifications.tasks.http_requests.post')
    def test_template_429_retries(self, mock_post):
        """429 Too Many Requests should trigger retry (transient)."""
        from concierge.notifications.tasks import send_whatsapp_template_notification

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_post.return_value = mock_resp

        record = self._make_record()
        with self.assertRaises(RuntimeError):
            send_whatsapp_template_notification(record.id, self._params())

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')

    @patch('concierge.notifications.tasks.http_requests.post')
    def test_template_401_fails_no_retry(self, mock_post):
        """401 Unauthorized should fail permanently (ValueError, not retried)."""
        from concierge.notifications.tasks import send_whatsapp_template_notification

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = 'Unauthorized'
        mock_post.return_value = mock_resp

        record = self._make_record()
        # Should NOT raise (ValueError not in _TRANSIENT_ERRORS)
        send_whatsapp_template_notification(record.id, self._params())

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')
        self.assertIn('401', record.error_message)

    @patch('concierge.notifications.tasks.http_requests.post')
    def test_template_missing_message_id_fails(self, mock_post):
        """200 with no messageId in body should fail."""
        from concierge.notifications.tasks import send_whatsapp_template_notification

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted'}  # No messageId
        mock_post.return_value = mock_resp

        record = self._make_record()
        send_whatsapp_template_notification(record.id, self._params())

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')
        self.assertIn('messageId', record.error_message)

    @patch('concierge.notifications.tasks.http_requests.post')
    def test_session_401_fails_no_fallback(self, mock_post):
        """Session send with 401 should fail, NOT fall back to template."""
        from concierge.notifications.tasks import send_whatsapp_session_notification

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = 'Unauthorized'
        mock_post.return_value = mock_resp

        record = self._make_record(message_type='SESSION')
        send_whatsapp_session_notification(record.id, self._params())

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')
        self.assertEqual(record.message_type, 'SESSION')  # NOT changed to TEMPLATE
        self.assertIn('401', record.error_message)


# ---------------------------------------------------------------------------
# EmailAdapter — enabled check
# ---------------------------------------------------------------------------

@override_settings(RESEND_API_KEY='test-resend-key')
class EmailAdapterEnabledTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = EmailAdapter()
        self.hotel.email_notifications_enabled = True
        self.hotel.save(update_fields=['email_notifications_enabled'])

    def tearDown(self):
        self.hotel.email_notifications_enabled = False
        self.hotel.save(update_fields=['email_notifications_enabled'])

    def test_enabled_when_hotel_flag_and_api_key(self):
        self.assertTrue(self.adapter.is_enabled(self.hotel))

    def test_disabled_when_hotel_flag_off(self):
        self.hotel.email_notifications_enabled = False
        self.assertFalse(self.adapter.is_enabled(self.hotel))

    @override_settings(RESEND_API_KEY='')
    def test_disabled_when_no_api_key(self):
        self.assertFalse(self.adapter.is_enabled(self.hotel))

    def test_skips_non_request_events(self):
        event = NotificationEvent(
            event_type='daily_digest', hotel=self.hotel,
            extra={'total_requests': 5, 'confirmed': 3, 'pending': 2},
        )
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(recipients, [])


# ---------------------------------------------------------------------------
# EmailAdapter — routing
# ---------------------------------------------------------------------------

@override_settings(RESEND_API_KEY='test-resend-key')
class EmailAdapterRoutingTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = EmailAdapter()
        self.hotel.email_notifications_enabled = True
        self.hotel.save(update_fields=['email_notifications_enabled'])
        self.route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='EMAIL', target='staff@hotel.com',
            label='Staff Email', created_by=self.admin_user,
        )

    def tearDown(self):
        self.hotel.email_notifications_enabled = False
        self.hotel.save(update_fields=['email_notifications_enabled'])

    def test_routes_to_department_wide_route(self):
        event = self._make_event()
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].target, 'staff@hotel.com')

    def test_includes_experience_specific_routes(self):
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            experience=self.experience,
            channel='EMAIL', target='spa-specialist@hotel.com',
            label='Spa Specialist', created_by=self.admin_user,
        )
        req = self._make_request(experience=self.experience)
        event = self._make_event(request=req)
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertIn('staff@hotel.com', targets)
        self.assertIn('spa-specialist@hotel.com', targets)

    def test_deduplicates_same_target(self):
        """Two routes to the same email → only one recipient."""
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            experience=self.experience,
            channel='EMAIL', target='staff@hotel.com',
            label='Same email, different route',
            created_by=self.admin_user,
        )
        req = self._make_request(experience=self.experience)
        event = self._make_event(request=req)
        recipients = self.adapter.get_recipients(event)
        targets = [r.target for r in recipients]
        self.assertEqual(targets.count('staff@hotel.com'), 1)

    def test_inactive_routes_excluded(self):
        self.route.is_active = False
        self.route.save()
        event = self._make_event()
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(len(recipients), 0)
        self.route.is_active = True
        self.route.save()

    def test_ignores_whatsapp_routes(self):
        """EMAIL adapter should not pick up WHATSAPP routes."""
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            label='WA Route', created_by=self.admin_user,
        )
        event = self._make_event()
        recipients = self.adapter.get_recipients(event)
        # Only the EMAIL route, not the WA one
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].channel, 'EMAIL')


# ---------------------------------------------------------------------------
# EmailAdapter — send + idempotency
# ---------------------------------------------------------------------------

@override_settings(
    RESEND_API_KEY='test-resend-key',
    FRONTEND_ORIGIN='http://localhost:6001',
)
class EmailAdapterSendTest(NotificationSetupMixin, TestCase):

    def setUp(self):
        self.adapter = EmailAdapter()
        self.hotel.email_notifications_enabled = True
        self.hotel.save(update_fields=['email_notifications_enabled'])
        self.route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='EMAIL', target='staff@hotel.com',
            label='Staff Email', created_by=self.admin_user,
        )

    def tearDown(self):
        self.hotel.email_notifications_enabled = False
        self.hotel.save(update_fields=['email_notifications_enabled'])

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    def test_send_creates_delivery_record_and_queues_task(self, mock_delay):
        event = self._make_event()
        record = self.adapter.send(self.route, event)

        self.assertIsInstance(record, DeliveryRecord)
        self.assertEqual(record.channel, 'EMAIL')
        self.assertEqual(record.target, 'staff@hotel.com')
        self.assertEqual(record.status, 'QUEUED')
        mock_delay.assert_called_once()

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    def test_idempotency_prevents_duplicate(self, mock_delay):
        event = self._make_event()
        record1 = self.adapter.send(self.route, event)
        record2 = self.adapter.send(self.route, event)

        self.assertEqual(record1.id, record2.id)
        # Task queued only once
        self.assertEqual(mock_delay.call_count, 1)

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    def test_params_include_hotel_and_request_info(self, mock_delay):
        event = self._make_event()
        self.adapter.send(self.route, event)

        call_args = mock_delay.call_args
        params = call_args[0][1]  # Second positional arg
        self.assertEqual(params['hotel_name'], 'Test Hotel')
        self.assertEqual(params['guest_name'], 'Guest User')
        self.assertEqual(params['room_number'], '101')
        self.assertEqual(params['department'], 'Spa')
        self.assertEqual(params['event_type'], 'request.created')

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    def test_after_hours_uses_original_dept_name(self, mock_delay):
        event = self._make_event(
            event_type='after_hours_fallback',
            extra={'original_department_name': 'Pool Bar'},
        )
        self.adapter.send(self.route, event)

        params = mock_delay.call_args[0][1]
        self.assertEqual(params['department'], 'Pool Bar')


# ---------------------------------------------------------------------------
# Cross-channel idempotency
# ---------------------------------------------------------------------------

@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919187551736',
    GUPSHUP_WA_APP_NAME='refuje',
    GUPSHUP_WA_STAFF_REQUEST_TEMPLATE_ID='tmpl-req',
    RESEND_API_KEY='test-resend-key',
)
class CrossChannelIdempotencyTest(NotificationSetupMixin, TestCase):
    """Ensure WA and Email adapters never suppress each other via key collision."""

    def setUp(self):
        self.hotel.whatsapp_notifications_enabled = True
        self.hotel.email_notifications_enabled = True
        self.hotel.save(update_fields=['whatsapp_notifications_enabled', 'email_notifications_enabled'])

    def tearDown(self):
        self.hotel.email_notifications_enabled = False
        self.hotel.save(update_fields=['email_notifications_enabled'])

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    @patch('concierge.notifications.tasks.send_whatsapp_template_notification.delay')
    def test_same_route_id_different_channels_both_create_records(
        self, mock_wa_delay, mock_email_delay,
    ):
        """Even if a WA route and EMAIL route have the same DB id,
        both adapters must create independent DeliveryRecords."""
        wa_route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            label='WA', created_by=self.admin_user,
        )
        email_route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='EMAIL', target='staff@hotel.com',
            label='Email', created_by=self.admin_user,
        )

        event = self._make_event()

        wa_adapter = WhatsAppAdapter()
        email_adapter = EmailAdapter()

        wa_record = wa_adapter.send(wa_route, event)
        email_record = email_adapter.send(email_route, event)

        # Both records exist and are distinct
        self.assertNotEqual(wa_record.id, email_record.id)
        self.assertEqual(wa_record.channel, 'WHATSAPP')
        self.assertEqual(email_record.channel, 'EMAIL')
        self.assertNotEqual(wa_record.idempotency_key, email_record.idempotency_key)

        # Both tasks were queued
        mock_wa_delay.assert_called_once()
        mock_email_delay.assert_called_once()


# ---------------------------------------------------------------------------
# Email Celery task
# ---------------------------------------------------------------------------

@override_settings(
    RESEND_API_KEY='test-resend-key',
    RESEND_FROM_EMAIL='Refuje <test@notifications.refuje.com>',
    FRONTEND_ORIGIN='http://localhost:6001',
)
class EmailTaskTest(NotificationSetupMixin, TestCase):

    def _make_record(self):
        req = self._make_request()
        return DeliveryRecord.objects.create(
            hotel=self.hotel,
            request=req,
            channel='EMAIL',
            target='staff@hotel.com',
            event_type='request.created',
            status='QUEUED',
            message_type='TEMPLATE',
            idempotency_key=f'email:request.created:{req.public_id}:0:test-{uuid.uuid4().hex[:8]}',
        )

    def _params(self):
        return {
            'hotel_name': 'Test Hotel',
            'primary_color': '#1a1a1a',
            'guest_name': 'Guest User',
            'room_number': '101',
            'department': 'Spa',
            'subject': 'Deep Tissue Massage',
            'request_type': 'Booking',
            'public_id': str(uuid.uuid4()),
            'event_type': 'request.created',
            'escalation_tier': None,
        }

    @patch('resend.Emails.send')
    def test_send_success(self, mock_send):
        mock_send.return_value = {'id': 'email-abc-123'}

        record = self._make_record()
        from concierge.notifications.tasks import send_email_notification
        send_email_notification(record.id, self._params())

        record.refresh_from_db()
        self.assertEqual(record.status, 'SENT')
        self.assertEqual(record.provider_message_id, 'email-abc-123')
        mock_send.assert_called_once()

        # Verify email params
        call_kwargs = mock_send.call_args[0][0]
        self.assertEqual(call_kwargs['to'], ['staff@hotel.com'])
        self.assertIn('New Request', call_kwargs['subject'])

    @patch('resend.Emails.send')
    def test_permanent_validation_error_no_retry(self, mock_send):
        from resend.exceptions import ValidationError
        mock_send.side_effect = ValidationError('invalid email', 400, 'validation_error')

        record = self._make_record()
        from concierge.notifications.tasks import send_email_notification
        # Should NOT raise — permanent errors are caught
        send_email_notification(record.id, self._params())

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')
        self.assertIn('invalid email', record.error_message)

    @patch('resend.Emails.send')
    def test_permanent_missing_api_key_no_retry(self, mock_send):
        from resend.exceptions import MissingApiKeyError
        mock_send.side_effect = MissingApiKeyError('missing key', 401, 'missing_api_key')

        record = self._make_record()
        from concierge.notifications.tasks import send_email_notification
        send_email_notification(record.id, self._params())

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')

    @patch('resend.Emails.send')
    def test_rate_limit_retries(self, mock_send):
        from resend.exceptions import RateLimitError
        mock_send.side_effect = RateLimitError('rate limited', 429, 'rate_limit_exceeded')

        record = self._make_record()
        from concierge.notifications.tasks import send_email_notification

        with self.assertRaises(RateLimitError):
            send_email_notification(record.id, self._params())

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')

    @patch('resend.Emails.send')
    def test_application_error_retries(self, mock_send):
        from resend.exceptions import ApplicationError
        mock_send.side_effect = ApplicationError('server error', 500, 'application_error')

        record = self._make_record()
        from concierge.notifications.tasks import send_email_notification

        with self.assertRaises(ApplicationError):
            send_email_notification(record.id, self._params())

        record.refresh_from_db()
        self.assertEqual(record.status, 'FAILED')

    @patch('resend.Emails.send')
    def test_escalation_email_subject(self, mock_send):
        mock_send.return_value = {'id': 'email-esc-123'}

        record = self._make_record()
        params = self._params()
        params['event_type'] = 'escalation'
        params['escalation_tier'] = 2

        from concierge.notifications.tasks import send_email_notification
        send_email_notification(record.id, params)

        call_kwargs = mock_send.call_args[0][0]
        self.assertIn('Escalation', call_kwargs['subject'])

    @patch('resend.Emails.send')
    def test_email_html_contains_dashboard_link(self, mock_send):
        mock_send.return_value = {'id': 'email-link-123'}

        record = self._make_record()
        params = self._params()

        from concierge.notifications.tasks import send_email_notification
        send_email_notification(record.id, params)

        call_kwargs = mock_send.call_args[0][0]
        self.assertIn(f'/dashboard/requests/{params["public_id"]}', call_kwargs['html'])


# ---------------------------------------------------------------------------
# Event-scoped notification routing
# ---------------------------------------------------------------------------

class EventRoutingSetupMixin(NotificationSetupMixin):
    """Extends shared setup with an Event + event-scoped routes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.event = Event.objects.create(
            hotel=cls.hotel,
            department=cls.dept,
            name='Wine Tasting',
            event_start=timezone.now() + timedelta(days=1),
            status='PUBLISHED',
        )


class EventRouteAPITest(EventRoutingSetupMixin, TestCase):
    """CRUD and filtering for event-scoped notification routes."""

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin_user)
        self.base_url = '/api/v1/hotels/test-hotel/admin/notification-routes/'

    def test_create_event_route(self):
        resp = self.client.post(self.base_url, {
            'event': self.event.id,
            'channel': 'EMAIL',
            'target': 'sommelier@hotel.com',
            'label': 'Sommelier',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        route = NotificationRoute.objects.get(id=resp.json()['id'])
        self.assertEqual(route.event_id, self.event.id)
        self.assertIsNone(route.department_id)

    def test_create_dept_route_still_works(self):
        resp = self.client.post(self.base_url, {
            'department': self.dept.id,
            'channel': 'WHATSAPP',
            'target': '919876543210',
            'label': 'Dept Staff',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        route = NotificationRoute.objects.get(id=resp.json()['id'])
        self.assertIsNone(route.event_id)
        self.assertEqual(route.department_id, self.dept.id)

    def test_create_rejects_both_scope(self):
        resp = self.client.post(self.base_url, {
            'department': self.dept.id,
            'event': self.event.id,
            'channel': 'EMAIL',
            'target': 'both@hotel.com',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_create_rejects_neither_scope(self):
        resp = self.client.post(self.base_url, {
            'channel': 'EMAIL',
            'target': 'neither@hotel.com',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_create_event_route_rejects_experience(self):
        resp = self.client.post(self.base_url, {
            'event': self.event.id,
            'experience': self.experience.id,
            'channel': 'EMAIL',
            'target': 'bad@hotel.com',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('experience', resp.json())

    def test_filter_by_event(self):
        NotificationRoute.objects.create(
            hotel=self.hotel, event=self.event,
            channel='EMAIL', target='evt@hotel.com',
            created_by=self.admin_user,
        )
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='EMAIL', target='dept@hotel.com',
            created_by=self.admin_user,
        )
        resp = self.client.get(f'{self.base_url}?event={self.event.id}')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['event'], self.event.id)

    def test_filter_both_params_rejected(self):
        resp = self.client.get(
            f'{self.base_url}?department={self.dept.id}&event={self.event.id}'
        )
        self.assertEqual(resp.status_code, 400)

    def test_filter_event_invalid_returns_empty(self):
        NotificationRoute.objects.create(
            hotel=self.hotel, event=self.event,
            channel='EMAIL', target='evt@hotel.com',
            created_by=self.admin_user,
        )
        resp = self.client.get(f'{self.base_url}?event=abc')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_duplicate_dept_route_returns_400(self):
        """Duplicate (dept, channel, target) must return 400, not 500."""
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='EMAIL', target='dup@hotel.com',
            created_by=self.admin_user,
        )
        resp = self.client.post(self.base_url, {
            'department': self.dept.id,
            'channel': 'EMAIL',
            'target': 'dup@hotel.com',
            'label': 'Dup',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_event_route_returns_400(self):
        """Duplicate (event, channel, target) must return 400, not 500."""
        NotificationRoute.objects.create(
            hotel=self.hotel, event=self.event,
            channel='WHATSAPP', target='919876543210',
            created_by=self.admin_user,
        )
        resp = self.client.post(self.base_url, {
            'event': self.event.id,
            'channel': 'WHATSAPP',
            'target': '919876543210',
            'label': 'Dup',
        }, format='json')
        self.assertEqual(resp.status_code, 400)


class EventRoutePushTest(EventRoutingSetupMixin, TestCase):
    """PushAdapter respects notify_department toggle."""

    def setUp(self):
        self.adapter = PushAdapter()

    def test_notify_dept_true_includes_dept_staff(self):
        """Default notify_department=True → dept staff + admins."""
        self.event.notify_department = True
        self.event.save(update_fields=['notify_department'])
        req = self._make_request(event=self.event)
        event = self._make_event(request=req, event_obj=self.event)
        recipients = self.adapter.get_recipients(event)
        user_ids = {m.user_id for m in recipients}
        self.assertIn(self.staff_user.id, user_ids)
        self.assertIn(self.admin_user.id, user_ids)
        self.assertIn(self.superadmin_user.id, user_ids)

    def test_notify_dept_false_excludes_dept_staff(self):
        """notify_department=False → admins only, no dept staff."""
        self.event.notify_department = False
        self.event.save(update_fields=['notify_department'])
        req = self._make_request(event=self.event)
        event = self._make_event(request=req, event_obj=self.event)
        recipients = self.adapter.get_recipients(event)
        user_ids = {m.user_id for m in recipients}
        self.assertNotIn(self.staff_user.id, user_ids)
        self.assertIn(self.admin_user.id, user_ids)
        self.assertIn(self.superadmin_user.id, user_ids)


class EventRouteWhatsAppTest(EventRoutingSetupMixin, TestCase):
    """WhatsAppAdapter routes with event-scoped + dept-scoped routes."""

    def setUp(self):
        self.adapter = WhatsAppAdapter()
        # Department-wide WA route
        self.dept_route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            label='Dept Staff', created_by=self.admin_user,
        )
        # Event-specific WA route
        self.event_route = NotificationRoute.objects.create(
            hotel=self.hotel, event=self.event,
            channel='WHATSAPP', target='919111222333',
            label='Sommelier', created_by=self.admin_user,
        )

    def test_both_routes_when_notify_dept_true(self):
        self.event.notify_department = True
        self.event.save(update_fields=['notify_department'])
        req = self._make_request(event=self.event)
        event = self._make_event(request=req, event_obj=self.event)
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertIn('919876543210', targets)
        self.assertIn('919111222333', targets)

    def test_only_event_routes_when_notify_dept_false(self):
        self.event.notify_department = False
        self.event.save(update_fields=['notify_department'])
        req = self._make_request(event=self.event)
        event = self._make_event(request=req, event_obj=self.event)
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertNotIn('919876543210', targets)
        self.assertIn('919111222333', targets)

    def test_dedup_same_target_across_scopes(self):
        """Same phone in dept route + event route → single recipient."""
        self.event_route.target = '919876543210'
        self.event_route.save()
        self.event.notify_department = True
        self.event.save(update_fields=['notify_department'])
        req = self._make_request(event=self.event)
        event = self._make_event(request=req, event_obj=self.event)
        recipients = self.adapter.get_recipients(event)
        targets = [r.target for r in recipients]
        self.assertEqual(targets.count('919876543210'), 1)


@override_settings(RESEND_API_KEY='re_test')
class EventRouteEmailTest(EventRoutingSetupMixin, TestCase):
    """EmailAdapter routes with event-scoped + dept-scoped routes."""

    def setUp(self):
        self.adapter = EmailAdapter()
        self.hotel.email_notifications_enabled = True
        self.hotel.save(update_fields=['email_notifications_enabled'])
        # Department-wide email route
        self.dept_route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='EMAIL', target='dept@hotel.com',
            label='Dept Manager', created_by=self.admin_user,
        )
        # Event-specific email route
        self.event_route = NotificationRoute.objects.create(
            hotel=self.hotel, event=self.event,
            channel='EMAIL', target='event@hotel.com',
            label='Event Coord', created_by=self.admin_user,
        )

    def test_both_routes_when_notify_dept_true(self):
        self.event.notify_department = True
        self.event.save(update_fields=['notify_department'])
        req = self._make_request(event=self.event)
        event = self._make_event(request=req, event_obj=self.event)
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertIn('dept@hotel.com', targets)
        self.assertIn('event@hotel.com', targets)

    def test_only_event_routes_when_notify_dept_false(self):
        self.event.notify_department = False
        self.event.save(update_fields=['notify_department'])
        req = self._make_request(event=self.event)
        event = self._make_event(request=req, event_obj=self.event)
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertNotIn('dept@hotel.com', targets)
        self.assertIn('event@hotel.com', targets)

    def test_no_event_obj_uses_dept_routes_only(self):
        """Regular dept request (no event_obj) → only dept routes."""
        req = self._make_request()
        event = self._make_event(request=req)
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertIn('dept@hotel.com', targets)
        self.assertNotIn('event@hotel.com', targets)


# ---------------------------------------------------------------------------
# OncallAdapter
# ---------------------------------------------------------------------------

from concierge.notifications.oncall import OncallAdapter, OncallTarget


class OncallAdapterEnabledTest(NotificationSetupMixin, TestCase):
    """is_enabled() checks escalation_fallback_channel + contact info."""

    def setUp(self):
        self.adapter = OncallAdapter()

    def test_disabled_when_channel_none(self):
        self.hotel.escalation_fallback_channel = 'NONE'
        self.hotel.oncall_email = 'oncall@hotel.com'
        self.hotel.save(update_fields=['escalation_fallback_channel', 'oncall_email'])
        self.assertFalse(self.adapter.is_enabled(self.hotel))

    def test_disabled_when_no_contacts(self):
        self.hotel.escalation_fallback_channel = 'EMAIL'
        self.hotel.oncall_email = ''
        self.hotel.oncall_phone = ''
        self.hotel.save(update_fields=['escalation_fallback_channel', 'oncall_email', 'oncall_phone'])
        self.assertFalse(self.adapter.is_enabled(self.hotel))

    def test_enabled_with_email_channel_and_email(self):
        self.hotel.escalation_fallback_channel = 'EMAIL'
        self.hotel.oncall_email = 'oncall@hotel.com'
        self.hotel.save(update_fields=['escalation_fallback_channel', 'oncall_email'])
        self.assertTrue(self.adapter.is_enabled(self.hotel))

    def test_enabled_with_whatsapp_channel_and_phone(self):
        self.hotel.escalation_fallback_channel = 'WHATSAPP'
        self.hotel.oncall_phone = '+919999999999'
        self.hotel.save(update_fields=['escalation_fallback_channel', 'oncall_phone'])
        self.assertTrue(self.adapter.is_enabled(self.hotel))

    def test_enabled_with_both_channel_and_only_email(self):
        """EMAIL_WHATSAPP with only email set — still enabled."""
        self.hotel.escalation_fallback_channel = 'EMAIL_WHATSAPP'
        self.hotel.oncall_email = 'oncall@hotel.com'
        self.hotel.oncall_phone = ''
        self.hotel.save(update_fields=['escalation_fallback_channel', 'oncall_email', 'oncall_phone'])
        self.assertTrue(self.adapter.is_enabled(self.hotel))


@override_settings(GUPSHUP_WA_API_KEY='test-key')
class OncallAdapterRecipientsTest(NotificationSetupMixin, TestCase):
    """get_recipients() only fires for escalation events."""

    def setUp(self):
        self.adapter = OncallAdapter()
        self.hotel.escalation_fallback_channel = 'EMAIL_WHATSAPP'
        self.hotel.oncall_email = 'oncall@hotel.com'
        self.hotel.oncall_phone = '+919999999999'
        self.hotel.save(update_fields=[
            'escalation_fallback_channel', 'oncall_email', 'oncall_phone',
        ])

    def test_returns_empty_for_request_created(self):
        event = self._make_event(event_type='request.created')
        self.assertEqual(self.adapter.get_recipients(event), [])

    def test_returns_empty_for_response_due(self):
        event = self._make_event(event_type='response_due')
        self.assertEqual(self.adapter.get_recipients(event), [])

    def test_returns_empty_for_daily_digest(self):
        event = NotificationEvent(
            event_type='daily_digest', hotel=self.hotel,
        )
        self.assertEqual(self.adapter.get_recipients(event), [])

    def test_returns_both_targets_for_escalation(self):
        event = self._make_event(event_type='escalation', escalation_tier=1)
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(len(recipients), 2)
        channels = {r.channel for r in recipients}
        self.assertEqual(channels, {'EMAIL', 'WHATSAPP'})

    def test_email_only_channel(self):
        self.hotel.escalation_fallback_channel = 'EMAIL'
        self.hotel.save(update_fields=['escalation_fallback_channel'])
        event = self._make_event(event_type='escalation', escalation_tier=1)
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].channel, 'EMAIL')
        self.assertEqual(recipients[0].target, 'oncall@hotel.com')

    def test_whatsapp_only_channel(self):
        self.hotel.escalation_fallback_channel = 'WHATSAPP'
        self.hotel.save(update_fields=['escalation_fallback_channel'])
        event = self._make_event(event_type='escalation', escalation_tier=1)
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].channel, 'WHATSAPP')
        self.assertEqual(recipients[0].target, '919999999999')

    def test_whatsapp_skipped_without_api_key(self):
        """WHATSAPP channel but no Gupshup key → no WA target."""
        with self.settings(GUPSHUP_WA_API_KEY=''):
            event = self._make_event(event_type='escalation', escalation_tier=1)
            recipients = self.adapter.get_recipients(event)
            channels = {r.channel for r in recipients}
            self.assertNotIn('WHATSAPP', channels)
            # EMAIL should still be present
            self.assertIn('EMAIL', channels)

    def test_email_whatsapp_with_only_phone(self):
        """EMAIL_WHATSAPP with no email → only WhatsApp target."""
        self.hotel.oncall_email = ''
        self.hotel.save(update_fields=['oncall_email'])
        event = self._make_event(event_type='escalation', escalation_tier=1)
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].channel, 'WHATSAPP')

    def test_skips_target_already_covered_by_route_adapter(self):
        """On-call email matches an existing route DeliveryRecord → deduplicated."""
        req = self._make_request()
        # Simulate a route-based EmailAdapter having already created a record
        DeliveryRecord.objects.create(
            idempotency_key=f"email:escalation:{req.public_id}:1:99",
            hotel=self.hotel,
            request=req,
            channel="EMAIL",
            target="oncall@hotel.com",
            event_type="escalation",
            status=DeliveryRecord.Status.QUEUED,
            message_type="TEMPLATE",
        )
        event = self._make_event(request=req, event_type='escalation', escalation_tier=1)
        recipients = self.adapter.get_recipients(event)
        # Email target should be filtered out; WhatsApp should remain
        channels = {r.channel for r in recipients}
        self.assertNotIn('EMAIL', channels)
        self.assertIn('WHATSAPP', channels)

    def test_skips_whatsapp_target_already_covered_by_route(self):
        """On-call phone matches an existing route DeliveryRecord → deduplicated."""
        req = self._make_request()
        # Route targets are digits-only (NotificationRoute.save() strips non-digits)
        DeliveryRecord.objects.create(
            idempotency_key=f"wa:escalation:{req.public_id}:2:42",
            hotel=self.hotel,
            request=req,
            channel="WHATSAPP",
            target="919999999999",
            event_type="escalation",
            status=DeliveryRecord.Status.QUEUED,
            message_type="TEMPLATE",
        )
        event = self._make_event(request=req, event_type='escalation', escalation_tier=2)
        recipients = self.adapter.get_recipients(event)
        channels = {r.channel for r in recipients}
        self.assertNotIn('WHATSAPP', channels)
        self.assertIn('EMAIL', channels)

    def test_different_tier_not_deduplicated(self):
        """Route record at tier 1 does NOT suppress on-call at tier 2."""
        req = self._make_request()
        DeliveryRecord.objects.create(
            idempotency_key=f"email:escalation:{req.public_id}:1:99",
            hotel=self.hotel,
            request=req,
            channel="EMAIL",
            target="oncall@hotel.com",
            event_type="escalation",
            status=DeliveryRecord.Status.QUEUED,
            message_type="TEMPLATE",
        )
        event = self._make_event(request=req, event_type='escalation', escalation_tier=2)
        recipients = self.adapter.get_recipients(event)
        # Tier 2 should NOT be suppressed by a tier 1 record
        channels = {r.channel for r in recipients}
        self.assertIn('EMAIL', channels)
        self.assertIn('WHATSAPP', channels)

    def test_both_targets_covered_returns_empty(self):
        """Both on-call targets already covered by routes → empty list."""
        req = self._make_request()
        DeliveryRecord.objects.create(
            idempotency_key=f"email:escalation:{req.public_id}:1:99",
            hotel=self.hotel, request=req, channel="EMAIL",
            target="oncall@hotel.com", event_type="escalation",
            status=DeliveryRecord.Status.QUEUED, message_type="TEMPLATE",
        )
        DeliveryRecord.objects.create(
            idempotency_key=f"wa:escalation:{req.public_id}:1:42",
            hotel=self.hotel, request=req, channel="WHATSAPP",
            target="919999999999", event_type="escalation",
            status=DeliveryRecord.Status.QUEUED, message_type="TEMPLATE",
        )
        event = self._make_event(request=req, event_type='escalation', escalation_tier=1)
        recipients = self.adapter.get_recipients(event)
        self.assertEqual(recipients, [])

    def test_phone_normalization_enables_dedupe(self):
        """hotel.oncall_phone='+919...' matches route target='919...' after normalization."""
        req = self._make_request()
        # Route adapter stores digits-only (NotificationRoute.save() strips non-digits)
        DeliveryRecord.objects.create(
            idempotency_key=f"wa:escalation:{req.public_id}:1:42",
            hotel=self.hotel, request=req, channel="WHATSAPP",
            target="919999999999", event_type="escalation",
            status=DeliveryRecord.Status.QUEUED, message_type="TEMPLATE",
        )
        # Hotel field stores +91... (CharField, no auto-normalization)
        self.assertEqual(self.hotel.oncall_phone, '+919999999999')
        event = self._make_event(request=req, event_type='escalation', escalation_tier=1)
        recipients = self.adapter.get_recipients(event)
        # OncallAdapter normalizes before comparing → should dedupe
        channels = {r.channel for r in recipients}
        self.assertNotIn('WHATSAPP', channels)


@override_settings(GUPSHUP_WA_API_KEY='test-key')
class OncallAdapterSendTest(NotificationSetupMixin, TestCase):
    """send() creates DeliveryRecord and dispatches Celery task."""

    def setUp(self):
        self.adapter = OncallAdapter()
        self.hotel.escalation_fallback_channel = 'EMAIL_WHATSAPP'
        self.hotel.oncall_email = 'oncall@hotel.com'
        self.hotel.oncall_phone = '+919999999999'
        self.hotel.save(update_fields=[
            'escalation_fallback_channel', 'oncall_email', 'oncall_phone',
        ])

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    def test_email_send_creates_delivery_record(self, mock_delay):
        req = self._make_request()
        event = self._make_event(request=req, event_type='escalation', escalation_tier=2)
        target = OncallTarget(channel='EMAIL', target='oncall@hotel.com')

        record = self.adapter.send(target, event)

        self.assertIsInstance(record, DeliveryRecord)
        self.assertIsNone(record.route)
        self.assertEqual(record.channel, 'EMAIL')
        self.assertEqual(record.target, 'oncall@hotel.com')
        self.assertEqual(record.event_type, 'escalation')
        self.assertEqual(record.status, DeliveryRecord.Status.QUEUED)
        self.assertIn('oncall:email:', record.idempotency_key)
        self.assertIn(':2', record.idempotency_key)
        mock_delay.assert_called_once()

    @patch('concierge.notifications.tasks.send_whatsapp_template_notification.delay')
    def test_whatsapp_send_template_no_window(self, mock_delay):
        req = self._make_request()
        event = self._make_event(request=req, event_type='escalation', escalation_tier=1)
        target = OncallTarget(channel='WHATSAPP', target='919999999999')

        record = self.adapter.send(target, event)

        self.assertIsInstance(record, DeliveryRecord)
        self.assertIsNone(record.route)
        self.assertEqual(record.channel, 'WHATSAPP')
        self.assertEqual(record.message_type, 'TEMPLATE')
        mock_delay.assert_called_once()

    @patch('concierge.notifications.tasks.send_whatsapp_session_notification.delay')
    def test_whatsapp_send_session_with_active_window(self, mock_delay):
        WhatsAppServiceWindow.objects.create(
            hotel=self.hotel, phone='919999999999',
            last_inbound_at=timezone.now(),
        )
        req = self._make_request()
        event = self._make_event(request=req, event_type='escalation', escalation_tier=1)
        target = OncallTarget(channel='WHATSAPP', target='919999999999')

        record = self.adapter.send(target, event)

        self.assertEqual(record.message_type, 'SESSION')
        mock_delay.assert_called_once()

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    def test_idempotency_prevents_duplicate(self, mock_delay):
        req = self._make_request()
        event = self._make_event(request=req, event_type='escalation', escalation_tier=1)
        target = OncallTarget(channel='EMAIL', target='oncall@hotel.com')

        record1 = self.adapter.send(target, event)
        record2 = self.adapter.send(target, event)

        self.assertEqual(record1.id, record2.id)
        mock_delay.assert_called_once()  # Only dispatched once

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    def test_different_tiers_create_separate_records(self, mock_delay):
        req = self._make_request()
        target = OncallTarget(channel='EMAIL', target='oncall@hotel.com')

        event1 = self._make_event(request=req, event_type='escalation', escalation_tier=1)
        record1 = self.adapter.send(target, event1)

        event2 = self._make_event(request=req, event_type='escalation', escalation_tier=2)
        record2 = self.adapter.send(target, event2)

        self.assertNotEqual(record1.id, record2.id)
        self.assertEqual(mock_delay.call_count, 2)

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    def test_params_include_escalation_tier(self, mock_delay):
        req = self._make_request()
        event = self._make_event(request=req, event_type='escalation', escalation_tier=3)
        target = OncallTarget(channel='EMAIL', target='oncall@hotel.com')

        self.adapter.send(target, event)

        params = mock_delay.call_args[0][1]
        self.assertEqual(params['escalation_tier'], 3)
        self.assertEqual(params['hotel_name'], 'Test Hotel')
        self.assertEqual(params['room_number'], '101')


@override_settings(GUPSHUP_WA_API_KEY='test-key')
class OncallDispatchIntegrationTest(NotificationSetupMixin, TestCase):
    """End-to-end: dispatch_notification routes escalation to OncallAdapter."""

    def setUp(self):
        self.hotel.escalation_fallback_channel = 'EMAIL'
        self.hotel.oncall_email = 'oncall@hotel.com'
        self.hotel.save(update_fields=['escalation_fallback_channel', 'oncall_email'])

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    @patch('concierge.notifications.tasks.send_push_notification_task.delay')
    def test_escalation_dispatches_to_oncall(self, mock_push, mock_email):
        req = self._make_request()
        event = self._make_event(request=req, event_type='escalation', escalation_tier=1)

        dispatch_notification(event)

        # OncallAdapter should have created a DeliveryRecord
        oncall_records = DeliveryRecord.objects.filter(
            idempotency_key__startswith='oncall:',
        )
        self.assertEqual(oncall_records.count(), 1)
        record = oncall_records.first()
        self.assertEqual(record.target, 'oncall@hotel.com')
        self.assertEqual(record.channel, 'EMAIL')
        self.assertIsNone(record.route)

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    @patch('concierge.notifications.tasks.send_push_notification_task.delay')
    def test_request_created_does_not_dispatch_to_oncall(self, mock_push, mock_email):
        req = self._make_request()
        event = self._make_event(request=req, event_type='request.created')

        dispatch_notification(event)

        oncall_records = DeliveryRecord.objects.filter(
            idempotency_key__startswith='oncall:',
        )
        self.assertEqual(oncall_records.count(), 0)

    @patch('concierge.notifications.tasks.send_email_notification.delay')
    @patch('concierge.notifications.tasks.send_push_notification_task.delay')
    def test_oncall_disabled_does_not_dispatch(self, mock_push, mock_email):
        self.hotel.escalation_fallback_channel = 'NONE'
        self.hotel.save(update_fields=['escalation_fallback_channel'])

        req = self._make_request()
        event = self._make_event(request=req, event_type='escalation', escalation_tier=1)

        dispatch_notification(event)

        oncall_records = DeliveryRecord.objects.filter(
            idempotency_key__startswith='oncall:',
        )
        self.assertEqual(oncall_records.count(), 0)
