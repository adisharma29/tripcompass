import logging
import re
import zoneinfo
from datetime import date, timedelta
from urllib.parse import urlparse

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def check_escalations_task():
    """Runs every 5 min via Celery Beat.
    Skips hotels where escalation_enabled=False.
    Idempotent with atomic claim (see plan S4)."""
    from .services import check_escalations
    check_escalations()


@shared_task
def expire_stale_stays_task():
    """Runs hourly. Deactivates GuestStay records whose expires_at has passed."""
    from .models import GuestStay
    now = timezone.now()
    expired = GuestStay.objects.filter(
        is_active=True,
        expires_at__lte=now,
    ).update(is_active=False)
    if expired:
        logger.info('Deactivated %d expired guest stays', expired)


@shared_task
def expire_stale_requests_task():
    """Runs hourly. Marks CREATED requests older than 72h as EXPIRED."""
    from .services import expire_stale_requests
    expire_stale_requests()


@shared_task
def response_due_reminder_task():
    """Runs every 5 min. Sends reminder if response_due_at passed
    and request still CREATED."""
    from .services import send_response_due_reminders
    send_response_due_reminders()


@shared_task
def otp_wa_fallback_sweep_task():
    """Runs every 10 seconds. Catches WhatsApp OTPs that never got
    delivery confirmation and fires SMS fallback.

    Uses a claim pattern: sets sms_fallback_claimed_at atomically,
    then sends SMS outside the lock. Stale claims (>60s) are retryable.
    """
    from django.db.models import Q
    from .models import OTPCode
    from .services import send_sms_fallback_for_otp

    now = timezone.now()
    timeout = settings.GUPSHUP_WA_FALLBACK_TIMEOUT_SECONDS
    cutoff = now - timedelta(seconds=timeout)
    expiry_window = now - timedelta(seconds=settings.OTP_EXPIRY_SECONDS)
    stale_claim_cutoff = now - timedelta(seconds=60)

    candidates = OTPCode.objects.filter(
        created_at__lt=cutoff,
        created_at__gt=expiry_window,
        wa_delivered=False,
        sms_fallback_sent=False,
        is_used=False,
    ).filter(
        # Unclaimed or stale claim (crashed before completing)
        Q(sms_fallback_claimed_at__isnull=True) | Q(sms_fallback_claimed_at__lt=stale_claim_cutoff),
    )

    for otp in candidates:
        # Claim atomically
        with transaction.atomic():
            try:
                locked = OTPCode.objects.select_for_update(
                    skip_locked=True,
                ).filter(
                    Q(sms_fallback_claimed_at__isnull=True) | Q(sms_fallback_claimed_at__lt=stale_claim_cutoff),
                ).get(
                    pk=otp.pk,
                    sms_fallback_sent=False,
                    is_used=False,
                )
            except OTPCode.DoesNotExist:
                continue
            locked.sms_fallback_claimed_at = now
            locked.save(update_fields=['sms_fallback_claimed_at'])

        # Send SMS outside the row lock
        send_sms_fallback_for_otp(locked)


@shared_task
def expire_top_deals_task():
    """Runs every 5 minutes. Clears is_top_deal when deal_ends_at has passed."""
    from .models import Experience
    now = timezone.now()
    expired = Experience.objects.filter(
        is_top_deal=True,
        deal_ends_at__isnull=False,
        deal_ends_at__lt=now,
    ).update(is_top_deal=False, deal_price_display='', deal_ends_at=None)
    if expired:
        logger.info('Cleared %d expired top deals', expired)


@shared_task
def expire_events_task():
    """Runs hourly. Auto-unpublishes published events whose event_end has passed."""
    from .models import ContentStatus, Event

    now = timezone.now()

    # One-time events: event_end in the past
    expired_onetime = Event.objects.filter(
        status=ContentStatus.PUBLISHED,
        auto_expire=True,
        is_recurring=False,
        event_end__isnull=False,
        event_end__lt=now,
    ).update(status=ContentStatus.UNPUBLISHED, is_active=False)

    # Recurring events: recurrence_rule.until in the past (evaluated in hotel tz)
    recurring_candidates = Event.objects.filter(
        status=ContentStatus.PUBLISHED,
        auto_expire=True,
        is_recurring=True,
    ).select_related('hotel')

    expired_recurring = 0
    for event in recurring_candidates:
        rule = event.recurrence_rule or {}
        until_str = rule.get('until')
        if until_str:
            try:
                hotel_tz = zoneinfo.ZoneInfo(event.hotel.timezone)
                hotel_today = now.astimezone(hotel_tz).date()
                until_date = date.fromisoformat(until_str)
                if until_date < hotel_today:
                    event.status = ContentStatus.UNPUBLISHED
                    event.is_active = False
                    event.save(update_fields=['status', 'is_active', 'updated_at'])
                    expired_recurring += 1
            except (ValueError, TypeError, KeyError):
                pass

    total = expired_onetime + expired_recurring
    if total:
        logger.info(
            'Auto-expired %d events (%d one-time, %d recurring)',
            total, expired_onetime, expired_recurring,
        )


@shared_task
def cleanup_expired_otps_task():
    """Runs daily. Deletes OTPCode records older than 24 hours."""
    from .models import OTPCode
    cutoff = timezone.now() - timedelta(hours=24)
    deleted, _ = OTPCode.objects.filter(created_at__lt=cutoff).delete()
    logger.info('Cleaned up %d expired OTP records', deleted)


@shared_task
def daily_digest_task():
    """Runs daily. Sends digest notification per hotel."""
    from .models import Hotel, Notification
    from .services import get_dashboard_stats

    for hotel in Hotel.objects.filter(is_active=True):
        stats = get_dashboard_stats(hotel)
        if stats['total_requests'] == 0:
            continue

        # Notify all admins/superadmins
        from .models import HotelMembership
        admins = HotelMembership.objects.filter(
            hotel=hotel,
            is_active=True,
            role__in=[HotelMembership.Role.ADMIN, HotelMembership.Role.SUPERADMIN],
        ).select_related('user')

        for m in admins:
            Notification.objects.create(
                user=m.user,
                hotel=hotel,
                title='Daily Summary',
                body=(
                    f"{stats['total_requests']} requests today — "
                    f"{stats['confirmed']} confirmed, "
                    f"{stats['pending']} pending"
                ),
                notification_type=Notification.NotificationType.DAILY_DIGEST,
            )


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_staff_invite_notification_task(self, user_id, hotel_id, role, resolved_channels=None):
    """Send WhatsApp/email invitation to newly added staff member (background).

    Tracks resolved channels (succeeded OR permanently failed) so retries
    skip them — avoiding duplicate sends and pointless permanent-failure retries.
    """
    from django.contrib.auth import get_user_model
    from .models import Hotel
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        hotel = Hotel.objects.get(id=hotel_id)
    except (User.DoesNotExist, Hotel.DoesNotExist):
        return

    skip = set(resolved_channels or [])

    from .services import send_staff_invite_notification
    try:
        send_staff_invite_notification(user, hotel, role, skip_channels=skip)
    except Exception as exc:
        # Merge any newly resolved channels (sent or permanently rejected)
        skip = skip | getattr(exc, '_resolved_channels', set())
        logger.warning(
            'Staff invite notification partially failed (attempt %d/%d, resolved=%s): %s',
            self.request.retries + 1, self.max_retries + 1, skip, exc,
        )
        if self.request.retries >= self.max_retries:
            logger.error(
                'Staff invite notification exhausted retries for user=%s hotel=%s. '
                'Resolved: %s. Manual follow-up may be needed.',
                user_id, hotel_id, skip,
            )
            return
        raise self.retry(exc=exc, kwargs={
            'user_id': user_id, 'hotel_id': hotel_id,
            'role': role, 'resolved_channels': list(skip),
        })


# ---------------------------------------------------------------------------
# Orphaned content-image cleanup
# ---------------------------------------------------------------------------

_IMG_SRC_RE = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']*)["\']', re.IGNORECASE)


def _list_storage_files(storage, prefix):
    """Recursively list all file paths under *prefix* in *storage*."""
    try:
        dirs, files = storage.listdir(prefix)
    except (FileNotFoundError, OSError):
        return []
    result = [f'{prefix}/{f}' for f in files if f]
    for d in dirs:
        if d:
            result.extend(_list_storage_files(storage, f'{prefix}/{d}'))
    return result


def _url_to_content_path(url):
    """Extract the ``content/…`` storage path from an image URL.

    Works for both local (``/media/content/…``) and CDN
    (``https://cdn.example.com/content/…``) URLs.
    """
    try:
        path = urlparse(url).path
    except Exception:
        path = url
    marker = 'content/'
    idx = path.find(marker)
    if idx == -1:
        return None
    return path[idx:]


def cleanup_orphaned_content_images(min_age_hours=24, dry_run=False):
    """Delete ``content/*`` files not referenced in any model HTML field.

    Args:
        min_age_hours: Skip files younger than this (avoids racing with
            editors that upload before saving the form).
        dry_run: If True, log what *would* be deleted but don't touch storage.

    Returns:
        ``(deleted_count, total_scanned)`` tuple.
    """
    from django.core.files.storage import default_storage

    from .models import Department, Event, Experience, Hotel, HotelInfoSection

    # 1. List every file under content/
    stored = _list_storage_files(default_storage, 'content')
    if not stored:
        return (0, 0)

    # 2. Filter to files older than min_age_hours
    cutoff = timezone.now() - timedelta(hours=min_age_hours)
    candidates = []
    for path in stored:
        try:
            modified = default_storage.get_modified_time(path)
            if timezone.is_naive(modified):
                modified = timezone.make_aware(modified)
            if modified < cutoff:
                candidates.append(path)
        except Exception:
            # Can't determine age → don't delete
            continue

    if not candidates:
        return (0, len(stored))

    # 3. Collect every content/* path referenced in HTML fields
    referenced = set()
    html_sources = [
        Hotel.objects.filter(description__contains='<img').values_list(
            'description', flat=True,
        ),
        Department.objects.filter(description__contains='<img').values_list(
            'description', flat=True,
        ),
        Experience.objects.filter(description__contains='<img').values_list(
            'description', flat=True,
        ),
        Event.objects.filter(description__contains='<img').values_list(
            'description', flat=True,
        ),
        HotelInfoSection.objects.filter(body__contains='<img').values_list(
            'body', flat=True,
        ),
    ]
    for qs in html_sources:
        for html in qs.iterator():
            if not html:
                continue
            for m in _IMG_SRC_RE.finditer(html):
                sp = _url_to_content_path(m.group(1))
                if sp:
                    referenced.add(sp)

    # 4. Delete orphans
    orphaned = [p for p in candidates if p not in referenced]
    deleted = 0
    for path in orphaned:
        if dry_run:
            logger.info('[DRY RUN] Would delete: %s', path)
        else:
            try:
                default_storage.delete(path)
                deleted += 1
            except Exception:
                logger.warning('Failed to delete orphaned content image: %s', path)

    return (len(orphaned) if dry_run else deleted, len(stored))


@shared_task
def cleanup_orphaned_content_images_task():
    """Runs weekly. Deletes content/* images not referenced in any HTML field."""
    deleted, total = cleanup_orphaned_content_images(min_age_hours=24)
    if deleted:
        logger.info(
            'Deleted %d orphaned content images (%d total scanned)',
            deleted, total,
        )
