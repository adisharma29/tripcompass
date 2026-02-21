import logging
from datetime import timedelta

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
