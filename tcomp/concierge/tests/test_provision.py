"""Tests for hotel provisioning via Django admin.

Covers:
- HotelProvisionForm validation (required fields, slug, timezone, phone)
- provision_hotel() transaction (hotel, department, user find/create, membership)
- Permission enforcement on the admin view
- Template/URL wiring
"""
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.urls import reverse

from concierge.admin import HotelProvisionForm, provision_hotel, HotelAdmin
from concierge.models import Hotel, HotelMembership, Department, ContentStatus

User = get_user_model()


# ---------------------------------------------------------------------------
# Form validation
# ---------------------------------------------------------------------------

class HotelProvisionFormTest(TestCase):

    def _base_data(self, **overrides):
        data = {
            'name': 'Test Hotel',
            'timezone': 'Asia/Kolkata',
            'owner_email': 'owner@example.com',
            'owner_phone_code': '+91',
            'owner_phone_number': '',
        }
        data.update(overrides)
        return data

    def test_valid_minimal(self):
        form = HotelProvisionForm(data=self._base_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_phone_only(self):
        form = HotelProvisionForm(data=self._base_data(
            owner_email='', owner_phone_code='+91', owner_phone_number='9876543210',
        ))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['owner_phone'], '919876543210')

    def test_rejects_missing_email_and_phone(self):
        form = HotelProvisionForm(data=self._base_data(
            owner_email='', owner_phone_number='',
        ))
        self.assertFalse(form.is_valid())
        self.assertIn('At least one of owner email or phone', str(form.errors))

    def test_rejects_invalid_timezone(self):
        form = HotelProvisionForm(data=self._base_data(timezone='Fake/Zone'))
        self.assertFalse(form.is_valid())
        self.assertIn('timezone', form.errors)

    def test_rejects_short_combined_phone(self):
        """Country code + local number must produce 11-15 total digits."""
        form = HotelProvisionForm(data=self._base_data(
            owner_phone_code='+1', owner_phone_number='12345',
        ))
        self.assertFalse(form.is_valid())
        self.assertIn('owner_phone_number', form.errors)

    def test_rejects_long_combined_phone(self):
        """Country code (2) + 14 digits = 16 total — over 15 limit."""
        form = HotelProvisionForm(data=self._base_data(
            owner_phone_code='+91', owner_phone_number='12345678901234',
        ))
        self.assertFalse(form.is_valid())
        self.assertIn('owner_phone_number', form.errors)

    def test_accepts_valid_combined_phone(self):
        """+91 (2 digits) + 10-digit local = 12 total digits — valid."""
        form = HotelProvisionForm(data=self._base_data(
            owner_phone_code='+91', owner_phone_number='9876543210',
        ))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['owner_phone'], '919876543210')

    def test_slug_not_rejected_on_duplicate(self):
        """Duplicate slug should be accepted by the form; dedup happens in provision_hotel."""
        Hotel.objects.create(name='Existing', slug='existing')
        form = HotelProvisionForm(data=self._base_data(slug='existing'))
        self.assertTrue(form.is_valid(), form.errors)

    def test_slug_max_length_100(self):
        """Slug field respects Hotel.slug max_length=100."""
        long_slug = 'a' * 101
        form = HotelProvisionForm(data=self._base_data(slug=long_slug))
        self.assertFalse(form.is_valid())
        self.assertIn('slug', form.errors)

    def test_phone_normalization_strips_non_digits(self):
        """Non-digit characters in local number are stripped before combining."""
        form = HotelProvisionForm(data=self._base_data(
            owner_phone_code='+91', owner_phone_number='9876-543-210',
        ))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['owner_phone'], '919876543210')


# ---------------------------------------------------------------------------
# provision_hotel() logic
# ---------------------------------------------------------------------------

class ProvisionHotelTest(TestCase):

    def _base_data(self, **overrides):
        data = {
            'name': 'Seaside Inn',
            'slug': '',
            'city': 'Goa',
            'timezone': 'Asia/Kolkata',
            'owner_email': 'owner@seaside.com',
            'owner_phone': '919876543210',
            'owner_first_name': 'Raj',
            'owner_last_name': 'Kumar',
            'send_invite': False,
        }
        data.update(overrides)
        return data

    def test_creates_hotel_department_user_membership(self):
        hotel, user, membership, trace = provision_hotel(self._base_data())

        self.assertEqual(hotel.name, 'Seaside Inn')
        self.assertTrue(hotel.slug.startswith('seaside-inn'))
        self.assertEqual(hotel.city, 'Goa')
        self.assertEqual(hotel.timezone, 'Asia/Kolkata')
        self.assertTrue(hotel.settings_configured)

        # Fallback department
        dept = hotel.fallback_department
        self.assertIsNotNone(dept)
        self.assertEqual(dept.name, 'General')
        self.assertEqual(dept.icon, 'concierge-bell')
        self.assertFalse(dept.is_ops)
        self.assertEqual(dept.status, ContentStatus.PUBLISHED)

        # User
        self.assertEqual(user.email, 'owner@seaside.com')
        self.assertEqual(user.phone, '919876543210')
        self.assertEqual(user.user_type, 'STAFF')
        self.assertFalse(user.has_usable_password())

        # Membership
        self.assertEqual(membership.role, HotelMembership.Role.SUPERADMIN)
        self.assertEqual(membership.hotel, hotel)
        self.assertEqual(membership.user, user)

    def test_slug_auto_dedup(self):
        Hotel.objects.create(name='Dup', slug='seaside-inn')
        hotel, *_ = provision_hotel(self._base_data())
        self.assertNotEqual(hotel.slug, 'seaside-inn')
        self.assertTrue(hotel.slug.startswith('seaside-inn'))

    def test_explicit_slug_dedup(self):
        Hotel.objects.create(name='Dup', slug='my-slug')
        hotel, *_ = provision_hotel(self._base_data(slug='my-slug'))
        self.assertNotEqual(hotel.slug, 'my-slug')
        self.assertTrue(hotel.slug.startswith('my-slug'))

    def test_department_schedule_matches_hotel_timezone(self):
        hotel, *_ = provision_hotel(self._base_data(timezone='America/New_York'))
        self.assertEqual(hotel.fallback_department.schedule['timezone'], 'America/New_York')

    def test_reuses_existing_user_by_email(self):
        existing = User.objects.create_user(
            email='owner@seaside.com', first_name='Old',
        )
        existing.set_unusable_password()
        existing.save()

        hotel, user, membership, trace = provision_hotel(self._base_data())
        self.assertEqual(user.pk, existing.pk)
        self.assertTrue(any('Found existing user by email' in m for m in trace))

    def test_reuses_existing_user_by_phone(self):
        existing = User(email='', phone='919876543210', user_type='GUEST')
        existing.set_unusable_password()
        existing.save()

        hotel, user, membership, trace = provision_hotel(self._base_data(owner_email=''))
        self.assertEqual(user.pk, existing.pk)
        self.assertEqual(user.user_type, 'STAFF')  # promoted

    def test_promotes_guest_to_staff(self):
        guest = User(email='', phone='919876543210', user_type='GUEST')
        guest.set_unusable_password()
        guest.save()

        _, user, _, trace = provision_hotel(self._base_data(owner_email=''))
        self.assertEqual(user.user_type, 'STAFF')
        self.assertTrue(any('Promoted' in m for m in trace))

    def test_backfills_missing_fields(self):
        existing = User(email='owner@seaside.com', phone='', first_name='', user_type='STAFF')
        existing.set_unusable_password()
        existing.save()

        _, user, _, trace = provision_hotel(self._base_data())
        user.refresh_from_db()
        self.assertEqual(user.phone, '919876543210')
        self.assertEqual(user.first_name, 'Raj')
        self.assertEqual(user.last_name, 'Kumar')

    def test_reactivates_inactive_user(self):
        """Provisioning an inactive user should reactivate them."""
        inactive = User.objects.create_user(email='owner@seaside.com')
        inactive.is_active = False
        inactive.set_unusable_password()
        inactive.save()

        _, user, _, trace = provision_hotel(self._base_data())
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(any('Reactivated' in m for m in trace))

    def test_email_only_user_gets_temp_password(self):
        """Email-only new user gets a temp password as break-glass fallback."""
        _, user, _, trace = provision_hotel(self._base_data(owner_phone=''))
        self.assertTrue(user.has_usable_password())
        self.assertTrue(any('Temp password' in m for m in trace))

    def test_reused_email_only_user_gets_temp_password(self):
        """Reused email-only user with unusable password gets a temp password."""
        existing = User(email='owner@seaside.com', phone='', user_type='STAFF')
        existing.set_unusable_password()
        existing.save()

        _, user, _, trace = provision_hotel(self._base_data(owner_phone=''))
        user.refresh_from_db()
        self.assertEqual(user.pk, existing.pk)
        self.assertTrue(user.has_usable_password())
        self.assertTrue(any('Temp password' in m for m in trace))

    def test_reused_user_with_password_keeps_it(self):
        """Reused user who already has a usable password keeps it (no temp override)."""
        existing = User.objects.create_user(email='owner@seaside.com', password='existing-pw')
        existing.user_type = 'STAFF'
        existing.save()

        _, user, _, trace = provision_hotel(self._base_data(owner_phone=''))
        user.refresh_from_db()
        self.assertTrue(user.check_password('existing-pw'))
        self.assertFalse(any('Temp password' in m for m in trace))

    def test_phone_user_gets_unusable_password(self):
        """User with phone gets unusable password (they use phone OTP)."""
        _, user, _, trace = provision_hotel(self._base_data())
        self.assertFalse(user.has_usable_password())

    @patch('concierge.admin.transaction.on_commit')
    def test_invite_enqueued_when_enabled(self, mock_on_commit):
        provision_hotel(self._base_data(send_invite=True))
        mock_on_commit.assert_called_once()

    @patch('concierge.admin.transaction.on_commit')
    def test_invite_skipped_when_disabled(self, mock_on_commit):
        provision_hotel(self._base_data(send_invite=False))
        mock_on_commit.assert_not_called()


# ---------------------------------------------------------------------------
# Admin view permissions
# ---------------------------------------------------------------------------

class ProvisionViewPermissionTest(TestCase):

    def setUp(self):
        self.factory = RequestFactory()
        self.provision_url = reverse('admin:concierge_hotel_provision')

    def test_url_resolves(self):
        self.assertEqual(self.provision_url, '/admin/concierge/hotel/provision/')

    def test_anonymous_redirected(self):
        resp = self.client.get(self.provision_url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin/login/', resp.url)

    def test_staff_without_add_perm_gets_403(self):
        user = User.objects.create_user(email='viewer@test.com', password='pass')
        user.is_staff = True
        user.save()
        self.client.login(email='viewer@test.com', password='pass')
        resp = self.client.get(self.provision_url)
        self.assertEqual(resp.status_code, 403)

    def test_superuser_gets_200(self):
        User.objects.create_superuser(email='admin@test.com', password='pass')
        self.client.login(email='admin@test.com', password='pass')
        resp = self.client.get(self.provision_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Provision New Hotel')

    def test_add_only_user_redirects_to_changelist(self):
        """User with add-but-no-change permission should not land on 403 change page."""
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType
        ct = ContentType.objects.get_for_model(Hotel)
        add_perm = Permission.objects.get(content_type=ct, codename='add_hotel')

        user = User.objects.create_user(email='adder@test.com', password='pass')
        user.is_staff = True
        user.user_permissions.add(add_perm)
        user.save()
        self.client.login(email='adder@test.com', password='pass')

        resp = self.client.post(self.provision_url, {
            'name': 'Add Only Hotel',
            'timezone': 'Asia/Kolkata',
            'owner_email': 'owner@addonly.com',
            'owner_phone_code': '+91',
            'owner_phone_number': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/concierge/hotel/', resp.url)
        # Should redirect to changelist, not change page
        self.assertNotRegex(resp.url, r'/concierge/hotel/\d+/change/')


# ---------------------------------------------------------------------------
# Changelist button
# ---------------------------------------------------------------------------

class ChangelistButtonTest(TestCase):

    def test_provision_button_visible_for_superuser(self):
        User.objects.create_superuser(email='admin@test.com', password='pass')
        self.client.login(email='admin@test.com', password='pass')
        resp = self.client.get(reverse('admin:concierge_hotel_changelist'))
        self.assertContains(resp, 'Provision New Hotel')

    def test_provision_button_hidden_without_add_perm(self):
        user = User.objects.create_user(email='viewer@test.com', password='pass')
        user.is_staff = True
        user.save()
        self.client.login(email='viewer@test.com', password='pass')
        resp = self.client.get(reverse('admin:concierge_hotel_changelist'))
        self.assertNotContains(resp, 'Provision New Hotel', status_code=403)
