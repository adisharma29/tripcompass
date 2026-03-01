import datetime
import hashlib
import io
import json
import logging
import random
import re
import secrets
import string
import zoneinfo
from datetime import timedelta

import redis as redis_lib
import requests as http_requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.contrib.auth.tokens import default_token_generator
from django.core.signing import TimestampSigner
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from .models import (
    ContentStatus, Hotel, HotelMembership, Department, Experience,
    GuestStay, OTPCode, ServiceRequest, RequestActivity, Notification,
    PushSubscription, QRCode, EscalationHeartbeat,
    Rating, RatingPrompt,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# QR Code generation
# ---------------------------------------------------------------------------

def generate_qr(hotel, label, placement, department=None, created_by=None):
    """Generate a QR code PNG pointing to the hotel's guest page."""
    import qrcode

    code = secrets.token_urlsafe(6)
    target_url = f'{settings.FRONTEND_ORIGIN}/h/{hotel.slug}?qr={code}'

    img = qrcode.make(target_url, box_size=10, border=4)
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)

    qr = QRCode(
        hotel=hotel,
        code=code,
        placement=placement,
        label=label,
        department=department,
        is_active=True,
        created_by=created_by,
    )
    qr.qr_image.save(f'qr_{code}.png', ContentFile(buffer.read()), save=False)
    qr.save()
    return qr


# ---------------------------------------------------------------------------
# OTP
# ---------------------------------------------------------------------------

def _hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _hash_ip(ip):
    return hashlib.sha256(ip.encode()).hexdigest()


def generate_otp_code():
    return ''.join(secrets.choice(string.digits) for _ in range(settings.OTP_CODE_LENGTH))


class OTPDeliveryError(Exception):
    """Raised when all OTP delivery channels fail."""
    pass


def send_otp(phone, ip_address='', hotel=None):
    """Generate OTP, store hashed, attempt WhatsApp delivery with SMS fallback.

    Raises OTPDeliveryError if neither channel succeeds.
    """
    code = generate_otp_code()
    now = timezone.now()
    expires = now + timedelta(seconds=settings.OTP_EXPIRY_SECONDS)

    # In DEBUG mode, log the code so developers can verify without real delivery
    if settings.DEBUG:
        logger.info('DEV OTP for %s: %s', phone, code)

    otp = OTPCode.objects.create(
        phone=phone,
        code_hash=_hash_code(code),
        hotel=hotel,
        channel=OTPCode.Channel.WHATSAPP,
        ip_hash=_hash_ip(ip_address) if ip_address else '',
        expires_at=expires,
    )

    # In DEBUG mode, skip delivery unless WhatsApp key is configured (sandbox)
    if settings.DEBUG and not settings.GUPSHUP_WA_API_KEY:
        return

    # Attempt WhatsApp delivery
    wa_success = _send_whatsapp_otp(otp, code)

    if not wa_success:
        # Mark fallback BEFORE sending so a crash after SMS delivery
        # won't leave the row looking unsent (which would let the sweeper
        # generate a new code and invalidate the delivered one).
        otp.channel = OTPCode.Channel.SMS
        otp.sms_fallback_sent = True
        otp.save(update_fields=['channel', 'sms_fallback_sent'])

        sms_ok = _send_sms_otp(otp, code)
        if not sms_ok:
            # Both channels failed. Mark OTP as used (unverifiable) but keep
            # the row for DB-fallback rate limiting. User must request a new OTP.
            otp.channel = OTPCode.Channel.WHATSAPP
            otp.sms_fallback_sent = False
            otp.is_used = True
            otp.save(update_fields=['channel', 'sms_fallback_sent', 'is_used'])
            logger.error('Both WhatsApp and SMS failed for OTP %s', otp.id)
            raise OTPDeliveryError('Unable to send OTP. Please try again later.')

    return otp


def _send_whatsapp_otp(otp, code):
    """Send OTP via Gupshup WhatsApp API. Returns True on success."""
    api_key = settings.GUPSHUP_WA_API_KEY
    if not api_key:
        logger.warning('Gupshup WhatsApp API key not configured, skipping WA')
        return False

    try:
        payload = {
            'channel': 'whatsapp',
            'source': settings.GUPSHUP_WA_SOURCE_PHONE,
            'destination': otp.phone,
            'src.name': settings.GUPSHUP_WA_APP_NAME,
            'template': json.dumps({
                'id': settings.GUPSHUP_WA_OTP_TEMPLATE_ID,
                'params': [code, code],
            }),
        }
        resp = http_requests.post(
            'https://api.gupshup.io/wa/api/v1/template/msg',
            data=payload,
            headers={'apikey': api_key},
            timeout=10,
        )
        if resp.status_code in (200, 202):
            data = resp.json()
            if data.get('status') == 'submitted':
                otp.gupshup_message_id = data.get('messageId', '')
                otp.save(update_fields=['gupshup_message_id'])
                return True
        logger.warning('WhatsApp OTP send failed: %s %s', resp.status_code, resp.text)
        return False
    except Exception:
        logger.exception('WhatsApp OTP send error')
        return False


def _send_sms_otp(otp, code):
    """Send OTP via Gupshup Enterprise SMS API."""
    userid = settings.GUPSHUP_SMS_USERID
    if not userid:
        logger.warning('Gupshup SMS credentials not configured, skipping SMS')
        return False

    msg = settings.GUPSHUP_SMS_OTP_MSG_TEMPLATE.replace('%code%', code)

    try:
        payload = {
            'method': 'TWO_FACTOR_AUTH',
            'userid': userid,
            'password': settings.GUPSHUP_SMS_PASSWORD,
            'phone_no': otp.phone,
            'msg': msg,
            'otpCodeLength': str(settings.OTP_CODE_LENGTH),
            'otpCodeType': 'NUMERIC',
            'v': '1.1',
            'format': 'text',
            'mask': settings.GUPSHUP_SMS_SENDER_MASK,
            'dltTemplateId': settings.GUPSHUP_SMS_DLT_TEMPLATE_ID,
            'principalEntityId': settings.GUPSHUP_SMS_PRINCIPAL_ENTITY_ID,
        }
        resp = http_requests.post(
            'https://enterprise.smsgupshup.com/GatewayAPI/rest',
            data=payload,
            timeout=10,
        )
        parts = resp.text.strip().split('|')
        if parts[0].strip().lower() == 'success':
            return True
        logger.warning('SMS OTP send failed: %s', resp.text)
        return False
    except Exception:
        logger.exception('SMS OTP send error')
        return False


def send_sms_fallback_for_otp(otp):
    """Fire SMS fallback for an existing OTP record (called by webhook/sweeper).

    Uses a claim pattern: the caller sets sms_fallback_claimed_at before
    invoking this function. This function generates a new code, sends SMS,
    then persists code_hash + sms_fallback_sent on success. On failure,
    the claim is cleared so the sweeper can retry. Stale claims (>60s)
    are also retryable by the sweeper.

    Crash windows:
    - Crash after claim, before SMS send → claim expires, sweeper retries.
    - Crash after SMS send, before save → code_hash not updated, but the
      new code was sent. The old WA code still verifies (code_hash unchanged).
      Sweeper will retry and send another SMS with a fresh code, which is
      acceptable (user gets a second SMS, both old WA and new SMS codes work
      until the retry succeeds and updates code_hash).
    """
    logger.info('SMS fallback triggered for OTP %s (phone: %s)', otp.id, otp.phone)
    new_code = generate_otp_code()

    sms_ok = _send_sms_otp(otp, new_code)
    if sms_ok:
        # Delivery confirmed — persist new code_hash + mark sent atomically
        otp.code_hash = _hash_code(new_code)
        otp.channel = OTPCode.Channel.SMS
        otp.sms_fallback_sent = True
        otp.save(update_fields=['code_hash', 'channel', 'sms_fallback_sent'])
    else:
        # Clear claim so sweeper can retry
        otp.sms_fallback_claimed_at = None
        otp.save(update_fields=['sms_fallback_claimed_at'])
        logger.error('SMS fallback send failed for OTP %s', otp.id)


def verify_otp(phone, code, hotel=None, qr_code_str=None):
    """Verify OTP code. Returns (user, guest_stay_or_none).

    - Phone matches existing STAFF user → (user, None)
    - Guest/unknown + hotel → (user, stay)
    - Guest/unknown + no hotel → raises ValidationError

    Uses select_for_update + transaction.atomic to prevent concurrent
    double-use of the same OTP.
    """
    from django.contrib.auth import get_user_model
    from rest_framework.exceptions import ValidationError

    User = get_user_model()
    now = timezone.now()

    # --- Wrong-code handling: increment attempts in its own atomic block
    # so the counter persists even when we raise ValidationError after.
    otp_error = None

    with transaction.atomic():
        # Scope OTP lookup by hotel context:
        # - Guest flow (hotel provided): only match OTPs issued for this exact hotel.
        # - Staff flow (no hotel): match any OTP for this phone.
        otp_qs = OTPCode.objects.select_for_update().filter(
            phone=phone,
            is_used=False,
            expires_at__gt=now,
            attempts__lt=settings.OTP_MAX_ATTEMPTS,
        )
        if hotel:
            otp_qs = otp_qs.filter(hotel=hotel)

        otp = otp_qs.order_by('-created_at').first()

        if not otp:
            raise ValidationError('Invalid or expired OTP.')

        if _hash_code(code) != otp.code_hash:
            # Persist the increment — commit happens when this block exits normally.
            OTPCode.objects.filter(pk=otp.pk).update(attempts=F('attempts') + 1)
            new_attempts = OTPCode.objects.filter(pk=otp.pk).values_list('attempts', flat=True).first()
            if new_attempts >= settings.OTP_MAX_ATTEMPTS:
                otp_error = 'Too many attempts. Please request a new code.'
            else:
                otp_error = 'Invalid code.'

    # Raise outside the atomic block so the attempts increment is committed.
    if otp_error:
        raise ValidationError(otp_error)

    with transaction.atomic():
        # Re-fetch under lock for the success path (code was correct above).
        otp = OTPCode.objects.select_for_update().filter(
            pk=otp.pk, is_used=False,
        ).first()
        if not otp:
            raise ValidationError('Invalid or expired OTP.')

        # Check if phone belongs to existing staff user.
        # Try normalized (digits-only) first, fall back to +prefixed
        # for pre-backfill compatibility.
        user = (
            User.objects.filter(phone=phone, user_type='STAFF').first()
            or User.objects.filter(phone=f'+{phone}', user_type='STAFF').first()
        )
        if user:
            if not user.is_active:
                raise ValidationError('Account is disabled.')
            otp.is_used = True
            otp.save(update_fields=['is_used'])
            return user, None

        # Guest flow — hotel is required (validate before consuming the OTP)
        if not hotel:
            raise ValidationError('hotel_slug is required for non-staff users.')

        # Check if existing guest is disabled before consuming the OTP
        existing_guest = (
            User.objects.filter(phone=phone, user_type='GUEST').first()
            or User.objects.filter(phone=f'+{phone}', user_type='GUEST').first()
        )
        if existing_guest and not existing_guest.is_active:
            raise ValidationError('Account is disabled.')

        # Mark as used atomically — concurrent requests block on select_for_update
        # and will see is_used=True when they acquire the lock.
        otp.is_used = True
        otp.save(update_fields=['is_used'])

    # Outside the lock: user/stay creation doesn't need the OTP row locked.
    # get-or-create with IntegrityError retry for concurrent requests.
    if existing_guest:
        user = existing_guest
    else:
        try:
            user = User.objects.create_guest_user(phone=phone)
        except IntegrityError:
            # Another request created the user between our get() and create()
            user = (
                User.objects.filter(phone=phone, user_type='GUEST').first()
                or User.objects.get(phone=f'+{phone}', user_type='GUEST')
            )

    # Resolve QR code
    qr = None
    if qr_code_str:
        try:
            qr = QRCode.objects.get(
                code=qr_code_str, hotel=hotel, is_active=True,
            )
        except QRCode.DoesNotExist:
            qr = None  # Silently ignore invalid QR

    # Reuse active stay for this hotel, or create a new one.
    # Atomic + row lock prevents double-verify races.
    new_expiry = now + timedelta(hours=24)
    with transaction.atomic():
        existing_stay = (
            GuestStay.objects.select_for_update()
            .filter(guest=user, hotel=hotel, is_active=True)
            .order_by('-created_at')
            .first()
        )
        if existing_stay:
            existing_stay.expires_at = new_expiry
            update_fields = ['expires_at']
            if qr:
                existing_stay.qr_code = qr
                update_fields.append('qr_code')
            existing_stay.save(update_fields=update_fields)
            stay = existing_stay
        else:
            stay = GuestStay.objects.create(
                guest=user,
                hotel=hotel,
                qr_code=qr,
                expires_at=new_expiry,
            )

    return user, stay


def handle_wa_delivery_event(payload):
    """Process Gupshup WhatsApp webhook delivery events."""
    event_type = payload.get('type', '')
    message_id = payload.get('id', '') or payload.get('messageId', '')

    if not message_id:
        return

    if event_type in ('delivered', 'read'):
        OTPCode.objects.filter(
            gupshup_message_id=message_id,
        ).update(wa_delivered=True)
        return

    if event_type == 'failed':
        error_code = payload.get('payload', {}).get('code', '')
        logger.warning('WhatsApp delivery failed: msg=%s code=%s', message_id, error_code)

        now = timezone.now()
        stale_claim_cutoff = now - timedelta(seconds=60)

        # Claim atomically — same pattern as sweeper to prevent races
        with transaction.atomic():
            try:
                otp = OTPCode.objects.select_for_update(
                    skip_locked=True,
                ).filter(
                    Q(sms_fallback_claimed_at__isnull=True) | Q(sms_fallback_claimed_at__lt=stale_claim_cutoff),
                ).get(
                    gupshup_message_id=message_id,
                    sms_fallback_sent=False,
                    is_used=False,
                    expires_at__gt=now,
                )
            except OTPCode.DoesNotExist:
                return

            otp.sms_fallback_claimed_at = now
            otp.save(update_fields=['sms_fallback_claimed_at'])

        # Send SMS outside the lock
        send_sms_fallback_for_otp(otp)


# ---------------------------------------------------------------------------
# Rate limiting (custom with DB fallback)
# ---------------------------------------------------------------------------

def check_rate_limit(key, limit, window_seconds):
    """Check rate limit using Redis. Returns True/False, or None if cache unavailable."""
    try:
        count = cache.get(key)
        if count is None:
            cache.set(key, 1, window_seconds)
            return True
        if count >= limit:
            return False
        cache.incr(key)
        return True
    except Exception:
        logger.warning('Cache unavailable for rate limit key=%s', key)
        return None  # Signal caller to use DB fallback


def check_otp_rate_limit_phone(phone, limit, window_seconds):
    """Rate limit OTP sends per phone, with DB fallback."""
    key = f'ratelimit:otp:phone:{phone}'
    result = check_rate_limit(key, limit, window_seconds)
    if result is not None:
        return result
    # DB fallback: count recent OTP records for this phone
    cutoff = timezone.now() - timedelta(seconds=window_seconds)
    count = OTPCode.objects.filter(phone=phone, created_at__gte=cutoff).count()
    return count < limit


def check_otp_rate_limit_ip(ip_hash, limit, window_seconds):
    """Rate limit OTP sends per IP, with DB fallback."""
    key = f'ratelimit:otp:ip:{ip_hash}'
    result = check_rate_limit(key, limit, window_seconds)
    if result is not None:
        return result
    # DB fallback: count recent OTP records for this IP hash
    cutoff = timezone.now() - timedelta(seconds=window_seconds)
    count = OTPCode.objects.filter(ip_hash=ip_hash, created_at__gte=cutoff).count()
    return count < limit


def check_otp_rate_limit_email(email, limit, window_seconds):
    """Rate limit OTP sends per email, with DB fallback."""
    key = f'ratelimit:otp:email:{email}'
    result = check_rate_limit(key, limit, window_seconds)
    if result is not None:
        return result
    cutoff = timezone.now() - timedelta(seconds=window_seconds)
    count = OTPCode.objects.filter(email=email, created_at__gte=cutoff).count()
    return count < limit


def send_email_otp(email, ip_address=''):
    """Generate OTP, store hashed, send via email.

    Silently succeeds even if no staff user exists with this email
    (prevents email enumeration).
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Check if a staff user exists with this email — if not, create a tracking
    # row for DB-fallback rate limiting and silently return.
    user = User.objects.filter(email__iexact=email, user_type='STAFF').order_by('date_joined').first()
    if not user:
        logger.info('Email OTP requested for non-existent staff email %s — silent no-op', email)
        OTPCode.objects.create(
            email=email.lower(),
            code_hash='',
            channel=OTPCode.Channel.EMAIL,
            ip_hash=_hash_ip(ip_address) if ip_address else '',
            is_used=True,
            expires_at=timezone.now(),
        )
        return None

    code = generate_otp_code()
    now = timezone.now()
    expires = now + timedelta(seconds=settings.OTP_EXPIRY_SECONDS)

    if settings.DEBUG:
        logger.info('DEV EMAIL OTP for %s: %s', email, code)

    otp = OTPCode.objects.create(
        email=email.lower(),
        code_hash=_hash_code(code),
        channel=OTPCode.Channel.EMAIL,
        ip_hash=_hash_ip(ip_address) if ip_address else '',
        expires_at=expires,
    )

    # In DEBUG mode, skip delivery unless Resend key is configured
    if settings.DEBUG and not settings.RESEND_API_KEY:
        return otp

    success = _send_otp_email(email, code)
    if not success:
        otp.is_used = True
        otp.save(update_fields=['is_used'])
        raise OTPDeliveryError('Unable to send verification code. Please try again later.')

    return otp


def _send_otp_email(email, code):
    """Send OTP code via Resend email. Returns True on success."""
    api_key = settings.RESEND_API_KEY
    if not api_key:
        logger.warning('Resend API key not configured, skipping email OTP')
        return False

    import resend
    resend.api_key = api_key

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 40px 24px;">
      <h2 style="color: #1a1a1a; font-size: 22px; margin-bottom: 8px;">Your verification code</h2>
      <p style="color: #555; font-size: 15px; line-height: 1.6; margin-bottom: 24px;">
        Use the code below to log in to your Refuje dashboard.
        This code expires in 10 minutes.
      </p>
      <div style="background: #f5f5f5; border-radius: 8px; padding: 20px; text-align: center; margin-bottom: 24px;">
        <span style="font-size: 32px; font-weight: 700; letter-spacing: 6px; color: #1a1a1a;">{code}</span>
      </div>
      <p style="color: #888; font-size: 13px; line-height: 1.5;">
        If you didn't request this code, you can safely ignore this email.
      </p>
      <hr style="border: none; border-top: 1px solid #eee; margin: 28px 0 16px;" />
      <p style="color: #aaa; font-size: 12px;">Powered by Refuje</p>
    </div>
    """

    from resend.exceptions import (
        ValidationError, MissingRequiredFieldsError,
        MissingApiKeyError, InvalidApiKeyError,
    )
    _PERMANENT = (ValidationError, MissingRequiredFieldsError, MissingApiKeyError, InvalidApiKeyError)

    try:
        response = resend.Emails.send({
            'from': settings.RESEND_FROM_EMAIL,
            'to': [email],
            'subject': 'Your Refuje verification code',
            'html': html,
            'tags': [
                {'name': 'type', 'value': 'email_otp'},
            ],
        })
        logger.info('Email OTP sent to %s (id=%s)', email, response.get('id'))
        return True
    except _PERMANENT as exc:
        logger.warning('Email OTP permanently rejected for %s: %s', email, exc)
        return False
    except Exception as exc:
        logger.error('Email OTP delivery failed for %s: %s', email, exc)
        return False


def verify_email_otp(email, code):
    """Verify email OTP code. Returns user or raises ValidationError.

    Staff-only: rejects if user has no active staff membership.
    """
    from django.contrib.auth import get_user_model
    from rest_framework.exceptions import ValidationError

    User = get_user_model()
    now = timezone.now()

    otp_error = None

    with transaction.atomic():
        otp_qs = OTPCode.objects.select_for_update().filter(
            email=email.lower(),
            channel=OTPCode.Channel.EMAIL,
            is_used=False,
            expires_at__gt=now,
            attempts__lt=settings.OTP_MAX_ATTEMPTS,
        )

        otp = otp_qs.order_by('-created_at').first()

        if not otp:
            raise ValidationError('Invalid or expired code.')

        if _hash_code(code) != otp.code_hash:
            OTPCode.objects.filter(pk=otp.pk).update(attempts=F('attempts') + 1)
            new_attempts = OTPCode.objects.filter(pk=otp.pk).values_list('attempts', flat=True).first()
            if new_attempts >= settings.OTP_MAX_ATTEMPTS:
                otp_error = 'Too many attempts. Please request a new code.'
            else:
                otp_error = 'Invalid code.'

    if otp_error:
        raise ValidationError(otp_error)

    with transaction.atomic():
        otp = OTPCode.objects.select_for_update().filter(
            pk=otp.pk, is_used=False,
        ).first()
        if not otp:
            raise ValidationError('Invalid or expired code.')

        user = User.objects.filter(email__iexact=email, user_type='STAFF').order_by('date_joined').first()
        if not user:
            raise ValidationError('No staff account found for this email.')
        if not user.is_active:
            raise ValidationError('Account is disabled.')

        # Verify user has at least one active hotel membership
        if not HotelMembership.objects.filter(user=user, is_active=True).exists():
            raise ValidationError('No active hotel membership found.')

        otp.is_used = True
        otp.save(update_fields=['is_used'])
        return user


def check_stay_rate_limit(stay):
    """Max 10 requests per stay per hour."""
    key = f'ratelimit:stay:{stay.id}'
    result = check_rate_limit(key, 10, 3600)
    if result is not None:
        return result
    # DB fallback
    one_hour_ago = timezone.now() - timedelta(hours=1)
    count = ServiceRequest.objects.filter(
        guest_stay=stay, created_at__gte=one_hour_ago,
    ).count()
    return count < 10


def check_room_rate_limit(hotel, room_number):
    """Max 5 requests per (hotel, room) per hour."""
    key = f'ratelimit:room:{hotel.id}:{room_number}'
    result = check_rate_limit(key, 5, 3600)
    if result is not None:
        return result
    # DB fallback
    one_hour_ago = timezone.now() - timedelta(hours=1)
    count = ServiceRequest.objects.filter(
        hotel=hotel,
        guest_stay__room_number=room_number,
        created_at__gte=one_hour_ago,
    ).count()
    return count < 5


# ---------------------------------------------------------------------------
# Guest invite rate limiting
# ---------------------------------------------------------------------------

def check_invite_rate_limit_phone(phone):
    """Max 3 invites per phone per 24 hours."""
    key = f'ratelimit:invite:phone:{phone}'
    result = check_rate_limit(key, 3, 86400)
    if result is not None:
        return result
    from .models import GuestInvite
    cutoff = timezone.now() - timedelta(hours=24)
    count = GuestInvite.objects.filter(guest_phone=phone, created_at__gte=cutoff).count()
    return count < 3


def check_invite_rate_limit_hotel(hotel_id):
    """Max 100 invites per hotel per 24 hours."""
    key = f'ratelimit:invite:hotel:{hotel_id}'
    result = check_rate_limit(key, 100, 86400)
    if result is not None:
        return result
    from .models import GuestInvite
    cutoff = timezone.now() - timedelta(hours=24)
    count = GuestInvite.objects.filter(hotel_id=hotel_id, created_at__gte=cutoff).count()
    return count < 100


def check_invite_rate_limit_staff(user_id):
    """Debounce: max 1 invite per staff per 10 seconds."""
    key = f'ratelimit:invite:staff:{user_id}'
    result = check_rate_limit(key, 1, 10)
    if result is not None:
        return result
    from .models import GuestInvite
    cutoff = timezone.now() - timedelta(seconds=10)
    count = GuestInvite.objects.filter(sent_by_id=user_id, created_at__gte=cutoff).count()
    return count < 1


def check_invite_resend_rate_limit(user_id):
    """Debounce resend: max 1 resend per staff per 10 seconds.

    Separate cache key from create so create + immediate resend don't
    cross-contaminate each other's debounce windows.

    Fails closed: if cache is unavailable, resend is denied. DeliveryRecord
    has no created_by field, so a reliable per-actor DB fallback isn't
    possible. A brief cache outage blocking resends is acceptable since
    the original invite is still valid.
    """
    key = f'ratelimit:invite_resend:staff:{user_id}'
    result = check_rate_limit(key, 1, 10)
    if result is not None:
        return result
    return False


# ---------------------------------------------------------------------------
# Service request SLA helpers
# ---------------------------------------------------------------------------

def _is_in_windows(time_str, windows):
    """Check if time_str falls within any of the schedule windows."""
    for window in windows:
        if len(window) != 2:
            continue
        start, end = window
        if start <= end:
            # Same-day window, e.g. ["09:00", "17:00"]
            if start <= time_str <= end:
                return True
        else:
            # Overnight window, e.g. ["22:00", "02:00"]
            if time_str >= start or time_str <= end:
                return True
    return False


def _get_windows(schedule, day_name):
    """Get schedule windows for a day, checking overrides first then default."""
    overrides = schedule.get('overrides', {})
    if day_name in overrides:
        return overrides[day_name]
    return schedule.get('default', [['00:00', '23:59']])


def is_department_after_hours(department):
    """Check if the department is currently outside its scheduled hours.

    Handles overnight windows that span day boundaries: if yesterday had
    ["22:00","02:00"], then today at 01:00 is still within that window.
    """
    import zoneinfo
    from datetime import timedelta as td

    schedule = department.schedule or {}
    tz_name = schedule.get('timezone', 'Asia/Kolkata')
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = zoneinfo.ZoneInfo('Asia/Kolkata')

    now_local = timezone.now().astimezone(tz)
    today_name = now_local.strftime('%A').lower()
    time_str = now_local.strftime('%H:%M')

    # Check today's windows
    today_windows = _get_windows(schedule, today_name)
    if _is_in_windows(time_str, today_windows):
        return False

    # Check yesterday's overnight windows that extend past midnight
    yesterday_local = now_local - td(days=1)
    yesterday_name = yesterday_local.strftime('%A').lower()
    yesterday_windows = _get_windows(schedule, yesterday_name)
    for window in yesterday_windows:
        if len(window) == 2 and window[0] > window[1]:
            # Overnight window — check if current time is in the "after midnight" portion
            if time_str <= window[1]:
                return False

    return True


def compute_response_due_at(hotel):
    """Return the response_due_at timestamp based on the first escalation tier."""
    tiers = hotel.escalation_tier_minutes or settings.ESCALATION_TIER_MINUTES
    if tiers:
        return timezone.now() + timedelta(minutes=tiers[0])
    return None


# ---------------------------------------------------------------------------
# Push notifications
# ---------------------------------------------------------------------------

def send_push_notification(user, title, body, url=None):
    """Send Web Push notification to all active subscriptions for a user."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning('pywebpush not installed')
        return

    subs = PushSubscription.objects.filter(user=user, is_active=True)
    payload = json.dumps({
        'title': title,
        'body': body,
        'url': url or '/',
    })

    vapid_claims = {
        'sub': f'mailto:{settings.WEBPUSH_VAPID_ADMIN_EMAIL}',
    }

    for sub in subs:
        try:
            webpush(
                subscription_info=sub.subscription_info,
                data=payload,
                vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
                vapid_claims=vapid_claims,
            )
        except Exception as e:
            logger.warning('Push send failed for sub %s: %s', sub.id, e)
            if '410' in str(e) or '404' in str(e):
                sub.is_active = False
                sub.save(update_fields=['is_active'])


def notify_department_staff(department, request_obj):
    """Create Notification + push for all active staff/admin of a department."""
    memberships = HotelMembership.objects.filter(
        hotel=request_obj.hotel,
        is_active=True,
    ).filter(
        Q(department=department) |
        Q(role__in=[HotelMembership.Role.ADMIN, HotelMembership.Role.SUPERADMIN])
    ).select_related('user')

    for m in memberships:
        Notification.objects.create(
            user=m.user,
            hotel=request_obj.hotel,
            request=request_obj,
            title=f'New request: {request_obj.department.name}',
            body=f'Room {request_obj.guest_stay.room_number} - {request_obj.request_type}',
            notification_type=Notification.NotificationType.NEW_REQUEST,
        )
        send_push_notification(
            m.user,
            title=f'New request: {request_obj.department.name}',
            body=f'Room {request_obj.guest_stay.room_number}',
            url=f'/dashboard/requests/{request_obj.public_id}',
        )


def notify_after_hours_fallback(request_obj):
    """Send informational notification to fallback department for after-hours requests."""
    hotel = request_obj.hotel
    fallback = hotel.fallback_department
    if not fallback:
        return

    memberships = HotelMembership.objects.filter(
        hotel=hotel,
        department=fallback,
        is_active=True,
    ).select_related('user')

    for m in memberships:
        Notification.objects.create(
            user=m.user,
            hotel=hotel,
            request=request_obj,
            title=f'After-hours request: {request_obj.department.name}',
            body=f'Room {request_obj.guest_stay.room_number} - department is closed',
            notification_type=Notification.NotificationType.NEW_REQUEST,
        )


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def check_escalations():
    """Check for requests that need escalation. Called by Celery task."""
    now = timezone.now()
    hotels = Hotel.objects.filter(is_active=True, escalation_enabled=True)

    for hotel in hotels:
        tier_minutes = hotel.escalation_tier_minutes or settings.ESCALATION_TIER_MINUTES
        pending_requests = ServiceRequest.objects.filter(
            hotel=hotel,
            status=ServiceRequest.Status.CREATED,
        ).select_related('department', 'guest_stay')

        for req in pending_requests:
            elapsed = (now - req.created_at).total_seconds() / 60

            for tier_idx, threshold in enumerate(tier_minutes, start=1):
                if elapsed >= threshold:
                    _fire_escalation(req, tier_idx, hotel)

    # Write heartbeat
    EscalationHeartbeat.objects.update_or_create(
        task_name='check_escalations',
        defaults={
            'last_run': now,
            'status': EscalationHeartbeat.HeartbeatStatus.OK,
        },
    )


def _fire_escalation(request_obj, tier, hotel):
    """Insert escalation event + claim + send notification. Idempotent."""
    # Step 1: Insert (atomic, partial unique index rejects duplicates)
    try:
        activity = RequestActivity.objects.create(
            request=request_obj,
            action=RequestActivity.Action.ESCALATED,
            escalation_tier=tier,
            details={'department_id': request_obj.department_id},
        )
    except IntegrityError:
        # Already exists — proceed to claim for possible retry
        activity = RequestActivity.objects.filter(
            request=request_obj,
            action=RequestActivity.Action.ESCALATED,
            escalation_tier=tier,
        ).first()
        if not activity:
            return

    # Step 2: Claim pending delivery
    stale_cutoff = timezone.now() - timedelta(minutes=5)
    with transaction.atomic():
        claimable = RequestActivity.objects.select_for_update(
            skip_locked=True,
        ).filter(
            id=activity.id,
            notified_at__isnull=True,
        ).filter(
            Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_cutoff),
        ).filter(
            request__status=ServiceRequest.Status.CREATED,
        )

        updated = claimable.update(claimed_at=timezone.now())
        if not updated:
            return  # Already claimed by another runner

    # Step 3: Send notification
    try:
        from .notifications import NotificationEvent, dispatch_notification
        dispatch_notification(NotificationEvent(
            event_type='escalation',
            hotel=hotel,
            department=request_obj.department,
            request=request_obj,
            event_obj=request_obj.event,
            offering_obj=request_obj.special_request_offering,
            escalation_tier=tier,
        ))
        activity.notified_at = timezone.now()
        activity.save(update_fields=['notified_at'])
    except Exception:
        logger.exception('Escalation notification failed for request %s tier %s',
                         request_obj.public_id, tier)


def expire_stale_requests():
    """Mark CREATED requests older than 72h as EXPIRED."""
    cutoff = timezone.now() - timedelta(hours=72)
    expired = ServiceRequest.objects.filter(
        status=ServiceRequest.Status.CREATED,
        created_at__lt=cutoff,
    )
    for req in expired:
        req.status = ServiceRequest.Status.EXPIRED
        req.save(update_fields=['status', 'updated_at'])
        RequestActivity.objects.create(
            request=req,
            action=RequestActivity.Action.EXPIRED,
        )


def send_response_due_reminders():
    """Send reminder if response_due_at passed and request still CREATED.
    Only sends once per request (sets reminder_sent_at to prevent duplicates)."""
    now = timezone.now()
    overdue = ServiceRequest.objects.filter(
        status=ServiceRequest.Status.CREATED,
        response_due_at__lt=now,
        reminder_sent_at__isnull=True,
    ).select_related('department', 'hotel', 'guest_stay', 'special_request_offering')

    from .notifications import NotificationEvent, dispatch_notification
    for req in overdue:
        dispatch_notification(NotificationEvent(
            event_type='response_due',
            hotel=req.hotel,
            department=req.department,
            request=req,
            event_obj=req.event,
            offering_obj=req.special_request_offering,
        ))
        req.reminder_sent_at = now
        req.save(update_fields=['reminder_sent_at'])


# ---------------------------------------------------------------------------
# SSE (Server-Sent Events via Redis pub/sub)
# ---------------------------------------------------------------------------

def get_sse_redis():
    """Get a Redis client for SSE pub/sub."""
    return redis_lib.from_url(settings.SSE_REDIS_URL)


def publish_request_event(hotel, event_type, request_obj):
    """Publish an SSE event to the hotel's Redis channel."""
    channel = f'hotel:{hotel.id}:requests'
    payload = json.dumps({
        'event': event_type,
        'request_id': request_obj.id,
        'public_id': str(request_obj.public_id),
        'status': request_obj.status,
        'department_id': request_obj.department_id,
        'updated_at': request_obj.updated_at.isoformat(),
        'event_id': request_obj.event_id,
        'event_name': request_obj.event.name if request_obj.event_id else None,
    })
    try:
        r = get_sse_redis()
        r.publish(channel, payload)
    except Exception:
        logger.exception('Failed to publish SSE event')


def publish_invite_event(hotel_id, delivery_id, invite_id, status):
    """Publish an invite delivery status update to the hotel's SSE channel."""
    channel = f'hotel:{hotel_id}:requests'
    payload = json.dumps({
        'event': 'invite.delivery_updated',
        'delivery_id': delivery_id,
        'invite_id': invite_id,
        'status': status,
        'updated_at': timezone.now().isoformat(),
    })
    try:
        r = get_sse_redis()
        r.publish(channel, payload)
    except Exception:
        logger.exception('Failed to publish invite SSE event')


async def stream_request_events(hotel, user):
    """Async SSE generator. Subscribes to Redis pub/sub, yields events.

    Uses pubsub.get_message(timeout=N) which blocks on the Redis socket for
    up to N seconds per call — not busy polling. Returns None on timeout,
    giving us a natural heartbeat injection point without cancelling any
    coroutine (unlike asyncio.wait_for which destroys the iterator).

    Django 5.x cancels streaming coroutines on disconnect/shutdown via
    asyncio.CancelledError, which propagates out of get_message and through
    the generator's finally block for cleanup.
    """
    import asyncio

    import redis.asyncio as aioredis
    from asgiref.sync import sync_to_async

    # Pre-fetch membership once (avoids repeated ORM calls per message)
    membership = await sync_to_async(
        lambda: HotelMembership.objects.filter(
            user=user, hotel=hotel, is_active=True,
        ).select_related('department').first()
    )()

    is_staff_role = membership and membership.role == HotelMembership.Role.STAFF
    staff_department_id = None
    if is_staff_role:
        staff_department_id = membership.department_id if membership.department else None

    r = aioredis.from_url(settings.SSE_REDIS_URL)
    pubsub = r.pubsub()
    channel = f'hotel:{hotel.id}:requests'
    await pubsub.subscribe(channel)

    heartbeat_interval = getattr(settings, 'SSE_HEARTBEAT_SECONDS', 15)

    try:
        while True:
            # Blocks on the Redis socket for up to heartbeat_interval seconds.
            # Returns None on timeout, a dict on message.
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=heartbeat_interval,
            )

            if message is None:
                # No message within heartbeat window — send keepalive
                yield ': heartbeat\n\n'
                continue

            if message['type'] != 'message':
                continue

            data = json.loads(message['data'])

            # STAFF users: skip events without department_id (e.g. rating events)
            # and filter to their assigned department.
            # STAFF with no assigned department see no events at all.
            if is_staff_role:
                event_dept = data.get('department_id')
                if event_dept is None:
                    continue
                if staff_department_id is None:
                    # Unassigned STAFF: skip all dept-scoped events
                    continue
                if event_dept != staff_department_id:
                    continue

            yield f'event: {data["event"]}\ndata: {json.dumps(data)}\n\n'
    except asyncio.CancelledError:
        raise
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await r.aclose()


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

def get_dashboard_stats(hotel, department=None):
    """Aggregate stats for the dashboard."""
    qs = ServiceRequest.objects.filter(hotel=hotel)
    if department:
        qs = qs.filter(department=department)

    # Compute "today" in the hotel's configured timezone (fallback to UTC)
    try:
        hotel_tz = zoneinfo.ZoneInfo(hotel.timezone) if hotel.timezone else zoneinfo.ZoneInfo('UTC')
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        hotel_tz = zoneinfo.ZoneInfo('UTC')
    now_local = timezone.now().astimezone(hotel_tz)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + datetime.timedelta(days=1)
    today_qs = qs.filter(created_at__gte=today_start, created_at__lt=tomorrow_start)

    # Single aggregate for all status counts
    agg = today_qs.aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(status=ServiceRequest.Status.CREATED)),
        acknowledged=Count('id', filter=Q(status=ServiceRequest.Status.ACKNOWLEDGED)),
        confirmed=Count('id', filter=Q(status=ServiceRequest.Status.CONFIRMED)),
    )
    total = agg['total']
    pending = agg['pending']
    acknowledged = agg['acknowledged']
    confirmed = agg['confirmed']

    conversion_rate = (confirmed / total * 100) if total > 0 else 0

    # Per-department counts in a single query, keyed by id to avoid name collisions
    dept_counts = dict(
        today_qs.filter(
            department__status=ContentStatus.PUBLISHED,
        ).values_list('department_id').annotate(c=Count('id')).values_list('department_id', 'c')
    )
    # Preserve ordering for departments that may have zero requests
    depts = Department.objects.filter(
        hotel=hotel, status=ContentStatus.PUBLISHED,
    ).values_list('id', 'name')
    by_department = [{'name': name, 'count': dept_counts.get(did, 0)} for did, name in depts]

    # Single aggregate for setup flags
    dept_qs = hotel.departments.all()
    setup_agg = dept_qs.aggregate(
        has_departments=Count('id'),
        has_published=Count('id', filter=Q(status=ContentStatus.PUBLISHED)),
        has_dept_photos=Count('id', filter=~Q(photo='')),
    )
    exp_agg = Experience.objects.filter(department__hotel=hotel).aggregate(
        has_experiences=Count('id'),
        has_exp_photos=Count('id', filter=~Q(photo='')),
    )
    setup = {
        'settings_configured': hotel.settings_configured,
        'settings_partial': bool(
            hotel.settings_configured
            and not (hotel.timezone and hotel.whatsapp_number)
        ),
        'has_departments': setup_agg['has_departments'] > 0,
        'has_experiences': exp_agg['has_experiences'] > 0,
        'has_photos': setup_agg['has_dept_photos'] > 0 or exp_agg['has_exp_photos'] > 0,
        'has_team': hotel.memberships.filter(is_active=True).count() > 1,
        'has_qr_codes': hotel.qr_codes.exists(),
        'has_published': setup_agg['has_published'] > 0,
    }

    # %-d is Linux/macOS only; backend is always Docker Linux.
    return {
        'total_requests': total,
        'pending': pending,
        'acknowledged': acknowledged,
        'confirmed': confirmed,
        'conversion_rate': round(conversion_rate, 1),
        'by_department': by_department,
        'setup': setup,
        'period_label': 'Today',
        'period_date_display': now_local.strftime('%a, %-d %b'),
        'period_date': now_local.strftime('%Y-%m-%d'),
        'period_timezone': str(hotel_tz),
    }


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

def validate_and_process_image(file):
    """Validate and process uploaded image. Wrapper around validators."""
    from .validators import validate_image_upload
    return validate_image_upload(file)


# ---------------------------------------------------------------------------
# Staff invitation notifications
# ---------------------------------------------------------------------------

def _normalize_phone(phone):
    """Strip to digits only (E.164 without '+').  e.g. '+91 918-755-1736' → '919187551736'."""
    return re.sub(r'\D', '', phone)


def send_staff_invite_whatsapp(phone, first_name, hotel_name, role_display):
    """Send WhatsApp invitation to newly added staff member.

    Raises on transient errors (network, timeout) so the Celery task can retry.
    Returns False only for permanent/config failures (missing keys, API rejection).
    """
    api_key = settings.GUPSHUP_WA_API_KEY
    template_id = settings.GUPSHUP_WA_STAFF_INVITE_TEMPLATE_ID
    if not api_key or not template_id:
        logger.warning('Staff invite WA skipped: missing API key or template ID')
        return False

    normalized = _normalize_phone(phone)
    if len(normalized) < 10:
        logger.warning('Staff invite WA skipped: phone too short after normalization: %s', phone)
        return False

    payload = {
        'channel': 'whatsapp',
        'source': settings.GUPSHUP_WA_SOURCE_PHONE,
        'destination': normalized,
        'src.name': settings.GUPSHUP_WA_APP_NAME,
        'template': json.dumps({
            'id': template_id,
            'params': [hotel_name, first_name or 'there', hotel_name, role_display],
        }),
    }

    # Let network/timeout errors propagate → Celery task retries.
    resp = http_requests.post(
        'https://api.gupshup.io/wa/api/v1/template/msg',
        data=payload,
        headers={'apikey': api_key},
        timeout=10,
    )
    # 5xx = transient provider error → raise so Celery retries
    if resp.status_code >= 500:
        raise RuntimeError(
            f'Gupshup server error {resp.status_code}: {resp.text[:200]}'
        )
    # Parse JSON safely — malformed responses on 4xx are permanent failures
    try:
        data = resp.json()
    except (ValueError, TypeError):
        if resp.status_code in (200, 202):
            # Success status but unparseable body — treat as transient
            raise RuntimeError(f'Gupshup returned non-JSON on {resp.status_code}: {resp.text[:200]}')
        logger.warning('Staff invite WA rejected (non-JSON): %s %s', resp.status_code, resp.text[:200])
        return False
    if resp.status_code in (200, 202) and data.get('status') == 'submitted':
        logger.info('Staff invite WA sent to %s (msgId=%s)', normalized, data.get('messageId'))
        return True
    # 4xx = permanent (bad template, invalid number, auth error)
    logger.warning('Staff invite WA rejected: %s %s', resp.status_code, data)
    return False


def generate_password_token(user):
    """Generate a uid + token pair for set-password / reset-password flows.

    Uses Django's PasswordResetTokenGenerator — the token is HMAC-signed
    against user.pk + password hash + last_login, so it auto-invalidates
    the moment the user sets/changes their password.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return uid, token


def verify_password_token(uidb64, token):
    """Verify a password-set/reset token. Returns user or None."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None
    if default_token_generator.check_token(user, token):
        return user
    return None


def send_staff_invite_email(user, hotel_name, role_display):
    """Send email invitation with set-password link to newly added staff member."""
    api_key = settings.RESEND_API_KEY
    if not api_key:
        logger.warning('Staff invite email skipped: missing RESEND_API_KEY')
        return False

    import resend
    resend.api_key = api_key

    uid, token = generate_password_token(user)
    set_password_url = f'{settings.FRONTEND_ORIGIN}/set-password?uid={uid}&token={token}'
    greeting = user.first_name or 'there'
    email = user.email

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 40px 24px;">
      <h2 style="color: #1a1a1a; font-size: 22px; margin-bottom: 8px;">You're invited to {hotel_name}</h2>
      <p style="color: #555; font-size: 15px; line-height: 1.6; margin-bottom: 20px;">
        Hi {greeting}, the team at <strong>{hotel_name}</strong> has added you as
        <strong>{role_display}</strong> on their concierge platform.
        Set up your password to get started.
      </p>
      <a href="{set_password_url}"
         style="display: inline-block; background: #1a1a1a; color: #ffffff; text-decoration: none;
                padding: 12px 28px; border-radius: 6px; font-size: 15px; font-weight: 500;">
        Set Up Your Password
      </a>
      <p style="color: #888; font-size: 13px; margin-top: 28px; line-height: 1.5;">
        If you didn't expect this invitation, you can safely ignore this email.
      </p>
      <hr style="border: none; border-top: 1px solid #eee; margin: 28px 0 16px;" />
      <p style="color: #aaa; font-size: 12px;">Powered by Refuje</p>
    </div>
    """

    from resend.exceptions import (
        ValidationError, MissingRequiredFieldsError,
        MissingApiKeyError, InvalidApiKeyError,
    )

    # Known-permanent Resend errors — don't retry these.
    _PERMANENT = (ValidationError, MissingRequiredFieldsError, MissingApiKeyError, InvalidApiKeyError)

    try:
        response = resend.Emails.send({
            'from': settings.RESEND_FROM_EMAIL,
            'to': [email],
            'subject': f"You're invited to {hotel_name}",
            'html': html,
            'tags': [
                {'name': 'type', 'value': 'staff_invite'},
                {'name': 'hotel', 'value': re.sub(r'[^A-Za-z0-9_-]', '-', hotel_name)},
            ],
        })
        logger.info('Staff invite email sent to %s (id=%s)', email, response.get('id'))
        return True
    except _PERMANENT as exc:
        # 400/401/403/422 — permanent, don't retry.
        logger.warning('Staff invite email permanently rejected for %s: %s', email, exc)
        return False
    # All other exceptions (RateLimitError 429, ApplicationError 500, future
    # transient ResendError subclasses, network/DNS errors) propagate → Celery retry.


def send_password_reset_email(user):
    """Send password reset email with token link."""
    api_key = settings.RESEND_API_KEY
    if not api_key:
        logger.warning('Password reset email skipped: missing RESEND_API_KEY')
        return False

    import resend
    resend.api_key = api_key

    uid, token = generate_password_token(user)
    reset_url = f'{settings.FRONTEND_ORIGIN}/reset-password?uid={uid}&token={token}'
    greeting = user.first_name or 'there'

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 40px 24px;">
      <h2 style="color: #1a1a1a; font-size: 22px; margin-bottom: 8px;">Reset your password</h2>
      <p style="color: #555; font-size: 15px; line-height: 1.6; margin-bottom: 20px;">
        Hi {greeting}, we received a request to reset your Refuje password.
        Click the button below to choose a new password.
      </p>
      <a href="{reset_url}"
         style="display: inline-block; background: #1a1a1a; color: #ffffff; text-decoration: none;
                padding: 12px 28px; border-radius: 6px; font-size: 15px; font-weight: 500;">
        Reset Password
      </a>
      <p style="color: #888; font-size: 13px; margin-top: 28px; line-height: 1.5;">
        If you didn't request this, you can safely ignore this email.
        This link will expire when your password is changed.
      </p>
      <hr style="border: none; border-top: 1px solid #eee; margin: 28px 0 16px;" />
      <p style="color: #aaa; font-size: 12px;">Powered by Refuje</p>
    </div>
    """

    from resend.exceptions import (
        ValidationError as ResendValidationError,
        MissingRequiredFieldsError, MissingApiKeyError, InvalidApiKeyError,
    )
    _PERMANENT = (ResendValidationError, MissingRequiredFieldsError, MissingApiKeyError, InvalidApiKeyError)

    try:
        response = resend.Emails.send({
            'from': settings.RESEND_FROM_EMAIL,
            'to': [user.email],
            'subject': 'Reset your Refuje password',
            'html': html,
            'tags': [
                {'name': 'type', 'value': 'password_reset'},
            ],
        })
        logger.info('Password reset email sent to %s (id=%s)', user.email, response.get('id'))
        return True
    except _PERMANENT as exc:
        logger.warning('Password reset email permanently rejected for %s: %s', user.email, exc)
        return False
    except Exception as exc:
        logger.error('Password reset email failed for %s: %s', user.email, exc)
        return False


def send_staff_invite_notification(user, hotel, role, *, skip_channels=None):
    """Send invitation notifications to a newly added staff member.

    Sends WhatsApp if phone is available, email if email is available.
    Returns a set of resolved channels (succeeded or permanently failed).
    Raises on the first transient error after attempting all remaining channels,
    so the Celery task can retry with skip_channels for already-resolved ones.

    "Resolved" means the channel doesn't need to be retried — either it
    succeeded (True) or permanently failed (False). Only transient errors
    (raised exceptions) leave a channel unresolved.
    """
    skip = skip_channels or set()
    role_display = dict(HotelMembership.Role.choices).get(role, role)
    hotel_name = hotel.name
    first_name = user.first_name
    resolved = set(skip)  # carry forward prior resolved channels
    errors = []

    if user.phone and 'whatsapp' not in skip:
        try:
            send_staff_invite_whatsapp(user.phone, first_name, hotel_name, role_display)
            # True = sent, False = permanent failure (bad number, missing config)
            resolved.add('whatsapp')
        except Exception as exc:
            # Transient — leave unresolved so retry attempts again
            logger.exception('Staff invite WA transient error for %s', user.phone)
            errors.append(exc)

    if user.email and 'email' not in skip:
        try:
            send_staff_invite_email(user, hotel_name, role_display)
            # True = sent, False = permanent Resend rejection
            resolved.add('email')
        except Exception as exc:
            # Transient — leave unresolved so retry attempts again
            logger.exception('Staff invite email transient error for %s', user.email)
            errors.append(exc)

    if errors:
        # Attach resolved channels so the Celery task skips them on retry
        errors[0]._resolved_channels = resolved
        raise errors[0]

    return resolved


# ---------------------------------------------------------------------------
# Guest Invite Token Signing
# ---------------------------------------------------------------------------

_invite_signer = TimestampSigner(salt='guest-invite')


def generate_invite_token(invite_id: int, version: int) -> str:
    """Sign invite_id:version. Version is fixed per invite; revoke is the invalidation path."""
    return _invite_signer.sign(f'{invite_id}:{version}')


def verify_invite_token(token: str) -> tuple[int, int]:
    """Verify and return (invite_id, version). Expiry checked via invite.expires_at, not signer max_age."""
    # No max_age on unsign — expiry is checked against invite.expires_at in the
    # verify view. Relying on the DB field means config changes to
    # GUEST_INVITE_EXPIRY_HOURS never conflict with already-issued tokens.
    # The signer only validates integrity (not tampered), not time.
    value = _invite_signer.unsign(token)
    invite_id, version = value.split(':')
    return int(invite_id), int(version)


# ---------------------------------------------------------------------------
# WhatsApp Template Resolution
# ---------------------------------------------------------------------------

def get_template(hotel, template_type):
    """Resolve template: hotel-specific → global default → None."""
    from .models import WhatsAppTemplate

    return (
        WhatsAppTemplate.objects.filter(
            hotel=hotel, template_type=template_type, is_active=True
        ).first()
        or WhatsAppTemplate.objects.filter(
            hotel=None, template_type=template_type, is_active=True
        ).first()
    )


# Context keys available for GUEST_INVITE templates.
# New template types (Phase 2) define their own context builders.
GUEST_INVITE_CONTEXT_BUILDERS = {
    'hotel_name': lambda invite: invite.hotel.name,
    'guest_name': lambda invite: invite.guest_name,
    'room_number': lambda invite: invite.room_number or '',
}

GUEST_RATING_BATCH_CONTEXT_BUILDERS = {
    'guest_name': lambda ctx: ctx['guest_name'],
    'hotel_name': lambda ctx: ctx['hotel_name'],
    'experience_count': lambda ctx: str(ctx['experience_count']),
    'rate_url_suffix': lambda ctx: ctx['rate_url_suffix'],
}

GUEST_STAY_SURVEY_CONTEXT_BUILDERS = {
    'guest_name': lambda ctx: ctx['guest_name'],
    'hotel_name': lambda ctx: ctx['hotel_name'],
    'rate_url_suffix': lambda ctx: ctx['rate_url_suffix'],
}

LOW_SCORE_ALERT_CONTEXT_BUILDERS = {
    'guest_summary': lambda ctx: ctx['guest_summary'],
    'rating_detail': lambda ctx: ctx['rating_detail'],
    'feedback_preview': lambda ctx: ctx['feedback_preview'],
    'dashboard_path': lambda ctx: ctx['dashboard_path'],
}


def resolve_template_params(wa_template, context_source):
    """Build the ordered param list from a template's variables JSON.

    context_source: the object to resolve keys from (e.g., GuestInvite instance).
    Returns list of strings in index order, ready to pass to Gupshup.
    Raises ValueError if a required variable key has no resolver.
    """
    _BUILDERS_MAP = {
        'GUEST_INVITE': GUEST_INVITE_CONTEXT_BUILDERS,
        'GUEST_RATING_BATCH': GUEST_RATING_BATCH_CONTEXT_BUILDERS,
        'GUEST_STAY_SURVEY': GUEST_STAY_SURVEY_CONTEXT_BUILDERS,
        'LOW_SCORE_ALERT': LOW_SCORE_ALERT_CONTEXT_BUILDERS,
    }
    builders = _BUILDERS_MAP.get(wa_template.template_type)
    if builders is None:
        raise ValueError(f'No context builders for template type: {wa_template.template_type}')

    variables = sorted(wa_template.variables, key=lambda v: v['index'])
    params = []
    for var in variables:
        key = var['key']
        if key not in builders:
            raise ValueError(f'Unknown variable key "{key}" in template "{wa_template.name}"')
        params.append(str(builders[key](context_source)))
    return params


def validate_template(wa_template):
    """Ensure body_text placeholders match declared variables."""
    placeholders = {int(m) for m in re.findall(r'\{\{(\d+)\}\}', wa_template.body_text)}
    declared = {v['index'] for v in wa_template.variables}
    if placeholders != declared:
        raise ValidationError(
            f'Placeholder mismatch: body has {placeholders}, variables declare {declared}'
        )


# ---------------------------------------------------------------------------
# Guest Ratings
# ---------------------------------------------------------------------------

def submit_rating(prompt_id, guest, score, feedback):
    """Create Rating, update prompt, handle low-score alert.

    Uses select_for_update + status precondition to prevent races.
    Returns (rating, created) — if already completed, returns existing rating.
    """
    with transaction.atomic():
        try:
            prompt = RatingPrompt.objects.select_for_update().get(
                id=prompt_id,
                guest=guest,
                status='SENT',
            )
        except RatingPrompt.DoesNotExist:
            prompt = RatingPrompt.objects.filter(
                id=prompt_id, guest=guest,
            ).first()
            if prompt is None:
                raise ValidationError('Rating prompt not found.')
            if prompt.status == 'COMPLETED':
                existing = Rating.objects.filter(
                    hotel=prompt.hotel,
                    guest=guest,
                    service_request=prompt.service_request,
                    guest_stay=prompt.guest_stay if prompt.prompt_type == 'STAY' else None,
                    rating_type=prompt.prompt_type,
                ).first()
                if existing:
                    return existing, False
            raise ValidationError('This rating prompt is no longer available.')

        rating = Rating.objects.create(
            hotel=prompt.hotel,
            guest=guest,
            service_request=prompt.service_request,
            guest_stay=prompt.guest_stay,
            rating_type=prompt.prompt_type,
            score=score,
            feedback=feedback,
        )
        prompt.status = 'COMPLETED'
        prompt.completed_at = timezone.now()
        prompt.save(update_fields=['status', 'completed_at'])

        if prompt.service_request:
            RequestActivity.objects.create(
                request=prompt.service_request,
                actor=guest,
                action='RATING_SUBMITTED',
                details={'score': score},
            )

    # Low-score alert (outside transaction — best effort)
    if score <= 3 and feedback:
        try:
            dispatch_low_score_alert(prompt, rating)
        except Exception:
            logger.exception('Failed to dispatch low-score alert for rating %s', rating.id)

    return rating, True


def dispatch_low_score_alert(prompt, rating):
    """Send notifications for low-score ratings to ADMIN/SUPERADMIN users."""
    hotel = prompt.hotel

    # 1. In-app notifications + push for ADMIN/SUPERADMIN
    admin_memberships = HotelMembership.objects.filter(
        hotel=hotel,
        is_active=True,
        role__in=(HotelMembership.Role.ADMIN, HotelMembership.Role.SUPERADMIN),
    ).select_related('user')

    guest_name = rating.guest.get_full_name() or rating.guest.phone or 'Guest'
    room = ''
    if prompt.guest_stay:
        room = prompt.guest_stay.room_number
    elif prompt.service_request and prompt.service_request.guest_stay:
        room = prompt.service_request.guest_stay.room_number

    title = 'Low rating alert'
    body = f'{guest_name} rated {rating.score}/5'
    if room:
        body = f'Room {room} — {body}'

    for membership in admin_memberships:
        Notification.objects.create(
            user=membership.user,
            hotel=hotel,
            request=prompt.service_request,
            title=title,
            body=body,
            notification_type=Notification.NotificationType.LOW_RATING_ALERT,
        )
        # Async push delivery
        from .notifications.tasks import send_push_notification_task
        send_push_notification_task.delay(
            user_id=membership.user.id,
            title=title,
            body=body,
            url='/dashboard/ratings',
        )

    # 2. SSE event
    try:
        publish_rating_event(hotel, 'rating.low_score', rating)
    except Exception:
        logger.exception('Failed to publish rating SSE event')

    # 3. WhatsApp to on-call phone
    if hotel.oncall_phone:
        from .notifications.tasks import send_low_score_whatsapp_task
        send_low_score_whatsapp_task.delay(hotel_id=hotel.id, rating_id=rating.id)

    # 4. Email to on-call email
    if hotel.oncall_email:
        from .notifications.tasks import send_low_score_email_task
        send_low_score_email_task.delay(hotel_id=hotel.id, rating_id=rating.id)

    # 5. Activity trail (request ratings only)
    if prompt.service_request:
        RequestActivity.objects.create(
            request=prompt.service_request,
            actor=rating.guest,
            action='FEEDBACK_ALERT',
            details={'score': rating.score},
        )


def publish_rating_event(hotel, event_type, rating):
    """Publish rating SSE event to the existing request stream channel."""
    channel = f'hotel:{hotel.id}:requests'
    payload = json.dumps({
        'event': event_type,
        'rating_id': rating.id,
        'public_id': str(rating.service_request.public_id) if rating.service_request_id else None,
        'score': rating.score,
        'hotel_id': hotel.id,
    })
    try:
        r = get_sse_redis()
        r.publish(channel, payload)
    except Exception:
        logger.exception('Failed to publish rating SSE event')


def send_stay_survey(hotel, guest_stay, triggered_by):
    """Admin triggers checkout survey. Creates prompt + enqueues WhatsApp."""
    prompt, created = RatingPrompt.objects.get_or_create(
        hotel=hotel,
        guest_stay=guest_stay,
        prompt_type='STAY',
        defaults={
            'guest': guest_stay.guest,
            'status': 'QUEUED',
            'eligible_at': timezone.now(),
            'queued_at': timezone.now(),
        },
    )
    if not created:
        raise ValidationError('Survey already sent for this stay.')

    # WhatsApp delivery — send task transitions QUEUED → SENT on 2xx,
    # or QUEUED → FAILED on 4xx (same lifecycle as batch-sent prompts).
    from .notifications.tasks import send_stay_survey_whatsapp_task
    send_stay_survey_whatsapp_task.delay(
        hotel_id=hotel.id,
        guest_id=guest_stay.guest_id,
        prompt_id=prompt.id,
    )
