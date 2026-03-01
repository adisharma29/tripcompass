"""Tests for team member invite API (POST /hotels/{slug}/admin/members/).

Covers:
- Email-only new user gets temp password in response
- Phone-only new user gets unusable password (no temp password)
- Email+phone new user gets unusable password (no temp password)
- Reused email-only user with unusable password gets temp password
- Reused user with existing password keeps it (no temp password)
- Case-insensitive email lookup with duplicate handling
- Resend invite temp-password gating (last_login guard)
"""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from concierge.models import Hotel, HotelMembership, Department, ContentStatus
from users.models import User


class MemberInviteTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.hotel = Hotel.objects.create(
            name='Test Hotel', slug='test-hotel', timezone='Asia/Kolkata',
        )
        dept = Department.objects.create(
            hotel=cls.hotel, name='General', icon='concierge-bell',
            status=ContentStatus.PUBLISHED,
        )
        cls.hotel.fallback_department = dept
        cls.hotel.save(update_fields=['fallback_department'])
        cls.dept = dept

        cls.superadmin = User.objects.create_user(
            email='super@test.com', password='pass', user_type='STAFF',
        )
        HotelMembership.objects.create(
            user=cls.superadmin, hotel=cls.hotel,
            role=HotelMembership.Role.SUPERADMIN,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.superadmin)
        self.url = f'/api/v1/hotels/{self.hotel.slug}/admin/members/'

    def _invite(self, **overrides):
        data = {
            'first_name': 'Test',
            'last_name': 'User',
            'role': 'STAFF',
            'department': self.dept.id,
        }
        data.update(overrides)
        # Remove empty-string email/phone — serializer uses default=''
        if 'email' in data and not data['email']:
            del data['email']
        if 'phone' in data and not data['phone']:
            del data['phone']
        return self.client.post(self.url, data, format='json')

    # ------------------------------------------------------------------
    # New user paths
    # ------------------------------------------------------------------

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_email_only_new_user_gets_temp_password(self, _mock_commit):
        resp = self._invite(email='newstaff@example.com')
        self.assertEqual(resp.status_code, 201)
        self.assertIn('temp_password', resp.data)
        self.assertTrue(len(resp.data['temp_password']) > 0)

        user = User.objects.get(email='newstaff@example.com')
        self.assertTrue(user.has_usable_password())
        self.assertTrue(user.check_password(resp.data['temp_password']))

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_phone_only_new_user_no_temp_password(self, _mock_commit):
        resp = self._invite(phone='919876543210')
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn('temp_password', resp.data)

        user = User.objects.get(phone='919876543210')
        self.assertFalse(user.has_usable_password())

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_email_and_phone_new_user_no_temp_password(self, _mock_commit):
        resp = self._invite(email='both@example.com', phone='919876500000')
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn('temp_password', resp.data)

        user = User.objects.get(email='both@example.com')
        self.assertFalse(user.has_usable_password())

    # ------------------------------------------------------------------
    # Reused user paths
    # ------------------------------------------------------------------

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_reused_email_only_user_unusable_pw_gets_temp(self, _mock_commit):
        existing = User(email='reuse@example.com', phone='', user_type='STAFF')
        existing.set_unusable_password()
        existing.save()

        resp = self._invite(email='reuse@example.com')
        self.assertEqual(resp.status_code, 201)
        self.assertIn('temp_password', resp.data)

        existing.refresh_from_db()
        self.assertTrue(existing.has_usable_password())
        self.assertTrue(existing.check_password(resp.data['temp_password']))

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_reused_user_with_password_keeps_it(self, _mock_commit):
        existing = User.objects.create_user(
            email='haspass@example.com', password='existing-pw', user_type='STAFF',
        )

        resp = self._invite(email='haspass@example.com')
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn('temp_password', resp.data)

        existing.refresh_from_db()
        self.assertTrue(existing.check_password('existing-pw'))

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_reused_user_with_phone_no_temp_password(self, _mock_commit):
        """Reused user who has phone should not get temp password even if email-only invite."""
        existing = User(
            email='hasphone@example.com', phone='919876500001', user_type='STAFF',
        )
        existing.set_unusable_password()
        existing.save()

        resp = self._invite(email='hasphone@example.com')
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn('temp_password', resp.data)

    # ------------------------------------------------------------------
    # Reactivation
    # ------------------------------------------------------------------

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_reused_inactive_user_reactivated(self, _mock_commit):
        """Inactive user should be reactivated on invite."""
        inactive = User.objects.create_user(
            email='inactive@example.com', password='pass', user_type='STAFF',
        )
        inactive.is_active = False
        inactive.save()

        resp = self._invite(email='inactive@example.com')
        self.assertEqual(resp.status_code, 201)

        inactive.refresh_from_db()
        self.assertTrue(inactive.is_active)

    # ------------------------------------------------------------------
    # Case-insensitive email lookup
    # ------------------------------------------------------------------

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_case_insensitive_email_lookup(self, _mock_commit):
        existing = User.objects.create_user(
            email='Mixed@Example.COM', password='pass', user_type='STAFF',
        )

        resp = self._invite(email='mixed@example.com')
        self.assertEqual(resp.status_code, 201)
        # Should reuse the existing user, not create a new one
        self.assertEqual(resp.data['user_id'], existing.id)

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_duplicate_case_variant_emails_uses_oldest(self, _mock_commit):
        """When multiple users match case-insensitively, use the oldest."""
        older = User.objects.create_user(
            email='DUP@example.com', password='pass', user_type='STAFF',
        )
        User.objects.create_user(
            email='dup@example.com', password='pass2', user_type='STAFF',
        )

        resp = self._invite(email='Dup@Example.com')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['user_id'], older.id)

    # ------------------------------------------------------------------
    # Resend invite — temp password gating
    # ------------------------------------------------------------------

    def _resend(self, membership_id):
        url = f'/api/v1/hotels/{self.hotel.slug}/admin/members/{membership_id}/resend-invite/'
        return self.client.post(url)

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_resend_email_only_pre_login_returns_temp_password(self, _mock_task):
        """Resend for email-only user who never logged in returns temp_password."""
        resp = self._invite(email='resend1@example.com')
        self.assertEqual(resp.status_code, 201)
        membership_id = resp.data['id']

        resend_resp = self._resend(membership_id)
        self.assertEqual(resend_resp.status_code, 200)
        self.assertIn('temp_password', resend_resp.data)
        self.assertTrue(len(resend_resp.data['temp_password']) > 0)

        # New temp password should be usable
        user = User.objects.get(email='resend1@example.com')
        self.assertTrue(user.check_password(resend_resp.data['temp_password']))

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_resend_email_only_post_login_no_temp_password(self, _mock_task):
        """Resend for email-only user who HAS logged in does not overwrite password."""
        resp = self._invite(email='resend2@example.com')
        self.assertEqual(resp.status_code, 201)
        membership_id = resp.data['id']

        # Simulate login
        user = User.objects.get(email='resend2@example.com')
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])

        resend_resp = self._resend(membership_id)
        self.assertEqual(resend_resp.status_code, 200)
        self.assertNotIn('temp_password', resend_resp.data)

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_resend_rotates_temp_password(self, _mock_task):
        """Each resend generates a fresh temp password (old one stops working)."""
        resp = self._invite(email='resend3@example.com')
        first_pw = resp.data['temp_password']

        membership_id = resp.data['id']
        resend_resp = self._resend(membership_id)
        second_pw = resend_resp.data['temp_password']

        self.assertNotEqual(first_pw, second_pw)

        user = User.objects.get(email='resend3@example.com')
        self.assertFalse(user.check_password(first_pw))
        self.assertTrue(user.check_password(second_pw))

    @patch('concierge.tasks.send_staff_invite_notification_task.delay')
    def test_resend_phone_user_no_temp_password(self, _mock_task):
        """Resend for user with phone does not return temp_password."""
        resp = self._invite(phone='919876500099')
        self.assertEqual(resp.status_code, 201)
        membership_id = resp.data['id']

        resend_resp = self._resend(membership_id)
        self.assertEqual(resend_resp.status_code, 200)
        self.assertNotIn('temp_password', resend_resp.data)

    @patch('concierge.tasks.send_staff_invite_notification_task.delay',
           side_effect=Exception('broker down'))
    def test_resend_broker_down_still_returns_temp_password(self, _mock_task):
        """If task enqueue fails, temp_password is still returned."""
        resp = self._invite(email='resend4@example.com')
        membership_id = resp.data['id']

        resend_resp = self._resend(membership_id)
        self.assertEqual(resend_resp.status_code, 200)
        self.assertIn('temp_password', resend_resp.data)
