"""Tests for event department routing hardening.

Covers:
- HotelSettingsSerializer: fallback_department read/write, fallback_department_name
- EventSerializer: publish guard blocks unroutable events
- EventPublicSerializer: is_routable field
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from concierge.models import Department, Event, Experience, Hotel, HotelMembership
from users.models import User


class EventRoutingTestBase(TestCase):
    """Shared setup: hotel, departments, admin + superadmin users."""

    @classmethod
    def setUpTestData(cls):
        cls.hotel = Hotel.objects.create(
            name='Routing Hotel', slug='routing-hotel',
        )
        cls.dept = Department.objects.create(
            hotel=cls.hotel, name='Concierge', slug='concierge',
        )
        cls.ops_dept = Department.objects.create(
            hotel=cls.hotel, name='Internal Ops', slug='ops', is_ops=True,
        )
        cls.experience = Experience.objects.create(
            department=cls.dept, name='City Tour',
            slug='city-tour', category='TOUR',
        )
        cls.admin_user = User.objects.create_user(
            email='routing-admin@test.com', password='pass',
        )
        HotelMembership.objects.create(
            user=cls.admin_user, hotel=cls.hotel, role='ADMIN',
        )
        cls.superadmin_user = User.objects.create_user(
            email='routing-super@test.com', password='pass',
        )
        HotelMembership.objects.create(
            user=cls.superadmin_user, hotel=cls.hotel, role='SUPERADMIN',
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin_user)
        self.events_url = f'/api/v1/hotels/{self.hotel.slug}/admin/events/'
        self.settings_url = f'/api/v1/hotels/{self.hotel.slug}/admin/settings/'


class FallbackDepartmentSettingsTest(EventRoutingTestBase):
    """HotelSettingsSerializer: fallback_department field."""

    def setUp(self):
        super().setUp()
        # Settings requires SUPERADMIN
        self.client.force_authenticate(self.superadmin_user)

    def test_read_fallback_department_null(self):
        resp = self.client.get(self.settings_url)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()['fallback_department'])
        self.assertIsNone(resp.json()['fallback_department_name'])

    def test_set_fallback_department(self):
        resp = self.client.patch(
            self.settings_url,
            {'fallback_department': self.dept.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['fallback_department'], self.dept.id)
        self.assertEqual(resp.json()['fallback_department_name'], 'Concierge')

    def test_clear_fallback_department(self):
        self.hotel.fallback_department = self.dept
        self.hotel.save()
        resp = self.client.patch(
            self.settings_url,
            {'fallback_department': None},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()['fallback_department'])

    def test_reject_ops_department_as_fallback(self):
        resp = self.client.patch(
            self.settings_url,
            {'fallback_department': self.ops_dept.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_reject_other_hotel_department(self):
        other_hotel = Hotel.objects.create(name='Other', slug='other-hotel')
        other_dept = Department.objects.create(
            hotel=other_hotel, name='Pool', slug='pool',
        )
        resp = self.client.patch(
            self.settings_url,
            {'fallback_department': other_dept.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class PublishGuardTest(EventRoutingTestBase):
    """EventSerializer.validate: block publishing unroutable events."""

    def _create_draft_event(self, **kwargs):
        data = {
            'name': 'Test Event',
            'event_start': (timezone.now() + timedelta(days=1)).isoformat(),
            'status': 'DRAFT',
            **kwargs,
        }
        resp = self.client.post(self.events_url, data, format='json')
        self.assertEqual(resp.status_code, 201, resp.json())
        return resp.json()['id']

    def test_publish_blocked_no_department(self):
        """No dept, no experience, no fallback -> 400."""
        event_id = self._create_draft_event()
        resp = self.client.patch(
            f'{self.events_url}{event_id}/',
            {'status': 'PUBLISHED'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('status', resp.json())

    def test_publish_allowed_with_direct_department(self):
        event_id = self._create_draft_event(department=self.dept.id)
        resp = self.client.patch(
            f'{self.events_url}{event_id}/',
            {'status': 'PUBLISHED'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'PUBLISHED')

    def test_publish_allowed_with_experience_department(self):
        event_id = self._create_draft_event(experience=self.experience.id)
        resp = self.client.patch(
            f'{self.events_url}{event_id}/',
            {'status': 'PUBLISHED'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)

    def test_publish_allowed_with_hotel_fallback(self):
        self.hotel.fallback_department = self.dept
        self.hotel.save()
        try:
            event_id = self._create_draft_event()
            resp = self.client.patch(
                f'{self.events_url}{event_id}/',
                {'status': 'PUBLISHED'},
                format='json',
            )
            self.assertEqual(resp.status_code, 200)
        finally:
            self.hotel.fallback_department = None
            self.hotel.save()

    def test_publish_blocked_ops_only_fallback(self):
        """Hotel fallback is ops dept, no other routing -> 400."""
        self.hotel.fallback_department = self.ops_dept
        self.hotel.save()
        try:
            event_id = self._create_draft_event()
            resp = self.client.patch(
                f'{self.events_url}{event_id}/',
                {'status': 'PUBLISHED'},
                format='json',
            )
            self.assertEqual(resp.status_code, 400)
            self.assertIn('status', resp.json())
        finally:
            self.hotel.fallback_department = None
            self.hotel.save()

    def test_create_as_published_blocked_no_department(self):
        """Direct create with status=PUBLISHED should also be blocked."""
        resp = self.client.post(self.events_url, {
            'name': 'Instant Publish',
            'event_start': (timezone.now() + timedelta(days=1)).isoformat(),
            'status': 'PUBLISHED',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('status', resp.json())

    def test_draft_allowed_no_department(self):
        """Draft events don't need routing."""
        resp = self.client.post(self.events_url, {
            'name': 'Draft Event',
            'event_start': (timezone.now() + timedelta(days=1)).isoformat(),
            'status': 'DRAFT',
        }, format='json')
        self.assertEqual(resp.status_code, 201)


class IsRoutablePublicTest(EventRoutingTestBase):
    """EventPublicSerializer: is_routable field on guest-facing API."""

    def test_routable_event(self):
        event = Event.objects.create(
            hotel=self.hotel, department=self.dept,
            name='Routable Event', slug='routable-event',
            event_start=timezone.now() + timedelta(days=1),
            status='PUBLISHED',
        )
        resp = self.client.get(
            f'/api/v1/hotels/{self.hotel.slug}/events/{event.slug}/',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['is_routable'])
        self.assertIsNotNone(resp.json()['routing_department_slug'])

    def test_unroutable_event(self):
        # Create directly on model to bypass serializer publish guard
        event = Event.objects.create(
            hotel=self.hotel,
            name='Unroutable Event', slug='unroutable-event',
            event_start=timezone.now() + timedelta(days=1),
            status='PUBLISHED',
        )
        resp = self.client.get(
            f'/api/v1/hotels/{self.hotel.slug}/events/{event.slug}/',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['is_routable'])
        self.assertIsNone(resp.json()['routing_department_slug'])
