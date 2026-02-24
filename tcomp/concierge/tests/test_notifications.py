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
    Experience,
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
