"""Tests for QR scan tracking — endpoint, dedup, timezone bucketing, analytics merge."""
import datetime
import zoneinfo
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.core.cache import cache
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from concierge.models import (
    Department,
    GuestStay,
    Hotel,
    QRCode,
    QRScanDaily,
    ServiceRequest,
)
from concierge.analytics import get_qr_placement_stats
from users.models import User


class QRScanSetupMixin:
    """Shared fixtures for QR scan tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.guest_user = User.objects.create_user(
            email='qrscan-guest@test.com', password='pass',
        )

    def setUp(self):
        super().setUp()
        cache.clear()
        self.hotel = Hotel.objects.create(
            name='Test Hotel', slug='test-hotel-qr', timezone='Asia/Kolkata',
        )
        self.qr = QRCode.objects.create(
            hotel=self.hotel, code='LOBBY01', placement='LOBBY', is_active=True,
        )
        self.client = APIClient()
        self.url = f'/api/v1/hotels/{self.hotel.slug}/qr-scan/'


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class QRScanEndpointTest(QRScanSetupMixin, TestCase):
    """POST /api/v1/hotels/{slug}/qr-scan/ basics."""

    def test_valid_scan_creates_daily_row(self):
        resp = self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'v1'}, format='json')
        self.assertEqual(resp.status_code, 204)
        row = QRScanDaily.objects.get(qr_code=self.qr)
        self.assertEqual(row.scan_count, 1)
        self.assertEqual(row.unique_visitors, 1)

    def test_invalid_code_returns_204_no_row(self):
        resp = self.client.post(self.url, {'code': 'DOESNOTEXIST', 'vid': 'v1'}, format='json')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(QRScanDaily.objects.exists())

    def test_missing_code_returns_204(self):
        resp = self.client.post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(QRScanDaily.objects.exists())

    def test_empty_code_returns_204(self):
        resp = self.client.post(self.url, {'code': '', 'vid': 'v1'}, format='json')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(QRScanDaily.objects.exists())

    def test_inactive_qr_returns_204_no_row(self):
        self.qr.is_active = False
        self.qr.save()
        resp = self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'v1'}, format='json')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(QRScanDaily.objects.exists())

    def test_wrong_hotel_slug_returns_204(self):
        resp = self.client.post(
            '/api/v1/hotels/nonexistent/qr-scan/',
            {'code': 'LOBBY01', 'vid': 'v1'}, format='json',
        )
        self.assertEqual(resp.status_code, 204)

    def test_vid_fallback_to_ip(self):
        """POST without vid → uses ip: prefix, still records."""
        resp = self.client.post(self.url, {'code': 'LOBBY01'}, format='json')
        self.assertEqual(resp.status_code, 204)
        row = QRScanDaily.objects.get(qr_code=self.qr)
        self.assertEqual(row.scan_count, 1)

    def test_form_encoded_body(self):
        """sendBeacon fallback sends form-encoded instead of JSON."""
        resp = self.client.post(
            self.url,
            data='code=LOBBY01&vid=v2',
            content_type='application/x-www-form-urlencoded',
        )
        self.assertEqual(resp.status_code, 204)
        row = QRScanDaily.objects.get(qr_code=self.qr)
        self.assertEqual(row.scan_count, 1)


# ---------------------------------------------------------------------------
# Dedup tests
# ---------------------------------------------------------------------------

@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class QRScanDedupTest(QRScanSetupMixin, TestCase):
    """Redis-based dedup behavior."""

    def test_dedup_same_vid_same_hour(self):
        """Two POSTs same vid within same hour → scan_count=1."""
        self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'same-vid'}, format='json')
        self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'same-vid'}, format='json')
        row = QRScanDaily.objects.get(qr_code=self.qr)
        self.assertEqual(row.scan_count, 1)

    def test_dedup_same_vid_different_hour(self):
        """Two POSTs same vid, different hours → scan_count=2, unique_visitors=1."""
        ist = zoneinfo.ZoneInfo('Asia/Kolkata')

        # First scan at 10:00 IST
        t1 = datetime.datetime(2026, 2, 25, 4, 30, tzinfo=zoneinfo.ZoneInfo('UTC'))  # 10:00 IST
        with patch('concierge.views.timezone.now', return_value=t1):
            self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'same-vid'}, format='json')

        # Second scan at 11:00 IST (different hour)
        t2 = datetime.datetime(2026, 2, 25, 5, 30, tzinfo=zoneinfo.ZoneInfo('UTC'))  # 11:00 IST
        with patch('concierge.views.timezone.now', return_value=t2):
            self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'same-vid'}, format='json')

        row = QRScanDaily.objects.get(qr_code=self.qr)
        self.assertEqual(row.scan_count, 2)
        self.assertEqual(row.unique_visitors, 1)

    def test_different_vid_same_hour(self):
        """Two POSTs different vids → scan_count=2, unique_visitors=2."""
        self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'vid-a'}, format='json')
        self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'vid-b'}, format='json')
        row = QRScanDaily.objects.get(qr_code=self.qr)
        self.assertEqual(row.scan_count, 2)
        self.assertEqual(row.unique_visitors, 2)


# ---------------------------------------------------------------------------
# Timezone bucketing
# ---------------------------------------------------------------------------

@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class QRScanTimezoneBucketingTest(QRScanSetupMixin, TestCase):
    """Date bucketing uses hotel timezone, not UTC."""

    def test_hotel_timezone_date_bucketing(self):
        """Scan at 23:00 UTC = 04:30 IST next day → date is next day."""
        # 23:00 UTC on Feb 25 = 04:30 IST on Feb 26
        fake_now = datetime.datetime(2026, 2, 25, 23, 0, tzinfo=zoneinfo.ZoneInfo('UTC'))
        with patch('concierge.views.timezone.now', return_value=fake_now):
            self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'tz-test'}, format='json')

        row = QRScanDaily.objects.get(qr_code=self.qr)
        self.assertEqual(row.date, datetime.date(2026, 2, 26))

    def test_hotel_timezone_midnight_boundary(self):
        """Two scans: 23:59 IST → today, 00:01 IST next day → tomorrow."""
        ist = zoneinfo.ZoneInfo('Asia/Kolkata')

        # 23:59 IST on Feb 25 = 18:29 UTC on Feb 25
        t_before = datetime.datetime(2026, 2, 25, 18, 29, tzinfo=zoneinfo.ZoneInfo('UTC'))
        with patch('concierge.views.timezone.now', return_value=t_before):
            self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'boundary-a'}, format='json')

        # 00:01 IST on Feb 26 = 18:31 UTC on Feb 25
        t_after = datetime.datetime(2026, 2, 25, 18, 31, tzinfo=zoneinfo.ZoneInfo('UTC'))
        with patch('concierge.views.timezone.now', return_value=t_after):
            self.client.post(self.url, {'code': 'LOBBY01', 'vid': 'boundary-b'}, format='json')

        rows = QRScanDaily.objects.filter(qr_code=self.qr).order_by('date')
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows[0].date, datetime.date(2026, 2, 25))
        self.assertEqual(rows[1].date, datetime.date(2026, 2, 26))


# ---------------------------------------------------------------------------
# Redis failure resilience
# ---------------------------------------------------------------------------

@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class QRScanRedisFailureTest(QRScanSetupMixin, TestCase):
    """Scan still records when cache is down."""

    def test_redis_down_falls_through(self):
        """cache.get raising → scan still recorded, no 500."""
        with patch('concierge.views.cache') as mock_cache:
            mock_cache.get.side_effect = Exception('Redis down')
            mock_cache.set.side_effect = Exception('Redis down')
            resp = self.client.post(
                self.url, {'code': 'LOBBY01', 'vid': 'redis-test'}, format='json',
            )
        self.assertEqual(resp.status_code, 204)
        row = QRScanDaily.objects.get(qr_code=self.qr)
        self.assertEqual(row.scan_count, 1)
        self.assertEqual(row.unique_visitors, 1)


# ---------------------------------------------------------------------------
# Concurrent write resilience
# ---------------------------------------------------------------------------

@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class QRScanConcurrentWriteTest(QRScanSetupMixin, TestCase):
    """IntegrityError on get_or_create → retries, scan recorded."""

    def test_concurrent_first_write(self):
        """Simulate IntegrityError on get_or_create → retries and succeeds."""
        original_get_or_create = QRScanDaily.objects.get_or_create

        call_count = [0]

        def mock_get_or_create(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: simulate race — the row was created by another process
                QRScanDaily.objects.create(
                    qr_code=kwargs['qr_code'], date=kwargs['date'],
                )
                raise IntegrityError('duplicate key')
            return original_get_or_create(**kwargs)

        with patch.object(QRScanDaily.objects, 'get_or_create', side_effect=mock_get_or_create):
            resp = self.client.post(
                self.url, {'code': 'LOBBY01', 'vid': 'race-test'}, format='json',
            )

        self.assertEqual(resp.status_code, 204)
        row = QRScanDaily.objects.get(qr_code=self.qr)
        self.assertEqual(row.scan_count, 1)


# ---------------------------------------------------------------------------
# Cascade delete
# ---------------------------------------------------------------------------

class QRScanCascadeDeleteTest(QRScanSetupMixin, TestCase):
    """Delete QRCode → QRScanDaily rows gone."""

    def test_deleted_qr_cascades(self):
        QRScanDaily.objects.create(qr_code=self.qr, date=datetime.date.today(), scan_count=5)
        self.assertEqual(QRScanDaily.objects.count(), 1)
        self.qr.delete()
        self.assertEqual(QRScanDaily.objects.count(), 0)


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class QRScanThrottleTest(QRScanSetupMixin, TestCase):
    """Rate limiting on the scan endpoint."""

    def test_throttle_returns_429_after_limit(self):
        """Requests beyond rate limit get 429, earlier ones get 204."""
        from concierge.views import QRScanThrottle

        original_rate = QRScanThrottle.rate
        QRScanThrottle.rate = '3/min'
        # Clear cached parsed rate so DRF re-parses the new value
        QRScanThrottle.THROTTLE_RATES['qr_scan'] = '3/min'
        try:
            for i in range(3):
                resp = self.client.post(
                    self.url, {'code': 'LOBBY01', 'vid': f'throttle-{i}'}, format='json',
                )
                self.assertEqual(resp.status_code, 204, f'Request {i+1} should be 204')

            # 4th request should be throttled
            resp = self.client.post(
                self.url, {'code': 'LOBBY01', 'vid': 'throttle-overflow'}, format='json',
            )
            self.assertEqual(resp.status_code, 429)
        finally:
            QRScanThrottle.rate = original_rate
            QRScanThrottle.THROTTLE_RATES['qr_scan'] = original_rate


# ---------------------------------------------------------------------------
# Analytics merge
# ---------------------------------------------------------------------------

@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class QRScanAnalyticsMergeTest(QRScanSetupMixin, TestCase):
    """get_qr_placement_stats merges scan data + verified sessions."""

    def setUp(self):
        super().setUp()
        self.ist = zoneinfo.ZoneInfo('Asia/Kolkata')
        self.start_dt = datetime.datetime(2026, 2, 20, 0, 0, tzinfo=self.ist)
        self.end_dt = datetime.datetime(2026, 2, 28, 23, 59, 59, tzinfo=self.ist)

    def test_analytics_merge_scans_into_placements(self):
        """QRScanDaily rows + GuestStay rows → merged output."""
        # Create scan data
        QRScanDaily.objects.create(
            qr_code=self.qr, date=datetime.date(2026, 2, 25),
            scan_count=50, unique_visitors=30,
        )

        # Create a verified session via GuestStay
        stay = GuestStay.objects.create(
            guest=self.guest_user, hotel=self.hotel, qr_code=self.qr,
            room_number='101',
            expires_at=timezone.now() + timedelta(days=3),
        )
        # Set created_at within the range
        GuestStay.objects.filter(pk=stay.pk).update(
            created_at=datetime.datetime(2026, 2, 25, 10, 0, tzinfo=self.ist),
        )

        result = get_qr_placement_stats(self.hotel, self.start_dt, self.end_dt)
        self.assertEqual(len(result), 1)

        entry = result[0]
        self.assertEqual(entry['placement'], 'LOBBY')
        self.assertEqual(entry['scans'], 50)
        self.assertEqual(entry['unique_visitors'], 30)
        self.assertEqual(entry['sessions'], 1)

    def test_analytics_scan_only_placement(self):
        """Placement with scans but no verified sessions → included with sessions=0."""
        # Create a second QR with a different placement
        qr_pool = QRCode.objects.create(
            hotel=self.hotel, code='POOL01', placement='POOL', is_active=True,
        )
        QRScanDaily.objects.create(
            qr_code=qr_pool, date=datetime.date(2026, 2, 25),
            scan_count=20, unique_visitors=15,
        )

        result = get_qr_placement_stats(self.hotel, self.start_dt, self.end_dt)
        pool_entry = next((r for r in result if r['placement'] == 'POOL'), None)
        self.assertIsNotNone(pool_entry)
        self.assertEqual(pool_entry['scans'], 20)
        self.assertEqual(pool_entry['unique_visitors'], 15)
        self.assertEqual(pool_entry['sessions'], 0)
        self.assertEqual(pool_entry['with_requests'], 0)
