"""Tests for staff invitation notification flow.

Covers:
- WhatsApp sender (happy path, malformed phone, network error, 5xx, 4xx)
- Email sender (happy path, permanent errors for all 4xx types, transient errors)
- Orchestrator (both succeed, partial success, partial permanent, both transient)
- Celery task (retry with resolved_channels, retries exhausted)
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from concierge.services import (
    _normalize_phone,
    send_staff_invite_whatsapp,
    send_staff_invite_email,
    send_staff_invite_notification,
)


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------

class NormalizePhoneTest(TestCase):
    def test_strips_non_digits(self):
        self.assertEqual(_normalize_phone('+91 918-755-1736'), '919187551736')

    def test_already_digits(self):
        self.assertEqual(_normalize_phone('919187551736'), '919187551736')

    def test_empty(self):
        self.assertEqual(_normalize_phone(''), '')


# ---------------------------------------------------------------------------
# WhatsApp sender
# ---------------------------------------------------------------------------

@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919187551736',
    GUPSHUP_WA_APP_NAME='refuje',
    GUPSHUP_WA_STAFF_INVITE_TEMPLATE_ID='template-uuid',
)
class SendStaffInviteWhatsAppTest(TestCase):

    @patch('concierge.services.http_requests.post')
    def test_happy_path(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'msg-123'}
        mock_post.return_value = mock_resp

        result = send_staff_invite_whatsapp('+91 9876543210', 'John', 'Test Hotel', 'Staff')
        self.assertTrue(result)

        # Verify payload
        call_kwargs = mock_post.call_args
        self.assertEqual(call_kwargs.kwargs['headers'], {'apikey': 'test-key'})
        payload = call_kwargs.kwargs['data']
        self.assertEqual(payload['destination'], '919876543210')

    @patch('concierge.services.http_requests.post')
    def test_malformed_phone_too_short(self, mock_post):
        result = send_staff_invite_whatsapp('123', 'John', 'Test Hotel', 'Staff')
        self.assertFalse(result)
        mock_post.assert_not_called()

    @override_settings(GUPSHUP_WA_API_KEY='', GUPSHUP_WA_STAFF_INVITE_TEMPLATE_ID='')
    def test_missing_config(self):
        result = send_staff_invite_whatsapp('+919876543210', 'John', 'Test Hotel', 'Staff')
        self.assertFalse(result)

    @patch('concierge.services.http_requests.post')
    def test_network_error_propagates(self, mock_post):
        mock_post.side_effect = ConnectionError('timeout')
        with self.assertRaises(ConnectionError):
            send_staff_invite_whatsapp('+919876543210', 'John', 'Test Hotel', 'Staff')

    @patch('concierge.services.http_requests.post')
    def test_5xx_raises_for_retry(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.json.return_value = {'message': 'Service Unavailable'}
        mock_post.return_value = mock_resp

        with self.assertRaises(RuntimeError) as ctx:
            send_staff_invite_whatsapp('+919876543210', 'John', 'Test Hotel', 'Staff')
        self.assertIn('503', str(ctx.exception))

    @patch('concierge.services.http_requests.post')
    def test_4xx_returns_false(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {'message': 'Bad Request'}
        mock_post.return_value = mock_resp

        result = send_staff_invite_whatsapp('+919876543210', 'John', 'Test Hotel', 'Staff')
        self.assertFalse(result)

    @patch('concierge.services.http_requests.post')
    def test_4xx_non_json_returns_false(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.side_effect = ValueError('No JSON')
        mock_resp.text = 'Bad Request'
        mock_post.return_value = mock_resp

        result = send_staff_invite_whatsapp('+919876543210', 'John', 'Test Hotel', 'Staff')
        self.assertFalse(result)

    @patch('concierge.services.http_requests.post')
    def test_empty_first_name_fallback(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'status': 'submitted', 'messageId': 'msg-456'}
        mock_post.return_value = mock_resp

        result = send_staff_invite_whatsapp('+919876543210', '', 'Test Hotel', 'Staff')
        self.assertTrue(result)

        import json
        payload = mock_post.call_args.kwargs['data']
        template = json.loads(payload['template'])
        self.assertEqual(template['params'][1], 'there')


# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------

@override_settings(
    RESEND_API_KEY='re_test_key',
    RESEND_FROM_EMAIL='Test <test@test.com>',
    FRONTEND_ORIGIN='https://refuje.com',
)
class SendStaffInviteEmailTest(TestCase):

    def _make_user(self, email='john@example.com', first_name='John'):
        user = MagicMock()
        user.email = email
        user.first_name = first_name
        user.pk = 1
        return user

    @patch('concierge.services.generate_password_token', return_value=('dWlk', 'tok-123'))
    @patch('resend.Emails.send')
    def test_happy_path(self, mock_send, _mock_token):
        mock_send.return_value = {'id': 'email-123'}

        result = send_staff_invite_email(self._make_user(), 'Test Hotel', 'Staff')
        self.assertTrue(result)
        mock_send.assert_called_once()

        call_args = mock_send.call_args[0][0]
        self.assertEqual(call_args['to'], ['john@example.com'])
        self.assertIn('Test Hotel', call_args['subject'])

    @override_settings(RESEND_API_KEY='')
    def test_missing_api_key(self):
        result = send_staff_invite_email(self._make_user(), 'Test Hotel', 'Staff')
        self.assertFalse(result)

    @patch('concierge.services.generate_password_token', return_value=('dWlk', 'tok-123'))
    @patch('resend.Emails.send')
    def test_permanent_validation_error(self, mock_send, _mock_token):
        from resend.exceptions import ValidationError
        mock_send.side_effect = ValidationError('invalid email', 400, 'validation_error')

        result = send_staff_invite_email(self._make_user(email='bad@'), 'Test Hotel', 'Staff')
        self.assertFalse(result)

    @patch('concierge.services.generate_password_token', return_value=('dWlk', 'tok-123'))
    @patch('resend.Emails.send')
    def test_permanent_missing_api_key_error(self, mock_send, _mock_token):
        from resend.exceptions import MissingApiKeyError
        mock_send.side_effect = MissingApiKeyError('missing key', 401, 'missing_api_key')

        result = send_staff_invite_email(self._make_user(), 'Test Hotel', 'Staff')
        self.assertFalse(result)

    @patch('concierge.services.generate_password_token', return_value=('dWlk', 'tok-123'))
    @patch('resend.Emails.send')
    def test_permanent_invalid_api_key_error(self, mock_send, _mock_token):
        from resend.exceptions import InvalidApiKeyError
        mock_send.side_effect = InvalidApiKeyError('bad key', 403, 'invalid_api_key')

        result = send_staff_invite_email(self._make_user(), 'Test Hotel', 'Staff')
        self.assertFalse(result)

    @patch('concierge.services.generate_password_token', return_value=('dWlk', 'tok-123'))
    @patch('resend.Emails.send')
    def test_permanent_missing_fields_error(self, mock_send, _mock_token):
        from resend.exceptions import MissingRequiredFieldsError
        mock_send.side_effect = MissingRequiredFieldsError(
            'missing fields', 422, 'missing_required_fields',
        )

        result = send_staff_invite_email(self._make_user(), 'Test Hotel', 'Staff')
        self.assertFalse(result)

    @patch('concierge.services.generate_password_token', return_value=('dWlk', 'tok-123'))
    @patch('resend.Emails.send')
    def test_transient_rate_limit_propagates(self, mock_send, _mock_token):
        from resend.exceptions import RateLimitError
        mock_send.side_effect = RateLimitError('rate limited', 429, 'rate_limit_exceeded')

        with self.assertRaises(RateLimitError):
            send_staff_invite_email(self._make_user(), 'Test Hotel', 'Staff')

    @patch('concierge.services.generate_password_token', return_value=('dWlk', 'tok-123'))
    @patch('resend.Emails.send')
    def test_transient_application_error_propagates(self, mock_send, _mock_token):
        from resend.exceptions import ApplicationError
        mock_send.side_effect = ApplicationError('server error', 500, 'application_error')

        with self.assertRaises(ApplicationError):
            send_staff_invite_email(self._make_user(), 'Test Hotel', 'Staff')

    @patch('concierge.services.generate_password_token', return_value=('dWlk', 'tok-123'))
    @patch('resend.Emails.send')
    def test_unknown_resend_error_propagates(self, mock_send, _mock_token):
        from resend.exceptions import ResendError
        mock_send.side_effect = ResendError('unknown', 499, 'unknown_type', 'retry')

        with self.assertRaises(ResendError):
            send_staff_invite_email(self._make_user(), 'Test Hotel', 'Staff')


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919187551736',
    GUPSHUP_WA_APP_NAME='refuje',
    GUPSHUP_WA_STAFF_INVITE_TEMPLATE_ID='template-uuid',
    RESEND_API_KEY='re_test_key',
    RESEND_FROM_EMAIL='Test <test@test.com>',
)
class SendStaffInviteNotificationTest(TestCase):

    def _make_user(self, phone='', email='', first_name='John'):
        user = MagicMock()
        user.phone = phone
        user.email = email
        user.first_name = first_name
        return user

    def _make_hotel(self, name='Test Hotel'):
        hotel = MagicMock()
        hotel.name = name
        return hotel

    @patch('concierge.services.send_staff_invite_email', return_value=True)
    @patch('concierge.services.send_staff_invite_whatsapp', return_value=True)
    def test_both_succeed(self, mock_wa, mock_email):
        user = self._make_user(phone='+919876543210', email='john@example.com')
        hotel = self._make_hotel()

        resolved = send_staff_invite_notification(user, hotel, 'STAFF')
        self.assertEqual(resolved, {'whatsapp', 'email'})
        mock_wa.assert_called_once()
        mock_email.assert_called_once()

    @patch('concierge.services.send_staff_invite_whatsapp', return_value=True)
    def test_phone_only(self, mock_wa):
        user = self._make_user(phone='+919876543210')
        hotel = self._make_hotel()

        resolved = send_staff_invite_notification(user, hotel, 'STAFF')
        self.assertEqual(resolved, {'whatsapp'})

    @patch('concierge.services.send_staff_invite_email', return_value=True)
    def test_email_only(self, mock_email):
        user = self._make_user(email='john@example.com')
        hotel = self._make_hotel()

        resolved = send_staff_invite_notification(user, hotel, 'STAFF')
        self.assertEqual(resolved, {'email'})

    @patch('concierge.services.send_staff_invite_email', return_value=True)
    @patch('concierge.services.send_staff_invite_whatsapp', side_effect=ConnectionError('timeout'))
    def test_wa_transient_email_succeeds(self, mock_wa, mock_email):
        user = self._make_user(phone='+919876543210', email='john@example.com')
        hotel = self._make_hotel()

        with self.assertRaises(ConnectionError):
            send_staff_invite_notification(user, hotel, 'STAFF')

        # Email was still attempted and succeeded
        mock_email.assert_called_once()

    @patch('concierge.services.send_staff_invite_email', return_value=True)
    @patch('concierge.services.send_staff_invite_whatsapp', side_effect=ConnectionError('timeout'))
    def test_wa_transient_resolved_channels_include_email(self, mock_wa, mock_email):
        user = self._make_user(phone='+919876543210', email='john@example.com')
        hotel = self._make_hotel()

        with self.assertRaises(ConnectionError) as ctx:
            send_staff_invite_notification(user, hotel, 'STAFF')
        self.assertEqual(ctx.exception._resolved_channels, {'email'})

    @patch('concierge.services.send_staff_invite_email', return_value=False)  # permanent
    @patch('concierge.services.send_staff_invite_whatsapp', side_effect=ConnectionError('timeout'))
    def test_wa_transient_email_permanent_both_in_resolved(self, mock_wa, mock_email):
        user = self._make_user(phone='+919876543210', email='john@example.com')
        hotel = self._make_hotel()

        with self.assertRaises(ConnectionError) as ctx:
            send_staff_invite_notification(user, hotel, 'STAFF')
        # Email is resolved (permanent fail), WA is unresolved (transient)
        self.assertEqual(ctx.exception._resolved_channels, {'email'})

    @patch('concierge.services.send_staff_invite_email', side_effect=RuntimeError('email transient'))
    @patch('concierge.services.send_staff_invite_whatsapp', side_effect=ConnectionError('wa transient'))
    def test_both_transient_resolved_empty(self, mock_wa, mock_email):
        user = self._make_user(phone='+919876543210', email='john@example.com')
        hotel = self._make_hotel()

        with self.assertRaises(ConnectionError) as ctx:
            send_staff_invite_notification(user, hotel, 'STAFF')
        self.assertEqual(ctx.exception._resolved_channels, set())

    @patch('concierge.services.send_staff_invite_email', return_value=True)
    @patch('concierge.services.send_staff_invite_whatsapp', return_value=True)
    def test_skip_channels_respected(self, mock_wa, mock_email):
        user = self._make_user(phone='+919876543210', email='john@example.com')
        hotel = self._make_hotel()

        resolved = send_staff_invite_notification(
            user, hotel, 'STAFF', skip_channels={'email'},
        )
        self.assertEqual(resolved, {'whatsapp', 'email'})
        mock_wa.assert_called_once()
        mock_email.assert_not_called()

    @patch('concierge.services.send_staff_invite_email', return_value=True)
    @patch('concierge.services.send_staff_invite_whatsapp', return_value=True)
    def test_skip_both_channels(self, mock_wa, mock_email):
        user = self._make_user(phone='+919876543210', email='john@example.com')
        hotel = self._make_hotel()

        resolved = send_staff_invite_notification(
            user, hotel, 'STAFF', skip_channels={'whatsapp', 'email'},
        )
        self.assertEqual(resolved, {'whatsapp', 'email'})
        mock_wa.assert_not_called()
        mock_email.assert_not_called()


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@override_settings(
    GUPSHUP_WA_API_KEY='test-key',
    GUPSHUP_WA_SOURCE_PHONE='919187551736',
    GUPSHUP_WA_APP_NAME='refuje',
    GUPSHUP_WA_STAFF_INVITE_TEMPLATE_ID='template-uuid',
    RESEND_API_KEY='re_test_key',
    RESEND_FROM_EMAIL='Test <test@test.com>',
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class SendStaffInviteNotificationTaskTest(TestCase):

    @patch('concierge.services.send_staff_invite_email', return_value=True)
    @patch('concierge.services.send_staff_invite_whatsapp', return_value=True)
    def test_happy_path(self, mock_wa, mock_email):
        from django.contrib.auth import get_user_model
        from concierge.models import Hotel
        User = get_user_model()

        user = User.objects.create(
            email='staff@example.com', phone='+919876543210',
            first_name='Test', user_type='STAFF',
        )
        user.set_unusable_password()
        user.save()

        hotel = Hotel.objects.create(name='Test Hotel', slug='test-hotel')

        from concierge.tasks import send_staff_invite_notification_task
        send_staff_invite_notification_task(user.id, hotel.id, 'STAFF')

        mock_wa.assert_called_once()
        mock_email.assert_called_once()

    @patch('concierge.services.send_staff_invite_email', return_value=True)
    @patch('concierge.services.send_staff_invite_whatsapp', return_value=True)
    def test_nonexistent_user_returns_early(self, mock_wa, mock_email):
        from concierge.tasks import send_staff_invite_notification_task
        send_staff_invite_notification_task(99999, 99999, 'STAFF')

        mock_wa.assert_not_called()
        mock_email.assert_not_called()

    @patch('concierge.services.send_staff_invite_email', return_value=True)
    @patch('concierge.services.send_staff_invite_whatsapp', side_effect=ConnectionError('wa down'))
    def test_partial_failure_passes_resolved_channels_on_retry(self, mock_wa, mock_email):
        from django.contrib.auth import get_user_model
        from concierge.models import Hotel
        from concierge.tasks import send_staff_invite_notification_task
        User = get_user_model()

        user = User.objects.create(
            email='staff2@example.com', phone='+919876543211',
            first_name='Test2', user_type='STAFF',
        )
        user.set_unusable_password()
        user.save()

        hotel = Hotel.objects.create(name='Test Hotel 2', slug='test-hotel-2')

        # Call the task directly — self.request.retries defaults to 0
        # Mock retry to capture kwargs instead of actually retrying
        task = send_staff_invite_notification_task
        with patch.object(task, 'retry', side_effect=Exception('retry called')) as mock_retry:
            try:
                task(user.id, hotel.id, 'STAFF')
            except Exception:
                pass

            mock_retry.assert_called_once()
            retry_kwargs = mock_retry.call_args.kwargs.get('kwargs', {})
            self.assertIn('email', retry_kwargs.get('resolved_channels', []))

    @patch('concierge.services.send_staff_invite_email', side_effect=RuntimeError('still down'))
    @patch('concierge.services.send_staff_invite_whatsapp', side_effect=ConnectionError('wa down'))
    def test_retries_exhausted_logs_error(self, mock_wa, mock_email):
        from django.contrib.auth import get_user_model
        from concierge.models import Hotel
        from concierge.tasks import send_staff_invite_notification_task
        User = get_user_model()

        user = User.objects.create(
            email='staff3@example.com', phone='+919876543212',
            first_name='Test3', user_type='STAFF',
        )
        user.set_unusable_password()
        user.save()

        hotel = Hotel.objects.create(name='Test Hotel 3', slug='test-hotel-3')

        # Push a request context with retries=2 (at max_retries)
        task = send_staff_invite_notification_task
        task.push_request(retries=2)
        try:
            with self.assertLogs('concierge.tasks', level='ERROR') as cm:
                # Should return (not raise) when retries exhausted
                task(user.id, hotel.id, 'STAFF')

            self.assertTrue(any('exhausted retries' in msg for msg in cm.output))
        finally:
            task.pop_request()
