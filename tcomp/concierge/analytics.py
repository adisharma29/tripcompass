"""Analytics query functions for the concierge dashboard.

All time-based aggregations use the hotel's timezone for correct bucketing.
All functions accept an optional department filter (used for STAFF role).
"""

import zoneinfo
from datetime import datetime, time, timedelta

from django.db.models import (
    Avg,
    Case,
    Count,
    F,
    Q,
    Sum,
    When,
)
from django.db.models.functions import (
    ExtractHour,
    ExtractWeekDay,
    TruncDate,
)
from django.utils import timezone

from .models import (
    Department,
    ContentStatus,
    GuestStay,
    QRScanDaily,
    RequestActivity,
    ServiceRequest,
)


def _parse_date_range(params, hotel_tz):
    """Parse date range from query params. Returns (start_dt, end_dt) as
    timezone-aware datetimes in the hotel's timezone.

    Accepts:
      - ?range=1d|7d|30d|90d
      - ?start=YYYY-MM-DD&end=YYYY-MM-DD  (takes precedence)

    Returns (start_dt, end_dt, error_string_or_None).
    """
    start_str = params.get('start')
    end_str = params.get('end')

    now_hotel = timezone.now().astimezone(hotel_tz)
    today_hotel = now_hotel.date()

    if start_str or end_str:
        if not (start_str and end_str):
            return None, None, 'Both start and end are required for custom date range.'
        try:
            start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_str, '%Y-%m-%d').date()
        except ValueError:
            return None, None, 'Invalid date format. Use YYYY-MM-DD.'

        if start_date > end_date:
            return None, None, 'start must be before or equal to end.'
        if end_date > today_hotel:
            end_date = today_hotel
        if (end_date - start_date).days > 90:
            return None, None, 'Maximum date range is 90 days.'
    else:
        range_str = params.get('range', '7d')
        range_map = {'1d': 1, '7d': 7, '30d': 30, '90d': 90}
        days = range_map.get(range_str)
        if days is None:
            return None, None, f'Invalid range. Use one of: {", ".join(range_map)}.'
        end_date = today_hotel
        start_date = today_hotel - timedelta(days=days - 1)

    # Convert to timezone-aware datetimes (start of start_date, end of end_date)
    start_dt = datetime.combine(start_date, time.min).replace(tzinfo=hotel_tz)
    end_dt = datetime.combine(end_date, time.max).replace(tzinfo=hotel_tz)

    return start_dt, end_dt, None


def _base_qs(hotel, department=None, start_dt=None, end_dt=None):
    """Build a base ServiceRequest queryset with common filters."""
    qs = ServiceRequest.objects.filter(hotel=hotel)
    if department:
        qs = qs.filter(department=department)
    if start_dt and end_dt:
        qs = qs.filter(created_at__range=(start_dt, end_dt))
    return qs


def get_overview_stats(hotel, start_dt, end_dt, department=None):
    """KPI summary with trends vs previous period of same length."""
    hotel_tz = zoneinfo.ZoneInfo(hotel.timezone)
    period_days = (end_dt.date() - start_dt.date()).days + 1
    prev_end = start_dt - timedelta(seconds=1)
    prev_start = datetime.combine(
        start_dt.date() - timedelta(days=period_days), time.min,
    ).replace(tzinfo=hotel_tz)

    current_qs = _base_qs(hotel, department, start_dt, end_dt)
    prev_qs = _base_qs(hotel, department, prev_start, prev_end)

    def _compute(qs):
        # Main aggregate — no activities JOIN to avoid row inflation
        agg = qs.aggregate(
            total=Count('id'),
            confirmed=Count('id', filter=Q(status=ServiceRequest.Status.CONFIRMED)),
            avg_ack=Avg(
                F('acknowledged_at') - F('created_at'),
                filter=Q(acknowledged_at__isnull=False),
            ),
        )
        # Escalated count needs activities JOIN — keep separate to avoid inflating other counts
        escalated = qs.filter(
            activities__action=RequestActivity.Action.ESCALATED,
        ).distinct().count()

        total = agg['total']
        confirmed = agg['confirmed']
        conversion = (confirmed / total * 100) if total > 0 else 0
        avg_ack = agg['avg_ack']
        avg_ack_minutes = avg_ack.total_seconds() / 60 if avg_ack else None

        return {
            'total': total,
            'confirmed': confirmed,
            'conversion': round(conversion, 1),
            'avg_response_min': round(avg_ack_minutes, 1) if avg_ack_minutes is not None else None,
            'escalated': escalated,
        }

    current = _compute(current_qs)
    prev = _compute(prev_qs)

    def _trend(curr_val, prev_val):
        if curr_val is None or prev_val is None:
            return None
        if prev_val == 0:
            return 100.0 if curr_val > 0 else 0.0
        return round((curr_val - prev_val) / prev_val * 100, 1)

    return {
        'total_requests': current['total'],
        'confirmed': current['confirmed'],
        'escalated': current['escalated'],
        'conversion_rate': current['conversion'],
        'avg_response_min': current['avg_response_min'],
        'trends': {
            'total_requests': _trend(current['total'], prev['total']),
            'confirmed': _trend(current['confirmed'], prev['confirmed']),
            'escalated': _trend(current['escalated'], prev['escalated']),
            'conversion_rate': _trend(current['conversion'], prev['conversion']),
            'avg_response_min': _trend(current['avg_response_min'], prev['avg_response_min']),
        },
        'period_days': period_days,
    }


def get_requests_over_time(hotel, start_dt, end_dt, department=None):
    """Time series of requests and confirmations per day."""
    hotel_tz = zoneinfo.ZoneInfo(hotel.timezone)
    qs = _base_qs(hotel, department, start_dt, end_dt)

    rows = (
        qs
        .annotate(date=TruncDate('created_at', tzinfo=hotel_tz))
        .values('date')
        .annotate(
            total=Count('id'),
            confirmed=Count('id', filter=Q(status=ServiceRequest.Status.CONFIRMED)),
        )
        .order_by('date')
    )

    # Fill gaps for dates with zero requests
    result = {}
    for row in rows:
        result[row['date'].isoformat()] = {
            'date': row['date'].isoformat(),
            'total': row['total'],
            'confirmed': row['confirmed'],
        }

    current = start_dt.date()
    end = end_dt.date()
    filled = []
    while current <= end:
        key = current.isoformat()
        filled.append(result.get(key, {'date': key, 'total': 0, 'confirmed': 0}))
        current += timedelta(days=1)

    return filled


def get_department_stats(hotel, start_dt, end_dt):
    """Per-department breakdown. Hotel-wide only (STAFF gets 403).

    Includes all departments that either are PUBLISHED or had request
    activity in the period, so historical/unpublished dept data is not lost.
    """
    qs = _base_qs(hotel, start_dt=start_dt, end_dt=end_dt)

    dept_data = (
        qs
        .values('department__id', 'department__name', 'department__slug')
        .annotate(
            requests=Count('id'),
            confirmed=Count('id', filter=Q(status=ServiceRequest.Status.CONFIRMED)),
        )
    )

    # Avg response time per department (timedelta can't use FloatField output)
    avg_response_by_dept = {}
    acked_qs = qs.exclude(acknowledged_at__isnull=True)
    for row in acked_qs.values('department__id').annotate(
        avg_dur=Avg(F('acknowledged_at') - F('created_at')),
    ):
        dur = row['avg_dur']
        if dur is not None:
            avg_response_by_dept[row['department__id']] = round(dur.total_seconds() / 60, 1)

    # Build lookup from departments that had requests
    lookup = {}
    for row in dept_data:
        dept_id = row['department__id']
        if dept_id is None:
            continue
        lookup[dept_id] = {
            'name': row['department__name'],
            'slug': row['department__slug'],
            'requests': row['requests'],
            'confirmed': row['confirmed'],
            'avg_response_min': avg_response_by_dept.get(dept_id),
        }

    # Also include published departments with zero requests
    published_depts = Department.objects.filter(
        hotel=hotel, status=ContentStatus.PUBLISHED,
    ).order_by('display_order')

    for dept in published_depts:
        if dept.id not in lookup:
            lookup[dept.id] = {
                'name': dept.name,
                'slug': dept.slug,
                'requests': 0,
                'confirmed': 0,
                'avg_response_min': None,
            }

    # Sort: departments with requests first (desc), then published with zero
    result = sorted(lookup.values(), key=lambda d: d['requests'], reverse=True)
    return result


def get_experience_stats(hotel, start_dt, end_dt, department=None):
    """Per-experience breakdown sorted by request count."""
    qs = _base_qs(hotel, department, start_dt, end_dt).exclude(experience__isnull=True)

    rows = (
        qs
        .values(
            'experience__id',
            'experience__name',
            'experience__department__name',
        )
        .annotate(
            requests=Count('id'),
            confirmed=Count('id', filter=Q(status=ServiceRequest.Status.CONFIRMED)),
        )
        .order_by('-requests')
    )

    result = []
    for row in rows:
        total = row['requests']
        confirmed = row['confirmed']
        result.append({
            'name': row['experience__name'],
            'department': row['experience__department__name'],
            'requests': total,
            'confirmed': confirmed,
            'conversion_pct': round(confirmed / total * 100, 1) if total > 0 else 0,
        })

    return result


def get_response_times(hotel, start_dt, end_dt, department=None):
    """Response time distribution buckets + staff leaderboard."""
    qs = _base_qs(hotel, department, start_dt, end_dt).exclude(
        acknowledged_at__isnull=True,
    )

    # --- Buckets ---
    bucket_defs = [
        ('< 5 min', 0, 5),
        ('5–15 min', 5, 15),
        ('15–30 min', 15, 30),
        ('30–60 min', 30, 60),
        ('> 60 min', 60, None),
    ]

    total_acked = qs.count()

    # Fetch all response times and bucket in Python — at <1K requests this is fine
    response_times = list(
        qs.annotate(
            response_delta=F('acknowledged_at') - F('created_at'),
        ).values_list('response_delta', flat=True)
    )

    buckets = []
    for label, low, high in bucket_defs:
        count = 0
        for delta in response_times:
            minutes = delta.total_seconds() / 60
            if high is not None:
                if low <= minutes < high:
                    count += 1
            else:
                if minutes >= low:
                    count += 1
        pct = round(count / total_acked * 100, 1) if total_acked > 0 else 0
        buckets.append({'label': label, 'count': count, 'pct': pct})

    # --- Staff leaderboard ---
    # Get requests that were acknowledged, grouped by assigned_to
    staff_qs = _base_qs(hotel, department, start_dt, end_dt).exclude(
        assigned_to__isnull=True,
    )

    staff_rows = (
        staff_qs
        .values(
            'assigned_to__id',
            'assigned_to__first_name',
            'assigned_to__last_name',
        )
        .annotate(
            handled=Count('id'),
            confirmed_count=Count('id', filter=Q(status=ServiceRequest.Status.CONFIRMED)),
            avg_response=Avg(
                Case(
                    When(
                        acknowledged_at__isnull=False,
                        then=F('acknowledged_at') - F('created_at'),
                    ),
                ),
            ),
        )
        .order_by('-handled')
    )

    leaderboard = []
    for row in staff_rows:
        avg_dur = row['avg_response']
        avg_min = round(avg_dur.total_seconds() / 60, 1) if avg_dur else None
        handled = row['handled']
        confirmed = row['confirmed_count']
        name = f"{row['assigned_to__first_name']} {row['assigned_to__last_name']}".strip()
        leaderboard.append({
            'name': name or 'Unknown',
            'handled': handled,
            'avg_response_min': avg_min,
            'confirmed': confirmed,
            'confirmed_pct': round(confirmed / handled * 100, 1) if handled > 0 else 0,
        })

    return {
        'total_acknowledged': total_acked,
        'buckets': buckets,
        'leaderboard': leaderboard,
    }


def get_heatmap_data(hotel, start_dt, end_dt, department=None):
    """7×24 matrix of request counts by day-of-week × hour.

    Returns a 7-element list (Mon=0 through Sun=6), each containing
    a 24-element list of counts (hour 0 through 23).
    """
    hotel_tz = zoneinfo.ZoneInfo(hotel.timezone)
    qs = _base_qs(hotel, department, start_dt, end_dt)

    rows = (
        qs
        .annotate(
            dow=ExtractWeekDay('created_at', tzinfo=hotel_tz),
            hour=ExtractHour('created_at', tzinfo=hotel_tz),
        )
        .values('dow', 'hour')
        .annotate(count=Count('id'))
    )

    # Django's ExtractWeekDay: 1=Sunday, 2=Monday, ..., 7=Saturday
    # Convert to 0=Monday, 1=Tuesday, ..., 6=Sunday
    matrix = [[0] * 24 for _ in range(7)]
    for row in rows:
        django_dow = row['dow']  # 1=Sun, 2=Mon, ..., 7=Sat
        # Convert: Mon=0 → django 2, Tue=1 → django 3, ..., Sun=6 → django 1
        mon_based = (django_dow - 2) % 7
        matrix[mon_based][row['hour']] = row['count']

    return matrix


def get_qr_placement_stats(hotel, start_dt, end_dt):
    """QR code performance by placement (lobby, room, pool, etc.).

    Merges two data sources:
    - QRScanDaily: raw scans (pre-verification)
    - GuestStay: verified sessions + request conversion
    """
    # Verified sessions from GuestStay
    verified_rows = (
        GuestStay.objects.filter(
            hotel=hotel,
            created_at__range=(start_dt, end_dt),
            qr_code__isnull=False,
        )
        .values('qr_code__placement')
        .annotate(
            sessions=Count('id'),
            with_requests=Count(
                'id',
                filter=Q(requests__isnull=False),
                distinct=True,
            ),
        )
        .order_by('-sessions')
    )

    result_map = {}
    for row in verified_rows:
        placement = row['qr_code__placement']
        sessions = row['sessions']
        with_req = row['with_requests']
        result_map[placement] = {
            'placement': placement,
            'sessions': sessions,
            'with_requests': with_req,
            'conversion_pct': round(with_req / sessions * 100, 1) if sessions > 0 else 0,
            'scans': 0,
            'unique_visitors': 0,
        }

    # Raw scans from QRScanDaily
    scan_rows = (
        QRScanDaily.objects.filter(
            qr_code__hotel=hotel,
            date__range=(start_dt.date(), end_dt.date()),
        )
        .values('qr_code__placement')
        .annotate(
            total_scans=Sum('scan_count'),
            total_unique=Sum('unique_visitors'),
        )
    )

    for row in scan_rows:
        placement = row['qr_code__placement']
        if placement in result_map:
            result_map[placement]['scans'] = row['total_scans']
            result_map[placement]['unique_visitors'] = row['total_unique']
        else:
            result_map[placement] = {
                'placement': placement,
                'sessions': 0,
                'with_requests': 0,
                'conversion_pct': 0,
                'scans': row['total_scans'],
                'unique_visitors': row['total_unique'],
            }

    return sorted(result_map.values(), key=lambda r: r['scans'], reverse=True)
