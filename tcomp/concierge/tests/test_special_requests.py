"""Tests for special request offerings.

Covers:
- Model: clean() validation, get_routing_department() safety
- Serializer: create validation (offering, custom, quantity/category leak guards)
- Admin API: CRUD, reorder, gallery
- Public API: list (grouped), detail
- Notification routing: offering-scoped routes in WhatsApp/Email adapters
- Route API: create/filter offering-scoped routes
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from concierge.models import (
    ContentStatus,
    Department,
    Event,
    Experience,
    Hotel,
    HotelMembership,
    GuestStay,
    NotificationRoute,
    ServiceRequest,
    SpecialRequestOffering,
    SpecialRequestOfferingImage,
)
from concierge.notifications.base import NotificationEvent
from concierge.notifications.email import EmailAdapter
from concierge.notifications.whatsapp import WhatsAppAdapter
from users.models import User


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

class SpecialRequestSetupMixin:
    """Shared test data for special request tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.hotel = Hotel.objects.create(
            name='Test Hotel', slug='test-hotel',
            whatsapp_notifications_enabled=True,
            custom_requests_enabled=True,
        )
        cls.dept = Department.objects.create(
            hotel=cls.hotel, name='Concierge', slug='concierge',
        )
        cls.ops_dept = Department.objects.create(
            hotel=cls.hotel, name='Ops Only', slug='ops-only', is_ops=True,
        )
        cls.hotel.fallback_department = cls.dept
        cls.hotel.save(update_fields=['fallback_department'])

        cls.other_hotel = Hotel.objects.create(
            name='Other Hotel', slug='other-hotel',
        )
        cls.other_dept = Department.objects.create(
            hotel=cls.other_hotel, name='Other Dept', slug='other-dept',
        )

        # Admin user
        cls.admin_user = User.objects.create_user(
            email='admin@test.com', password='pass',
            first_name='Admin', last_name='User',
        )
        cls.admin_membership = HotelMembership.objects.create(
            user=cls.admin_user, hotel=cls.hotel, role='ADMIN',
        )

        # Staff user
        cls.staff_user = User.objects.create_user(
            email='staff@test.com', password='pass',
            first_name='Staff', last_name='One', phone='+919876543210',
        )
        cls.staff_membership = HotelMembership.objects.create(
            user=cls.staff_user, hotel=cls.hotel,
            role='STAFF', department=cls.dept,
        )

        # Guest user + stay
        cls.guest_user = User.objects.create_user(
            email='guest@test.com', password='pass',
            first_name='Guest', last_name='User', user_type='GUEST',
        )
        cls.stay = GuestStay.objects.create(
            guest=cls.guest_user, hotel=cls.hotel,
            room_number='101',
            expires_at=timezone.now() + timedelta(days=3),
        )

        # Published offering
        cls.offering = SpecialRequestOffering.objects.create(
            hotel=cls.hotel,
            category='UTILITARIAN',
            name='Extra Towels',
            department=cls.dept,
            max_quantity=5,
            status=ContentStatus.PUBLISHED,
            published_at=timezone.now(),
        )

        # Draft offering
        cls.draft_offering = SpecialRequestOffering.objects.create(
            hotel=cls.hotel,
            category='PERSONALIZATION',
            name='Birthday Cake',
            department=cls.dept,
            status=ContentStatus.DRAFT,
        )

    def _make_request(self, **kwargs):
        defaults = {
            'hotel': self.hotel,
            'guest_stay': self.stay,
            'department': self.dept,
            'request_type': 'BOOKING',
        }
        defaults.update(kwargs)
        return ServiceRequest.objects.create(**defaults)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class SpecialRequestOfferingModelTest(SpecialRequestSetupMixin, TestCase):

    def test_auto_slug_generation(self):
        offering = SpecialRequestOffering.objects.create(
            hotel=self.hotel, category='UTILITARIAN', name='Room Upgrade',
        )
        self.assertEqual(offering.slug, 'room-upgrade')

    def test_auto_slug_dedup(self):
        SpecialRequestOffering.objects.create(
            hotel=self.hotel, category='UTILITARIAN', name='Extra Towels',
            slug='extra-towels-dup',
        )
        offering = SpecialRequestOffering.objects.create(
            hotel=self.hotel, category='UTILITARIAN', name='Extra Towels Dup',
        )
        # Slug should be deduped
        self.assertNotEqual(offering.slug, 'extra-towels')

    def test_clean_rejects_cross_hotel_department(self):
        offering = SpecialRequestOffering(
            hotel=self.hotel, category='UTILITARIAN', name='Test',
            department=self.other_dept,
        )
        with self.assertRaises(DjangoValidationError) as ctx:
            offering.clean()
        self.assertIn('department', ctx.exception.message_dict)

    def test_clean_rejects_ops_department(self):
        offering = SpecialRequestOffering(
            hotel=self.hotel, category='UTILITARIAN', name='Test',
            department=self.ops_dept,
        )
        with self.assertRaises(DjangoValidationError) as ctx:
            offering.clean()
        self.assertIn('department', ctx.exception.message_dict)

    def test_clean_allows_valid_department(self):
        offering = SpecialRequestOffering(
            hotel=self.hotel, category='UTILITARIAN', name='Test',
            department=self.dept,
        )
        offering.clean()  # Should not raise

    def test_clean_allows_no_department(self):
        offering = SpecialRequestOffering(
            hotel=self.hotel, category='UTILITARIAN', name='Test',
        )
        offering.clean()  # Should not raise

    def test_get_routing_department_with_valid_dept(self):
        self.assertEqual(self.offering.get_routing_department(), self.dept)

    def test_get_routing_department_fallback(self):
        offering = SpecialRequestOffering(
            hotel=self.hotel, category='UTILITARIAN', name='No Dept',
        )
        self.assertEqual(offering.get_routing_department(), self.dept)

    def test_get_routing_department_cross_hotel_falls_through(self):
        """Cross-hotel dept is ignored, falls back to hotel.fallback_department."""
        offering = SpecialRequestOffering(
            hotel=self.hotel, category='UTILITARIAN', name='Bad',
            department=self.other_dept,
        )
        # Should skip cross-hotel dept and use fallback
        self.assertEqual(offering.get_routing_department(), self.dept)

    def test_get_routing_department_ops_dept_falls_through(self):
        """Ops-only dept is ignored, falls back to hotel.fallback_department."""
        offering = SpecialRequestOffering(
            hotel=self.hotel, category='UTILITARIAN', name='Bad',
            department=self.ops_dept,
        )
        self.assertEqual(offering.get_routing_department(), self.dept)

    def test_get_routing_department_none_when_no_fallback(self):
        hotel_no_fb = Hotel.objects.create(name='No FB', slug='no-fb')
        offering = SpecialRequestOffering(
            hotel=hotel_no_fb, category='UTILITARIAN', name='Test',
        )
        self.assertIsNone(offering.get_routing_department())


# ---------------------------------------------------------------------------
# Serializer / guest request create tests
# ---------------------------------------------------------------------------

class SpecialRequestCreateTest(SpecialRequestSetupMixin, TestCase):
    """Guest request creation tests with rate limiting bypassed."""

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.guest_user)
        self.url = '/api/v1/hotels/test-hotel/requests/'
        # Bypass rate limiting for all tests in this class
        p1 = patch('concierge.views.check_stay_rate_limit', return_value=True)
        p2 = patch('concierge.views.check_room_rate_limit', return_value=True)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_create_special_request_success(self):
        resp = self.client.post(self.url, {
            'special_request_offering': self.offering.id,
            'request_type': 'SPECIAL_REQUEST',
            'guest_name': 'Guest User',
            'guest_notes': 'Extra fluffy please',
            'quantity': 3,
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data['request_type'], 'SPECIAL_REQUEST')
        self.assertEqual(data['special_request_offering_name'], 'Extra Towels')
        self.assertEqual(data['special_request_category'], 'UTILITARIAN')
        self.assertEqual(data['quantity'], 3)

    def test_create_rejects_draft_offering(self):
        resp = self.client.post(self.url, {
            'special_request_offering': self.draft_offering.id,
            'request_type': 'SPECIAL_REQUEST',
            'guest_name': 'Guest User',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_create_rejects_cross_hotel_offering(self):
        other_offering = SpecialRequestOffering.objects.create(
            hotel=self.other_hotel, category='UTILITARIAN', name='Other',
            status=ContentStatus.PUBLISHED, published_at=timezone.now(),
        )
        resp = self.client.post(self.url, {
            'special_request_offering': other_offering.id,
            'request_type': 'SPECIAL_REQUEST',
            'guest_name': 'Guest User',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_create_rejects_quantity_over_max(self):
        resp = self.client.post(self.url, {
            'special_request_offering': self.offering.id,
            'request_type': 'SPECIAL_REQUEST',
            'guest_name': 'Guest User',
            'quantity': 10,  # max is 5
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('quantity', resp.json())

    def test_create_rejects_special_request_type_without_offering(self):
        resp = self.client.post(self.url, {
            'request_type': 'SPECIAL_REQUEST',
            'department': self.dept.id,
            'guest_name': 'Guest User',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_quantity_reset_on_booking_request(self):
        """Quantity sent on a BOOKING request should be forced to 1."""
        resp = self.client.post(self.url, {
            'request_type': 'BOOKING',
            'department': self.dept.id,
            'guest_name': 'Guest User',
            'quantity': 99,
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        req = ServiceRequest.objects.get(public_id=resp.json()['public_id'])
        self.assertEqual(req.quantity, 1)

    def test_category_cleared_on_booking_request(self):
        """special_request_category sent on a BOOKING request should be blank."""
        resp = self.client.post(self.url, {
            'request_type': 'BOOKING',
            'department': self.dept.id,
            'guest_name': 'Guest User',
            'special_request_category': 'UTILITARIAN',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        req = ServiceRequest.objects.get(public_id=resp.json()['public_id'])
        self.assertEqual(req.special_request_category, '')

    def test_custom_request_preserves_category(self):
        """CUSTOM type should preserve special_request_category."""
        resp = self.client.post(self.url, {
            'request_type': 'CUSTOM',
            'guest_name': 'Guest User',
            'guest_notes': 'Something custom',
            'special_request_category': 'PERSONALIZATION',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        req = ServiceRequest.objects.get(public_id=resp.json()['public_id'])
        self.assertEqual(req.special_request_category, 'PERSONALIZATION')


# ---------------------------------------------------------------------------
# Room number requirement tests (experience / event)
# ---------------------------------------------------------------------------

class RoomNumberRequirementTest(SpecialRequestSetupMixin, TestCase):
    """Tests for requires_room_number on Experience and Event."""

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.guest_user)
        self.url = '/api/v1/hotels/test-hotel/requests/'
        # Bypass rate limiting
        p1 = patch('concierge.views.check_stay_rate_limit', return_value=True)
        p2 = patch('concierge.views.check_room_rate_limit', return_value=True)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

        # Experience that does not require room number
        self.exp_no_room = Experience.objects.create(
            department=self.dept, name='Pool Access',
            category='ACTIVITY', requires_room_number=False,
            status=ContentStatus.PUBLISHED,
        )

        # Experience that requires room number (default)
        self.exp_room = Experience.objects.create(
            department=self.dept, name='In-Room Dining',
            category='OTHER', requires_room_number=True,
            status=ContentStatus.PUBLISHED,
        )

        # Event that does not require room number
        self.event_no_room = Event.objects.create(
            hotel=self.hotel, name='Sunset Yoga',
            category='ACTIVITY', requires_room_number=False,
            event_start=timezone.now() + timedelta(days=1),
            status=ContentStatus.PUBLISHED,
        )

        # Guest without room number
        self.guest_no_room = User.objects.create_user(
            email='noroom@test.com', password='pass', user_type='GUEST',
        )
        self.stay_no_room = GuestStay.objects.create(
            guest=self.guest_no_room, hotel=self.hotel,
            room_number='',
            expires_at=timezone.now() + timedelta(days=3),
        )

    def test_experience_no_room_required_succeeds_without_room(self):
        """Guest without room can book experience that doesn't require it."""
        self.client.force_authenticate(self.guest_no_room)
        resp = self.client.post(self.url, {
            'experience': self.exp_no_room.id,
            'department': self.dept.id,
            'request_type': 'BOOKING',
            'guest_name': 'No Room Guest',
        }, format='json')
        self.assertEqual(resp.status_code, 201)

    def test_experience_room_required_fails_without_room(self):
        """Guest without room cannot book experience that requires it."""
        self.client.force_authenticate(self.guest_no_room)
        resp = self.client.post(self.url, {
            'experience': self.exp_room.id,
            'department': self.dept.id,
            'request_type': 'BOOKING',
            'guest_name': 'No Room Guest',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('room number', resp.json()['detail'].lower())

    def test_event_no_room_required_succeeds_without_room(self):
        """Guest without room can book event that doesn't require it."""
        self.client.force_authenticate(self.guest_no_room)
        resp = self.client.post(self.url, {
            'event': self.event_no_room.id,
            'request_type': 'BOOKING',
            'guest_name': 'No Room Guest',
        }, format='json')
        self.assertEqual(resp.status_code, 201)

    def test_experience_room_required_succeeds_with_room(self):
        """Guest with room can book experience that requires it."""
        resp = self.client.post(self.url, {
            'experience': self.exp_room.id,
            'department': self.dept.id,
            'request_type': 'BOOKING',
            'guest_name': 'Guest User',
        }, format='json')
        self.assertEqual(resp.status_code, 201)


# ---------------------------------------------------------------------------
# Admin API tests
# ---------------------------------------------------------------------------

class SpecialRequestAdminAPITest(SpecialRequestSetupMixin, TestCase):

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin_user)
        self.base_url = '/api/v1/hotels/test-hotel/admin/special-requests/'

    def test_list_offerings(self):
        resp = self.client.get(self.base_url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # pagination_class = None → plain list
        names = [o['name'] for o in data]
        self.assertIn('Extra Towels', names)
        self.assertIn('Birthday Cake', names)

    def test_create_offering(self):
        resp = self.client.post(self.base_url, {
            'category': 'PERSONALIZATION',
            'name': 'Anniversary Roses',
            'price_display': '₹2,500',
            'status': 'DRAFT',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data['name'], 'Anniversary Roses')
        self.assertEqual(data['slug'], 'anniversary-roses')

    def test_retrieve_offering(self):
        resp = self.client.get(f'{self.base_url}{self.offering.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['name'], 'Extra Towels')

    def test_update_offering(self):
        resp = self.client.patch(f'{self.base_url}{self.offering.id}/', {
            'price_display': '₹100 per set',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['price_display'], '₹100 per set')

    def test_delete_offering(self):
        to_delete = SpecialRequestOffering.objects.create(
            hotel=self.hotel, category='UTILITARIAN', name='Delete Me',
        )
        resp = self.client.delete(f'{self.base_url}{to_delete.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(SpecialRequestOffering.objects.filter(id=to_delete.id).exists())

    def test_staff_can_list_but_not_create(self):
        self.client.force_authenticate(self.staff_user)
        resp = self.client.get(self.base_url)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(self.base_url, {
            'category': 'UTILITARIAN', 'name': 'Nope',
        }, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_filter_by_category(self):
        resp = self.client.get(f'{self.base_url}?category=UTILITARIAN')
        self.assertEqual(resp.status_code, 200)
        for o in resp.json():
            self.assertEqual(o['category'], 'UTILITARIAN')

    def test_reorder(self):
        o1 = self.offering
        o2 = SpecialRequestOffering.objects.create(
            hotel=self.hotel, category='UTILITARIAN', name='Reorder Test',
        )
        resp = self.client.patch(
            f'{self.base_url}reorder/?category=UTILITARIAN',
            {'order': [o2.id, o1.id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        o1.refresh_from_db()
        o2.refresh_from_db()
        self.assertGreater(o1.display_order, o2.display_order)

    def test_gallery_upload_and_delete(self):
        from io import BytesIO
        from PIL import Image as PILImage
        from django.core.files.uploadedfile import SimpleUploadedFile

        buf = BytesIO()
        PILImage.new('RGB', (100, 100), 'red').save(buf, 'JPEG')
        buf.seek(0)
        img_file = SimpleUploadedFile('test.jpg', buf.read(), content_type='image/jpeg')

        # Upload
        resp = self.client.post(
            f'{self.base_url}{self.offering.id}/images/',
            {'image': img_file},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 201)
        img_id = resp.json()['id']

        # Delete
        resp = self.client.delete(
            f'/api/v1/hotels/test-hotel/admin/special-request-images/{img_id}/',
        )
        self.assertEqual(resp.status_code, 204)


# ---------------------------------------------------------------------------
# Public API tests
# ---------------------------------------------------------------------------

class SpecialRequestPublicAPITest(SpecialRequestSetupMixin, TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_public_list_grouped(self):
        resp = self.client.get('/api/v1/hotels/test-hotel/special-requests/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('utilitarian', data)
        self.assertIn('personalization', data)
        # Only published offerings
        all_names = [o['name'] for o in data['utilitarian'] + data['personalization']]
        self.assertIn('Extra Towels', all_names)
        self.assertNotIn('Birthday Cake', all_names)  # Draft

    def test_public_detail(self):
        resp = self.client.get(
            f'/api/v1/hotels/test-hotel/special-requests/{self.offering.slug}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['name'], 'Extra Towels')

    def test_public_detail_draft_404(self):
        resp = self.client.get(
            f'/api/v1/hotels/test-hotel/special-requests/{self.draft_offering.slug}/'
        )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Notification routing tests (offering-scoped)
# ---------------------------------------------------------------------------

class OfferingRouteAPITest(SpecialRequestSetupMixin, TestCase):
    """CRUD and filtering for offering-scoped notification routes."""

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin_user)
        self.base_url = '/api/v1/hotels/test-hotel/admin/notification-routes/'

    def test_create_offering_route(self):
        resp = self.client.post(self.base_url, {
            'special_request_offering': self.offering.id,
            'channel': 'EMAIL',
            'target': 'towels@hotel.com',
            'label': 'Towel Team',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        route = NotificationRoute.objects.get(id=resp.json()['id'])
        self.assertEqual(route.special_request_offering_id, self.offering.id)
        self.assertIsNone(route.department_id)
        self.assertIsNone(route.event_id)

    def test_create_rejects_offering_plus_department(self):
        resp = self.client.post(self.base_url, {
            'special_request_offering': self.offering.id,
            'department': self.dept.id,
            'channel': 'EMAIL',
            'target': 'bad@hotel.com',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_create_rejects_offering_plus_experience(self):
        from concierge.models import Experience
        exp = Experience.objects.create(
            department=self.dept, name='Spa', slug='spa', category='SPA',
        )
        resp = self.client.post(self.base_url, {
            'special_request_offering': self.offering.id,
            'experience': exp.id,
            'channel': 'EMAIL',
            'target': 'bad@hotel.com',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_filter_by_offering(self):
        NotificationRoute.objects.create(
            hotel=self.hotel, special_request_offering=self.offering,
            channel='EMAIL', target='offering@hotel.com',
            created_by=self.admin_user,
        )
        NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='EMAIL', target='dept@hotel.com',
            created_by=self.admin_user,
        )
        resp = self.client.get(
            f'{self.base_url}?special_request_offering={self.offering.id}'
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['special_request_offering'], self.offering.id)

    def test_filter_multi_scope_rejected(self):
        resp = self.client.get(
            f'{self.base_url}?department={self.dept.id}'
            f'&special_request_offering={self.offering.id}'
        )
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_offering_route_returns_400(self):
        NotificationRoute.objects.create(
            hotel=self.hotel, special_request_offering=self.offering,
            channel='WHATSAPP', target='919876543210',
            created_by=self.admin_user,
        )
        resp = self.client.post(self.base_url, {
            'special_request_offering': self.offering.id,
            'channel': 'WHATSAPP',
            'target': '919876543210',
            'label': 'Dup',
        }, format='json')
        self.assertEqual(resp.status_code, 400)


@override_settings(GUPSHUP_WA_API_KEY='test-key')
class OfferingRouteWhatsAppTest(SpecialRequestSetupMixin, TestCase):
    """WhatsAppAdapter routes with offering-scoped + dept-scoped routes."""

    def setUp(self):
        self.adapter = WhatsAppAdapter()
        # Department-wide WA route
        self.dept_route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='WHATSAPP', target='919876543210',
            label='Dept Staff', created_by=self.admin_user,
        )
        # Offering-specific WA route
        self.offering_route = NotificationRoute.objects.create(
            hotel=self.hotel, special_request_offering=self.offering,
            channel='WHATSAPP', target='919111222333',
            label='Towel Manager', created_by=self.admin_user,
        )

    def test_offering_request_gets_both_routes(self):
        """Offering request → offering route + dept route (dept via resolved department)."""
        req = self._make_request(
            special_request_offering=self.offering,
            request_type='SPECIAL_REQUEST',
        )
        event = NotificationEvent(
            event_type='request.created',
            hotel=self.hotel,
            department=self.dept,
            request=req,
            offering_obj=self.offering,
        )
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertIn('919876543210', targets)   # dept route
        self.assertIn('919111222333', targets)   # offering route

    def test_non_offering_request_skips_offering_route(self):
        """Regular request without offering → only dept route, no offering route."""
        req = self._make_request()
        event = NotificationEvent(
            event_type='request.created',
            hotel=self.hotel,
            department=self.dept,
            request=req,
        )
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertIn('919876543210', targets)
        self.assertNotIn('919111222333', targets)

    def test_dedup_same_target_across_scopes(self):
        """Same phone in dept route + offering route → single recipient."""
        self.offering_route.target = '919876543210'
        self.offering_route.save()
        req = self._make_request(
            special_request_offering=self.offering,
            request_type='SPECIAL_REQUEST',
        )
        event = NotificationEvent(
            event_type='request.created',
            hotel=self.hotel,
            department=self.dept,
            request=req,
            offering_obj=self.offering,
        )
        recipients = self.adapter.get_recipients(event)
        targets = [r.target for r in recipients]
        self.assertEqual(targets.count('919876543210'), 1)


@override_settings(RESEND_API_KEY='re_test')
class OfferingRouteEmailTest(SpecialRequestSetupMixin, TestCase):
    """EmailAdapter routes with offering-scoped + dept-scoped routes."""

    def setUp(self):
        self.adapter = EmailAdapter()
        self.hotel.email_notifications_enabled = True
        self.hotel.save(update_fields=['email_notifications_enabled'])
        self.dept_route = NotificationRoute.objects.create(
            hotel=self.hotel, department=self.dept,
            channel='EMAIL', target='dept@hotel.com',
            label='Dept Manager', created_by=self.admin_user,
        )
        self.offering_route = NotificationRoute.objects.create(
            hotel=self.hotel, special_request_offering=self.offering,
            channel='EMAIL', target='offering@hotel.com',
            label='Offering Lead', created_by=self.admin_user,
        )

    def test_offering_request_gets_both_routes(self):
        req = self._make_request(
            special_request_offering=self.offering,
            request_type='SPECIAL_REQUEST',
        )
        event = NotificationEvent(
            event_type='request.created',
            hotel=self.hotel,
            department=self.dept,
            request=req,
            offering_obj=self.offering,
        )
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertIn('dept@hotel.com', targets)
        self.assertIn('offering@hotel.com', targets)

    def test_non_offering_request_skips_offering_route(self):
        req = self._make_request()
        event = NotificationEvent(
            event_type='request.created',
            hotel=self.hotel,
            department=self.dept,
            request=req,
        )
        recipients = self.adapter.get_recipients(event)
        targets = {r.target for r in recipients}
        self.assertIn('dept@hotel.com', targets)
        self.assertNotIn('offering@hotel.com', targets)


# ---------------------------------------------------------------------------
# NotificationEvent display_name tests
# ---------------------------------------------------------------------------

class OfferingNotificationEventTest(SpecialRequestSetupMixin, TestCase):

    def test_display_name_offering_priority(self):
        """offering_obj.name takes priority over event_obj.name."""
        from concierge.models import Event
        event_obj = Event.objects.create(
            hotel=self.hotel, department=self.dept,
            name='Wine Tasting', event_start=timezone.now() + timedelta(days=1),
            status='PUBLISHED',
        )
        req = self._make_request(special_request_offering=self.offering)
        ne = NotificationEvent(
            event_type='request.created',
            hotel=self.hotel,
            department=self.dept,
            request=req,
            event_obj=event_obj,
            offering_obj=self.offering,
        )
        self.assertEqual(ne.display_name, 'Extra Towels')

    def test_display_name_offering_alone(self):
        req = self._make_request(special_request_offering=self.offering)
        ne = NotificationEvent(
            event_type='request.created',
            hotel=self.hotel,
            department=self.dept,
            request=req,
            offering_obj=self.offering,
        )
        self.assertEqual(ne.display_name, 'Extra Towels')

    def test_display_name_no_offering_falls_through(self):
        req = self._make_request()
        ne = NotificationEvent(
            event_type='request.created',
            hotel=self.hotel,
            department=self.dept,
            request=req,
        )
        self.assertEqual(ne.display_name, 'General Request')
