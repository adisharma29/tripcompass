"""Tests for the BookingEmailTemplate API.

Covers:
- Permissions (ADMIN/SUPERADMIN can access, STAFF gets 403, cross-hotel denied)
- GET auto-create flow (template + BOOKING QR on first request, idempotent)
- BOOKING QR uniqueness (generic QR create rejects BOOKING placement)
- PATCH validation (features list, max lengths, read-only fields ignored)
- Serializer output (hotel_context, absolute logo URL, nested QR)
"""
import shutil
import tempfile

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from concierge.models import (
    BookingEmailTemplate,
    Hotel,
    HotelMembership,
    QRCode,
)
from users.models import User

MEDIA_ROOT = tempfile.mkdtemp()


def _url(hotel_slug):
    return f'/api/v1/hotels/{hotel_slug}/admin/booking-email/'


def _qr_url(hotel_slug):
    return f'/api/v1/hotels/{hotel_slug}/admin/qr-codes/'


class BookingEmailSetupMixin:
    """Shared setup: one hotel, three users (SUPERADMIN, ADMIN, STAFF)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hotel = Hotel.objects.create(name='Test Hotel', slug='test-hotel')
        cls.hotel_b = Hotel.objects.create(name='Other Hotel', slug='other-hotel')

        cls.superadmin = User.objects.create_user(
            email='super@test.com', password='pass', first_name='Super', last_name='Admin',
        )
        cls.admin = User.objects.create_user(
            email='admin@test.com', password='pass', first_name='Admin', last_name='User',
        )
        cls.staff = User.objects.create_user(
            email='staff@test.com', password='pass', first_name='Staff', last_name='User',
        )
        cls.admin_b = User.objects.create_user(
            email='adminb@test.com', password='pass', first_name='Admin', last_name='B',
        )

        HotelMembership.objects.create(user=cls.superadmin, hotel=cls.hotel, role='SUPERADMIN')
        HotelMembership.objects.create(user=cls.admin, hotel=cls.hotel, role='ADMIN')
        HotelMembership.objects.create(user=cls.staff, hotel=cls.hotel, role='STAFF')
        HotelMembership.objects.create(user=cls.admin_b, hotel=cls.hotel_b, role='ADMIN')

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BookingEmailPermissionTest(BookingEmailSetupMixin, TestCase):

    def test_admin_can_get(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        resp = client.get(_url('test-hotel'))
        self.assertEqual(resp.status_code, 200)

    def test_superadmin_can_get(self):
        client = APIClient()
        client.force_authenticate(self.superadmin)
        resp = client.get(_url('test-hotel'))
        self.assertEqual(resp.status_code, 200)

    def test_staff_denied(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        resp = client.get(_url('test-hotel'))
        self.assertEqual(resp.status_code, 403)

    def test_staff_denied_patch(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        resp = client.patch(_url('test-hotel'), {'heading': 'X'}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_cross_hotel_denied(self):
        """Admin of hotel B cannot access hotel A's template."""
        client = APIClient()
        client.force_authenticate(self.admin_b)
        resp = client.get(_url('test-hotel'))
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_denied(self):
        client = APIClient()
        resp = client.get(_url('test-hotel'))
        self.assertEqual(resp.status_code, 401)


# ---------------------------------------------------------------------------
# GET auto-create
# ---------------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BookingEmailAutoCreateTest(BookingEmailSetupMixin, TestCase):

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        # Clean any templates/QRs from other tests
        BookingEmailTemplate.objects.filter(hotel=self.hotel).delete()
        QRCode.objects.filter(hotel=self.hotel, placement='BOOKING').delete()

    def test_first_get_creates_template_with_defaults(self):
        self.assertFalse(BookingEmailTemplate.objects.filter(hotel=self.hotel).exists())
        resp = self.client.get(_url('test-hotel'))
        self.assertEqual(resp.status_code, 200)

        # Template created
        self.assertTrue(BookingEmailTemplate.objects.filter(hotel=self.hotel).exists())
        data = resp.json()
        self.assertIn('Digital Concierge', data['heading'])
        self.assertEqual(len(data['features']), 3)
        self.assertIn('Explore Our Services', data['cta_text'])

    def test_first_get_creates_booking_qr(self):
        resp = self.client.get(_url('test-hotel'))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # QR created and nested in response
        self.assertIsNotNone(data['qr_code'])
        self.assertIn('target_url', data['qr_code'])

        # DB check
        qr = QRCode.objects.get(hotel=self.hotel, placement='BOOKING')
        self.assertEqual(qr.label, 'Booking Email')

    def test_second_get_returns_same_template(self):
        resp1 = self.client.get(_url('test-hotel'))
        resp2 = self.client.get(_url('test-hotel'))
        self.assertEqual(resp1.json()['id'], resp2.json()['id'])

        # Only one template and one BOOKING QR
        self.assertEqual(BookingEmailTemplate.objects.filter(hotel=self.hotel).count(), 1)
        self.assertEqual(QRCode.objects.filter(hotel=self.hotel, placement='BOOKING').count(), 1)

    def test_reuses_existing_booking_qr(self):
        """If a BOOKING QR already exists (orphaned), GET reuses it."""
        from concierge.services import generate_qr
        existing_qr = generate_qr(
            hotel=self.hotel, label='Old Booking', placement='BOOKING',
            created_by=self.admin,
        )

        resp = self.client.get(_url('test-hotel'))
        data = resp.json()
        self.assertEqual(data['qr_code']['id'], existing_qr.id)


# ---------------------------------------------------------------------------
# BOOKING QR uniqueness
# ---------------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BookingQRUniquenessTest(BookingEmailSetupMixin, TestCase):

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_generic_qr_create_rejects_booking(self):
        resp = self.client.post(_qr_url('test-hotel'), {
            'label': 'Sneaky Booking QR',
            'placement': 'BOOKING',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('placement', resp.json())

    def test_generic_qr_create_allows_other_placements(self):
        resp = self.client.post(_qr_url('test-hotel'), {
            'label': 'Lobby QR',
            'placement': 'LOBBY',
        }, format='json')
        self.assertIn(resp.status_code, [200, 201])

    def test_generic_qr_patch_to_booking_rejected(self):
        """Patching an existing non-BOOKING QR to BOOKING placement is blocked."""
        # Create a LOBBY QR first
        resp = self.client.post(_qr_url('test-hotel'), {
            'label': 'Lobby QR',
            'placement': 'LOBBY',
        }, format='json')
        qr_id = resp.json()['id']

        # Try to patch it to BOOKING
        resp = self.client.patch(
            f"{_qr_url('test-hotel')}{qr_id}/",
            {'placement': 'BOOKING'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('placement', resp.json())

    def test_existing_booking_qr_not_editable_via_generic_endpoint(self):
        """A BOOKING QR created by the booking email flow cannot be patched via QR CRUD."""
        # Trigger auto-creation of BOOKING QR
        self.client.get(_url('test-hotel'))
        booking_qr = QRCode.objects.get(hotel=self.hotel, placement='BOOKING')

        resp = self.client.patch(
            f"{_qr_url('test-hotel')}{booking_qr.id}/",
            {'label': 'Hacked Label'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('placement', resp.json())


# ---------------------------------------------------------------------------
# Model-level validation
# ---------------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BookingEmailModelCleanTest(BookingEmailSetupMixin, TestCase):

    def test_clean_rejects_wrong_hotel_qr(self):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from concierge.services import generate_qr

        qr_other = generate_qr(
            hotel=self.hotel_b, label='Other Booking', placement='BOOKING',
            created_by=self.admin_b,
        )
        tpl = BookingEmailTemplate(hotel=self.hotel, qr_code=qr_other)
        with self.assertRaises(DjangoValidationError) as ctx:
            tpl.clean()
        self.assertIn('qr_code', ctx.exception.message_dict)

    def test_clean_rejects_non_booking_qr(self):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from concierge.services import generate_qr

        lobby_qr = generate_qr(
            hotel=self.hotel, label='Lobby', placement='LOBBY',
            created_by=self.admin,
        )
        tpl = BookingEmailTemplate(hotel=self.hotel, qr_code=lobby_qr)
        with self.assertRaises(DjangoValidationError) as ctx:
            tpl.clean()
        self.assertIn('qr_code', ctx.exception.message_dict)

    def test_clean_accepts_valid_booking_qr(self):
        from concierge.services import generate_qr

        qr = generate_qr(
            hotel=self.hotel, label='Booking', placement='BOOKING',
            created_by=self.admin,
        )
        tpl = BookingEmailTemplate(hotel=self.hotel, qr_code=qr)
        tpl.clean()  # Should not raise


# ---------------------------------------------------------------------------
# PATCH validation
# ---------------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BookingEmailPatchTest(BookingEmailSetupMixin, TestCase):

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        # Ensure template exists
        self.client.get(_url('test-hotel'))

    def test_partial_update_heading(self):
        resp = self.client.patch(
            _url('test-hotel'),
            {'heading': 'New Heading'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['heading'], 'New Heading')

    def test_partial_update_features(self):
        features = ['Feature A', 'Feature B']
        resp = self.client.patch(
            _url('test-hotel'),
            {'features': features},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['features'], features)

    def test_features_max_6(self):
        features = ['A', 'B', 'C', 'D', 'E', 'F', 'G']
        resp = self.client.patch(
            _url('test-hotel'),
            {'features': features},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_features_must_be_list_of_strings(self):
        resp = self.client.patch(
            _url('test-hotel'),
            {'features': 'not a list'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_features_rejects_non_string_items(self):
        resp = self.client.patch(
            _url('test-hotel'),
            {'features': [1, 2, 3]},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_read_only_fields_ignored(self):
        """qr_code and hotel_context are read-only — PATCH should ignore them."""
        resp = self.client.patch(
            _url('test-hotel'),
            {'heading': 'Test', 'qr_code': None, 'hotel_context': {'name': 'Hacked'}},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['heading'], 'Test')
        # qr_code should still be present (not nullified)
        self.assertIsNotNone(resp.json()['qr_code'])

    def test_subject_max_length(self):
        resp = self.client.patch(
            _url('test-hotel'),
            {'subject': 'X' * 201},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_cta_text_max_length(self):
        resp = self.client.patch(
            _url('test-hotel'),
            {'cta_text': 'X' * 101},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_patch_auto_creates_template(self):
        """PATCH on a hotel with no template should auto-create, then apply update."""
        BookingEmailTemplate.objects.filter(hotel=self.hotel).delete()
        QRCode.objects.filter(hotel=self.hotel, placement='BOOKING').delete()

        resp = self.client.patch(
            _url('test-hotel'),
            {'heading': 'Patched First'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['heading'], 'Patched First')
        self.assertIsNotNone(resp.json()['qr_code'])
        self.assertTrue(BookingEmailTemplate.objects.filter(hotel=self.hotel).exists())


# ---------------------------------------------------------------------------
# Serializer output
# ---------------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BookingEmailSerializerTest(BookingEmailSetupMixin, TestCase):

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_hotel_context_present(self):
        resp = self.client.get(_url('test-hotel'))
        data = resp.json()
        ctx = data['hotel_context']
        self.assertEqual(ctx['name'], 'Test Hotel')
        self.assertIn('primary_color', ctx)
        self.assertIn('secondary_color', ctx)
        self.assertIn('accent_color', ctx)

    def test_qr_code_nested(self):
        resp = self.client.get(_url('test-hotel'))
        qr = resp.json()['qr_code']
        self.assertIn('code', qr)
        self.assertIn('target_url', qr)
        self.assertIn('qr_image', qr)

    def test_timestamps_present(self):
        resp = self.client.get(_url('test-hotel'))
        data = resp.json()
        self.assertIn('created_at', data)
        self.assertIn('updated_at', data)

    def test_qr_image_is_absolute_url(self):
        resp = self.client.get(_url('test-hotel'))
        qr = resp.json()['qr_code']
        self.assertTrue(
            qr['qr_image'].startswith('http'),
            f"qr_image should be absolute URL, got: {qr['qr_image']}",
        )


# ---------------------------------------------------------------------------
# Idempotent creation (sequential proxy for concurrent access)
# ---------------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BookingEmailIdempotencyTest(BookingEmailSetupMixin, TestCase):

    def test_multiple_gets_from_different_users_produce_single_template(self):
        """Two sequential GETs from different admins after deletion return the
        same template and QR. True concurrent races are guarded by
        select_for_update + unique_booking_qr_per_hotel constraint."""
        BookingEmailTemplate.objects.filter(hotel=self.hotel).delete()
        QRCode.objects.filter(hotel=self.hotel, placement='BOOKING').delete()

        client1 = APIClient()
        client1.force_authenticate(self.admin)
        client2 = APIClient()
        client2.force_authenticate(self.superadmin)

        resp1 = client1.get(_url('test-hotel'))
        resp2 = client2.get(_url('test-hotel'))

        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp1.json()['id'], resp2.json()['id'])
        self.assertEqual(
            BookingEmailTemplate.objects.filter(hotel=self.hotel).count(), 1,
        )
        self.assertEqual(
            QRCode.objects.filter(hotel=self.hotel, placement='BOOKING').count(), 1,
        )


# ---------------------------------------------------------------------------
# Color override fields
# ---------------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BookingEmailColorTest(BookingEmailSetupMixin, TestCase):

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        # Ensure template exists
        self.client.get(_url('test-hotel'))

    def test_patch_valid_primary_color(self):
        resp = self.client.patch(
            _url('test-hotel'),
            {'primary_color': '#FF5733'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['primary_color'], '#FF5733')

    def test_patch_valid_accent_color(self):
        resp = self.client.patch(
            _url('test-hotel'),
            {'accent_color': '#00AAFF'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['accent_color'], '#00AAFF')

    def test_patch_invalid_hex_rejected(self):
        resp = self.client.patch(
            _url('test-hotel'),
            {'primary_color': 'red'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_patch_short_hex_rejected(self):
        resp = self.client.patch(
            _url('test-hotel'),
            {'accent_color': '#FFF'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_clear_color_to_empty(self):
        # Set a color first
        self.client.patch(
            _url('test-hotel'),
            {'primary_color': '#112233'},
            format='json',
        )
        # Clear it
        resp = self.client.patch(
            _url('test-hotel'),
            {'primary_color': ''},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['primary_color'], '')

    def test_colors_in_get_response(self):
        self.client.patch(
            _url('test-hotel'),
            {'primary_color': '#AABBCC', 'accent_color': '#112233'},
            format='json',
        )
        resp = self.client.get(_url('test-hotel'))
        data = resp.json()
        self.assertEqual(data['primary_color'], '#AABBCC')
        self.assertEqual(data['accent_color'], '#112233')

    def test_default_colors_empty(self):
        """Newly created template has empty color overrides."""
        resp = self.client.get(_url('test-hotel'))
        data = resp.json()
        self.assertEqual(data['primary_color'], '')
        self.assertEqual(data['accent_color'], '')

    def test_model_clean_rejects_invalid_color(self):
        from django.core.exceptions import ValidationError as DjangoValidationError
        tpl = BookingEmailTemplate.objects.get(hotel=self.hotel)
        tpl.primary_color = 'notacolor'
        with self.assertRaises(DjangoValidationError) as ctx:
            tpl.clean()
        self.assertIn('primary_color', ctx.exception.message_dict)

    def test_model_save_rejects_invalid_color(self):
        """save() enforces hex validation even without full_clean()."""
        from django.core.exceptions import ValidationError as DjangoValidationError
        tpl = BookingEmailTemplate.objects.get(hotel=self.hotel)
        tpl.primary_color = 'nothex'
        with self.assertRaises(DjangoValidationError) as ctx:
            tpl.save()
        self.assertIn('primary_color', ctx.exception.message_dict)

    def test_model_save_accepts_valid_color(self):
        tpl = BookingEmailTemplate.objects.get(hotel=self.hotel)
        tpl.accent_color = '#AABBCC'
        tpl.save()
        tpl.refresh_from_db()
        self.assertEqual(tpl.accent_color, '#AABBCC')

    def test_model_save_accepts_empty_color(self):
        tpl = BookingEmailTemplate.objects.get(hotel=self.hotel)
        tpl.primary_color = ''
        tpl.save()
        tpl.refresh_from_db()
        self.assertEqual(tpl.primary_color, '')
