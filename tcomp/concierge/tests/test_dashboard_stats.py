"""Tests for get_dashboard_stats() — period metadata and timezone handling."""
import datetime
import zoneinfo
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from concierge.models import (
    Department,
    GuestStay,
    Hotel,
    ServiceRequest,
)
from concierge.services import get_dashboard_stats
from users.models import User


class DashboardStatsSetupMixin:
    """Shared fixtures for dashboard stats tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.guest_user = User.objects.create_user(
            email='dashboard-guest@test.com', password='pass',
            first_name='Guest', last_name='User',
        )

    def _make_hotel(self, **kwargs):
        defaults = {
            'name': 'Test Hotel',
            'slug': f'test-hotel-{id(self)}',
            'timezone': 'Asia/Kolkata',
        }
        defaults.update(kwargs)
        return Hotel.objects.create(**defaults)

    def _make_request(self, hotel, dept, stay, **kwargs):
        defaults = {
            'hotel': hotel,
            'guest_stay': stay,
            'department': dept,
            'request_type': 'BOOKING',
        }
        defaults.update(kwargs)
        return ServiceRequest.objects.create(**defaults)


class DashboardStatsBadTimezoneTest(DashboardStatsSetupMixin, TestCase):
    """Dashboard stats should not 500 if hotel.timezone is garbage."""

    def test_invalid_timezone_falls_back_to_utc(self):
        hotel = self._make_hotel(slug='bad-tz-hotel', timezone='Not/A/Timezone')
        stats = get_dashboard_stats(hotel)
        self.assertEqual(stats['period_timezone'], 'UTC')
        self.assertIn('period_date_display', stats)
        self.assertIn('period_label', stats)
        self.assertEqual(stats['period_label'], 'Today')

    def test_empty_timezone_falls_back_to_utc(self):
        hotel = self._make_hotel(slug='empty-tz-hotel', timezone='')
        stats = get_dashboard_stats(hotel)
        self.assertEqual(stats['period_timezone'], 'UTC')


class DashboardStatsMidnightBoundaryTest(DashboardStatsSetupMixin, TestCase):
    """Request at 23:59 local counts as today; 00:01 next day does not."""

    def test_midnight_boundary_hotel_timezone(self):
        hotel = self._make_hotel(slug='midnight-hotel', timezone='Asia/Kolkata')
        dept = Department.objects.create(hotel=hotel, name='Spa', slug='spa')
        stay = GuestStay.objects.create(
            guest=self.guest_user, hotel=hotel,
            room_number='101',
            expires_at=timezone.now() + timedelta(days=3),
        )

        ist = zoneinfo.ZoneInfo('Asia/Kolkata')

        # "Now" is Feb 25 IST 06:00 (= Feb 25 UTC 00:30)
        # So "today" in IST = Feb 25
        fake_now = datetime.datetime(2026, 2, 25, 0, 30, tzinfo=zoneinfo.ZoneInfo('UTC'))

        # Create two requests, then force timestamps via QuerySet.update()
        # (ServiceRequest.created_at is auto_now_add=True — constructor values are ignored)
        req_today = self._make_request(hotel, dept, stay)
        req_tomorrow = self._make_request(hotel, dept, stay)

        # Feb 25 IST 23:59 — should count as "today"
        ServiceRequest.objects.filter(pk=req_today.pk).update(
            created_at=datetime.datetime(2026, 2, 25, 23, 59, tzinfo=ist),
        )
        # Feb 26 IST 00:01 — should NOT count (it's tomorrow)
        ServiceRequest.objects.filter(pk=req_tomorrow.pk).update(
            created_at=datetime.datetime(2026, 2, 26, 0, 1, tzinfo=ist),
        )

        with patch('concierge.services.timezone.now', return_value=fake_now):
            stats = get_dashboard_stats(hotel)

        self.assertEqual(stats['total_requests'], 1)
        self.assertIn('25 Feb', stats['period_date_display'])
        self.assertEqual(stats['period_date'], '2026-02-25')
        self.assertEqual(stats['period_timezone'], 'Asia/Kolkata')
