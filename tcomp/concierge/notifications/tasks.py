import json
import logging

import requests as http_requests
from celery import shared_task
from django.conf import settings

from concierge.models import DeliveryRecord
from concierge.services import (
    send_push_notification, generate_invite_token,
    get_template, resolve_template_params,
)
from shortlinks.models import ShortLink

# Transient error types that warrant a Celery retry
_TRANSIENT_ERRORS = (
    http_requests.exceptions.ConnectionError,
    http_requests.exceptions.Timeout,
    RuntimeError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=1, default_retry_delay=5)
def send_push_notification_task(self, user_id, title, body, url):
    """Async web-push delivery. Called by PushAdapter after sync Notification creation.

    Separated from PushAdapter.send() so that per-subscription HTTP calls
    (one per device) don't block the request lifecycle.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return  # User deleted between dispatch and task execution

    try:
        send_push_notification(user=user, title=title, body=body, url=url)
    except Exception as exc:
        logger.warning("Web push failed for user %s: %s", user_id, exc)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# WhatsApp — helpers
# ---------------------------------------------------------------------------

def _resolve_template_id(event_type):
    """Map event type to the correct Gupshup template UUID."""
    return {
        "request.created": settings.GUPSHUP_WA_STAFF_REQUEST_TEMPLATE_ID,
        "escalation": settings.GUPSHUP_WA_STAFF_ESCALATION_TEMPLATE_ID,
        "response_due": settings.GUPSHUP_WA_STAFF_RESPONSE_DUE_TEMPLATE_ID,
    }.get(event_type, settings.GUPSHUP_WA_STAFF_REQUEST_TEMPLATE_ID)


def _build_template_params(event_type, params):
    """Build ordered template params list for the given event type."""
    if event_type == "escalation":
        return [
            params["guest_name"], params["room_number"],
            params["department"], params["subject"],
            str(params.get("escalation_tier", 1)),
        ]
    # request.created and response_due use the same 4-param template
    return [
        params["guest_name"], params["room_number"],
        params["department"], params["subject"],
    ]


def _build_session_message(event_type, params):
    """Build interactive session message JSON for the given event type."""
    public_id = params["public_id"]

    if event_type == "escalation":
        tier = params.get("escalation_tier", 1)
        return {
            "type": "quick_reply",
            "msgid": f"esc_{public_id}_{tier}",
            "content": {
                "type": "text",
                "header": "⚠️ Escalation Alert",
                "text": (
                    f"*{params['guest_name']}* (Room {params['room_number']}) needs attention.\n\n"
                    f"Department: {params['department']}\n"
                    f"Subject: {params['subject']}\n"
                    f"Escalation: Tier {tier}"
                ),
            },
            "options": [
                {"type": "text", "title": "On it", "postbackText": f"esc_ack:{public_id}:{tier}"},
                {"type": "text", "title": "View Details", "postbackText": f"view:{public_id}"},
            ],
        }

    if event_type == "response_due":
        header = "Reminder"
        text = (
            f"Request from *{params['guest_name']}* (Room {params['room_number']}) "
            f"is awaiting response.\n\n"
            f"Department: {params['department']}\n"
            f"Subject: {params['subject']}"
        )
    else:
        header = "New Request"
        text = (
            f"*{params['guest_name']}* (Room {params['room_number']})\n\n"
            f"Department: {params['department']}\n"
            f"Subject: {params['subject']}"
        )

    return {
        "type": "quick_reply",
        "msgid": f"req_{public_id}",
        "content": {"type": "text", "header": header, "text": text},
        "options": [
            {"type": "text", "title": "Acknowledge", "postbackText": f"ack:{public_id}"},
            {"type": "text", "title": "View Details", "postbackText": f"view:{public_id}"},
        ],
    }


# ---------------------------------------------------------------------------
# WhatsApp — Celery tasks
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_whatsapp_template_notification(self, delivery_record_id, params):
    """Send a PAID utility template message (no active service window).

    Includes 2 quick-reply buttons so staff can open the 24h window.
    """
    record = DeliveryRecord.objects.get(id=delivery_record_id)
    template_id = _resolve_template_id(record.event_type)
    template_params = _build_template_params(record.event_type, params)
    public_id = params["public_id"]

    # postbackTexts for quick-reply buttons
    view_postback = {"index": 1, "text": f"view:{public_id}"}
    if record.event_type == "escalation":
        tier = params.get("escalation_tier", 1)
        postback_texts = [
            {"index": 0, "text": f"esc_ack:{public_id}:{tier}"},
            view_postback,
        ]
    else:
        postback_texts = [
            {"index": 0, "text": f"ack:{public_id}"},
            view_postback,
        ]

    try:
        response = http_requests.post(
            "https://api.gupshup.io/wa/api/v1/template/msg",
            headers={"apikey": settings.GUPSHUP_WA_API_KEY},
            data={
                "channel": "whatsapp",
                "source": settings.GUPSHUP_WA_SOURCE_PHONE,
                "destination": record.target,
                "src.name": settings.GUPSHUP_WA_APP_NAME,
                "template": json.dumps({
                    "id": template_id,
                    "params": template_params,
                }),
                "postbackTexts": json.dumps(postback_texts),
            },
            timeout=10,
        )
        # Branch by HTTP status first
        status_code = response.status_code
        if status_code >= 500 or status_code == 429:
            raise RuntimeError(f"Gupshup server error {status_code}")
        if status_code >= 400:
            raise ValueError(f"Gupshup client error {status_code}: {response.text[:200]}")

        # 2xx — inspect body for provider business errors
        data = response.json()
        if data.get("status") == "error":
            raise ValueError(f"Gupshup API error: {data.get('message', 'Unknown')}")
        if not data.get("messageId"):
            raise ValueError(f"Gupshup response missing messageId: {data}")

        record.provider_message_id = data["messageId"]
        record.status = DeliveryRecord.Status.SENT
        record.save(update_fields=["provider_message_id", "status"])
    except Exception as exc:
        record.status = DeliveryRecord.Status.FAILED
        record.error_message = str(exc)[:500]
        record.save(update_fields=["status", "error_message"])
        if isinstance(exc, _TRANSIENT_ERRORS):
            raise self.retry(exc=exc)  # Only retry on transient errors


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_whatsapp_session_notification(self, delivery_record_id, params):
    """Send a FREE session message (active service window).

    Uses /wa/api/v1/msg (not /template/msg). Interactive quick-reply format.
    No Meta charge — only Gupshup's markup.
    """
    record = DeliveryRecord.objects.get(id=delivery_record_id)
    message = _build_session_message(record.event_type, params)

    try:
        response = http_requests.post(
            "https://api.gupshup.io/wa/api/v1/msg",
            headers={"apikey": settings.GUPSHUP_WA_API_KEY},
            data={
                "channel": "whatsapp",
                "source": settings.GUPSHUP_WA_SOURCE_PHONE,
                "destination": record.target,
                "src.name": settings.GUPSHUP_WA_APP_NAME,
                "message": json.dumps(message),
            },
            timeout=10,
        )
        # Branch by HTTP status first
        status_code = response.status_code
        if status_code >= 500 or status_code == 429:
            raise RuntimeError(f"Gupshup server error {status_code}")
        if status_code >= 400:
            # Auth/config errors (401/403) — fail permanently, don't misinterpret as window expiry
            raise ValueError(f"Gupshup client error {status_code}: {response.text[:200]}")

        # 2xx — inspect body for provider business errors
        data = response.json()
        if data.get("status") == "error":
            # On a 2xx + body error, the most common cause is window expiry.
            # Fall back to paid template.
            logger.warning(
                "Session message body error: %s — falling back to template",
                data,
            )
            record.message_type = "TEMPLATE"
            record.save(update_fields=["message_type"])
            send_whatsapp_template_notification.delay(record.id, params)
            return

        if not data.get("messageId"):
            raise ValueError(f"Gupshup response missing messageId: {data}")

        record.provider_message_id = data["messageId"]
        record.status = DeliveryRecord.Status.SENT
        record.save(update_fields=["provider_message_id", "status"])
    except Exception as exc:
        record.status = DeliveryRecord.Status.FAILED
        record.error_message = str(exc)[:500]
        record.save(update_fields=["status", "error_message"])
        if isinstance(exc, _TRANSIENT_ERRORS):
            raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# WhatsApp — Guest invite
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_guest_invite_whatsapp(self, delivery_id):
    """Send the WhatsApp invite template via Gupshup.

    Takes delivery_id (not invite_id) so each Celery task is pinned to
    a specific DeliveryRecord. This prevents a race on quick resends
    where two tasks share the same invite but should operate on different
    delivery rows.
    """
    try:
        delivery = (
            DeliveryRecord.objects
            .select_related('guest_invite', 'guest_invite__hotel')
            .get(id=delivery_id, guest_invite__isnull=False)
        )
    except DeliveryRecord.DoesNotExist:
        logger.error('DeliveryRecord %s not found or has no invite', delivery_id)
        return
    invite = delivery.guest_invite

    if invite.status != 'PENDING':
        return  # Already used/expired

    # Resolve template: hotel-specific → global default
    wa_template = get_template(invite.hotel, 'GUEST_INVITE')
    if not wa_template or not wa_template.gupshup_template_id:
        logger.error('No GUEST_INVITE template configured for hotel %s', invite.hotel.slug)
        delivery.status = DeliveryRecord.Status.FAILED
        delivery.error_message = 'WhatsApp invite template not configured'
        delivery.save(update_fields=['status', 'error_message'])
        return

    # Resolve template variables dynamically from the template's variables JSON
    try:
        params = resolve_template_params(wa_template, invite)
    except ValueError as e:
        logger.error('Template variable resolution failed for delivery %s: %s', delivery_id, e)
        delivery.status = DeliveryRecord.Status.FAILED
        delivery.error_message = str(e)[:500]
        delivery.save(update_fields=['status', 'error_message'])
        return

    # Build signed URL → create short link (sent later via session message, not in template)
    token = generate_invite_token(invite.id, invite.token_version)
    verify_url = f'{settings.API_ORIGIN}/api/v1/auth/wa-invite/{token}/'
    short_link = ShortLink.objects.create_for_url(
        target_url=verify_url,
        expires_at=invite.expires_at,
        metadata={'type': 'guest_invite', 'invite_id': invite.id, 'delivery_id': delivery.id},
    )
    # Two quick-reply postbacks, both keyed by delivery_id so after resends,
    # tapping an older message resolves to the correct DeliveryRecord row.
    postback_texts = [
        {"index": 0, "text": f"g_inv_access:{delivery.id}"},
        {"index": 1, "text": f"g_inv_ack:{delivery.id}"},
    ]

    try:
        response = http_requests.post(
            "https://api.gupshup.io/wa/api/v1/template/msg",
            headers={"apikey": settings.GUPSHUP_WA_API_KEY},
            data={
                "channel": "whatsapp",
                "source": settings.GUPSHUP_WA_SOURCE_PHONE,
                "destination": invite.guest_phone,
                "src.name": settings.GUPSHUP_WA_APP_NAME,
                "template": json.dumps({
                    "id": wa_template.gupshup_template_id,
                    "params": params,
                }),
                "postbackTexts": json.dumps(postback_texts),
            },
            timeout=10,
        )
        # Branch by HTTP status first
        status_code = response.status_code
        if status_code >= 500 or status_code == 429:
            raise RuntimeError(f"Gupshup server error {status_code}")
        if status_code >= 400:
            raise ValueError(f"Gupshup client error {status_code}: {response.text[:200]}")

        # 2xx — inspect body for provider business errors
        data = response.json()
        if data.get("status") == "error":
            raise ValueError(f"Gupshup API error: {data.get('message', 'Unknown')}")
        if not data.get("messageId"):
            raise ValueError(f"Gupshup response missing messageId: {data}")

        # Update delivery record only — GuestInvite.status stays PENDING
        # (it transitions to USED on link click, not on send)
        delivery.provider_message_id = data["messageId"]
        delivery.status = DeliveryRecord.Status.SENT
        delivery.save(update_fields=["provider_message_id", "status"])
    except Exception as exc:
        delivery.status = DeliveryRecord.Status.FAILED
        delivery.error_message = str(exc)[:500]
        delivery.save(update_fields=["status", "error_message"])
        if isinstance(exc, _TRANSIENT_ERRORS):
            raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Email — via Resend API
# ---------------------------------------------------------------------------

def _build_email_html(params):
    """Build HTML email body for staff notification."""
    from html import escape as esc

    hotel_name = esc(params["hotel_name"])
    color = esc(params["primary_color"])
    guest = esc(params["guest_name"])
    room = esc(params["room_number"])
    dept = esc(params["department"])
    subject = esc(params["subject"])
    public_id = esc(params["public_id"])
    event_type = params["event_type"]
    tier = params.get("escalation_tier")

    if event_type == "escalation":
        title = f"Escalation Alert &mdash; {dept}"
        body = (
            f"<strong>{guest}</strong> (Room {room}) needs attention.<br/>"
            f"Department: {dept}<br/>"
            f"Subject: {subject}<br/>"
            f"Escalation: Tier {tier or 1}"
        )
    elif event_type == "response_due":
        title = f"Reminder &mdash; {dept}"
        body = (
            f"Request from <strong>{guest}</strong> (Room {room}) "
            f"is awaiting response.<br/>"
            f"Department: {dept}<br/>"
            f"Subject: {subject}"
        )
    elif event_type == "after_hours_fallback":
        title = f"After-hours request &mdash; {dept}"
        body = (
            f"<strong>{guest}</strong> (Room {room})<br/>"
            f"Department: {dept}<br/>"
            f"Subject: {subject}"
        )
    else:
        title = f"New Request &mdash; {dept}"
        body = (
            f"<strong>{guest}</strong> (Room {room})<br/>"
            f"Department: {dept}<br/>"
            f"Subject: {subject}"
        )

    dashboard_url = f"{settings.FRONTEND_ORIGIN}/dashboard/requests/{public_id}"

    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 40px 24px;">
      <h2 style="color: {color}; font-size: 20px; margin-bottom: 8px;">{title}</h2>
      <p style="color: #555; font-size: 15px; line-height: 1.6; margin-bottom: 20px;">
        {body}
      </p>
      <a href="{dashboard_url}"
         style="display: inline-block; background: {color}; color: #ffffff; text-decoration: none;
                padding: 12px 28px; border-radius: 6px; font-size: 15px; font-weight: 500;">
        View Request
      </a>
      <hr style="border: none; border-top: 1px solid #eee; margin: 28px 0 16px;" />
      <p style="color: #aaa; font-size: 12px;">Sent by {hotel_name} via Refuje</p>
    </div>
    """


def _email_subject(params):
    """Build email subject line."""
    event_type = params["event_type"]
    dept = params["department"]
    hotel = params["hotel_name"]
    if event_type == "escalation":
        return f"[{hotel}] Escalation — {dept}"
    if event_type == "response_due":
        return f"[{hotel}] Reminder — {dept}"
    if event_type == "after_hours_fallback":
        return f"[{hotel}] After-hours — {dept}"
    return f"[{hotel}] New Request — {dept}"


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_email_notification(self, delivery_record_id, params):
    """Send a staff notification email via Resend API."""
    import resend
    from resend.exceptions import (
        ValidationError, MissingRequiredFieldsError,
        MissingApiKeyError, InvalidApiKeyError,
    )

    record = DeliveryRecord.objects.get(id=delivery_record_id)

    resend.api_key = settings.RESEND_API_KEY
    html = _build_email_html(params)
    subject_line = _email_subject(params)

    # Known-permanent Resend errors — don't retry.
    _PERMANENT = (ValidationError, MissingRequiredFieldsError, MissingApiKeyError, InvalidApiKeyError)

    try:
        response = resend.Emails.send({
            'from': settings.RESEND_FROM_EMAIL,
            'to': [record.target],
            'subject': subject_line,
            'html': html,
            'tags': [
                {'name': 'type', 'value': 'staff_notification'},
                {'name': 'event', 'value': record.event_type},
            ],
        })
        record.provider_message_id = response.get('id', '')
        record.status = DeliveryRecord.Status.SENT
        record.save(update_fields=["provider_message_id", "status"])
    except _PERMANENT as exc:
        record.status = DeliveryRecord.Status.FAILED
        record.error_message = str(exc)[:500]
        record.save(update_fields=["status", "error_message"])
    except Exception as exc:
        record.status = DeliveryRecord.Status.FAILED
        record.error_message = str(exc)[:500]
        record.save(update_fields=["status", "error_message"])
        # Transient (RateLimitError, ApplicationError, network) → retry
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# WhatsApp — Guest Ratings
# ---------------------------------------------------------------------------

def _normalize_phone(phone):
    """Strip to digits-only E.164 (no '+' prefix)."""
    import re
    return re.sub(r'\D', '', phone or '')


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_rating_whatsapp_task(self, hotel_id, guest_id, prompt_ids, batch_key):
    """Send batched rating prompt via WhatsApp template (GUEST_RATING_BATCH).

    Always uses template (never session) — rating messages are batched/delayed
    so the 24h service window will usually have expired.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import F
    from django.utils import timezone

    from concierge.models import DeliveryRecord, Hotel, RatingPrompt
    from concierge.services import get_template, resolve_template_params

    User = get_user_model()
    try:
        hotel = Hotel.objects.get(id=hotel_id)
    except Hotel.DoesNotExist:
        logger.warning('Hotel %s deleted — skipping rating batch WA', hotel_id)
        return
    try:
        guest = User.objects.get(id=guest_id)
    except User.DoesNotExist:
        logger.warning('Guest %s deleted — skipping rating batch WA', guest_id)
        return

    # Resolve template
    wa_template = get_template(hotel, 'GUEST_RATING_BATCH')
    if not wa_template:
        logger.error('No GUEST_RATING_BATCH template for hotel %s', hotel.slug)
        RatingPrompt.objects.filter(
            id__in=prompt_ids, status='QUEUED',
        ).update(status='FAILED', failure_count=F('failure_count') + 1)
        return

    guest_name = guest.get_full_name() or guest.phone or 'Guest'
    phone = _normalize_phone(guest.phone)
    if not phone:
        logger.warning('No phone for guest %s — skipping rating WA', guest_id)
        RatingPrompt.objects.filter(
            id__in=prompt_ids, status='QUEUED',
        ).update(status='FAILED', failure_count=F('failure_count') + 1)
        return

    # Idempotency
    idem_key = f'rating_batch:wa:{hotel_id}:{guest_id}:{batch_key}'
    record, created = DeliveryRecord.objects.get_or_create(
        idempotency_key=idem_key,
        defaults={
            'hotel': hotel,
            'channel': 'WHATSAPP',
            'target': phone,
            'event_type': 'rating.batch',
            'message_type': 'TEMPLATE',
            'status': DeliveryRecord.Status.QUEUED,
        },
    )
    if not created and record.status == DeliveryRecord.Status.SENT:
        return  # Already sent

    # Build params — rate_url_suffix is the dynamic CTA button suffix
    # appended to the base URL (https://refuje.com/) configured in Gupshup.
    context = {
        'guest_name': guest_name,
        'hotel_name': hotel.name,
        'experience_count': len(prompt_ids),
        'rate_url_suffix': f'h/{hotel.slug}/rate',
    }
    template_params = resolve_template_params(wa_template, context)

    now = timezone.now()
    try:
        response = http_requests.post(
            "https://api.gupshup.io/wa/api/v1/template/msg",
            headers={"apikey": settings.GUPSHUP_WA_API_KEY},
            data={
                "channel": "whatsapp",
                "source": settings.GUPSHUP_WA_SOURCE_PHONE,
                "destination": phone,
                "src.name": settings.GUPSHUP_WA_APP_NAME,
                "template": json.dumps({
                    "id": wa_template.gupshup_template_id,
                    "params": template_params,
                }),
            },
            timeout=10,
        )
        status_code = response.status_code
        if status_code >= 500 or status_code == 429:
            raise RuntimeError(f"Gupshup server error {status_code}")
        if status_code >= 400:
            raise ValueError(f"Gupshup client error {status_code}: {response.text[:200]}")

        data = response.json()
        if data.get("status") == "error":
            raise ValueError(f"Gupshup API error: {data.get('message', 'Unknown')}")
        if not data.get("messageId"):
            raise ValueError(f"Gupshup response missing messageId: {data}")

        record.provider_message_id = data["messageId"]
        record.status = DeliveryRecord.Status.SENT
        record.save(update_fields=["provider_message_id", "status"])

        RatingPrompt.objects.filter(
            id__in=prompt_ids, status='QUEUED',
        ).update(status='SENT', sent_at=now)
    except Exception as exc:
        record.status = DeliveryRecord.Status.FAILED
        record.error_message = str(exc)[:500]
        record.save(update_fields=["status", "error_message"])
        if isinstance(exc, _TRANSIENT_ERRORS):
            raise self.retry(exc=exc)
        # Permanent failure
        RatingPrompt.objects.filter(
            id__in=prompt_ids, status='QUEUED',
        ).update(status='FAILED', failure_count=F('failure_count') + 1)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_stay_survey_whatsapp_task(self, hotel_id, guest_id, prompt_id):
    """Send stay survey via WhatsApp template (GUEST_STAY_SURVEY)."""
    from django.contrib.auth import get_user_model
    from django.db.models import F
    from django.utils import timezone

    from concierge.models import DeliveryRecord, Hotel, RatingPrompt
    from concierge.services import get_template, resolve_template_params

    User = get_user_model()
    try:
        hotel = Hotel.objects.get(id=hotel_id)
    except Hotel.DoesNotExist:
        logger.warning('Hotel %s deleted — skipping stay survey WA', hotel_id)
        return
    try:
        guest = User.objects.get(id=guest_id)
    except User.DoesNotExist:
        logger.warning('Guest %s deleted — skipping stay survey WA', guest_id)
        return

    prompt = RatingPrompt.objects.filter(id=prompt_id, status='QUEUED').first()
    if not prompt:
        return  # Already sent or expired

    wa_template = get_template(hotel, 'GUEST_STAY_SURVEY')
    if not wa_template:
        logger.error('No GUEST_STAY_SURVEY template for hotel %s', hotel.slug)
        RatingPrompt.objects.filter(id=prompt_id, status='QUEUED').update(
            status='FAILED', failure_count=F('failure_count') + 1,
        )
        return

    guest_name = guest.get_full_name() or guest.phone or 'Guest'
    phone = _normalize_phone(guest.phone)
    if not phone:
        logger.warning('No phone for guest %s — skipping stay survey WA', guest_id)
        RatingPrompt.objects.filter(id=prompt_id, status='QUEUED').update(
            status='FAILED', failure_count=F('failure_count') + 1,
        )
        return

    idem_key = f'stay_survey:wa:{hotel_id}:{prompt.guest_stay_id}'
    record, created = DeliveryRecord.objects.get_or_create(
        idempotency_key=idem_key,
        defaults={
            'hotel': hotel,
            'channel': 'WHATSAPP',
            'target': phone,
            'event_type': 'rating.stay_survey',
            'message_type': 'TEMPLATE',
            'status': DeliveryRecord.Status.QUEUED,
        },
    )
    if not created and record.status == DeliveryRecord.Status.SENT:
        return

    context = {
        'guest_name': guest_name,
        'hotel_name': hotel.name,
        'rate_url_suffix': f'h/{hotel.slug}/rate?type=stay',
    }
    template_params = resolve_template_params(wa_template, context)

    now = timezone.now()
    try:
        response = http_requests.post(
            "https://api.gupshup.io/wa/api/v1/template/msg",
            headers={"apikey": settings.GUPSHUP_WA_API_KEY},
            data={
                "channel": "whatsapp",
                "source": settings.GUPSHUP_WA_SOURCE_PHONE,
                "destination": phone,
                "src.name": settings.GUPSHUP_WA_APP_NAME,
                "template": json.dumps({
                    "id": wa_template.gupshup_template_id,
                    "params": template_params,
                }),
            },
            timeout=10,
        )
        status_code = response.status_code
        if status_code >= 500 or status_code == 429:
            raise RuntimeError(f"Gupshup server error {status_code}")
        if status_code >= 400:
            raise ValueError(f"Gupshup client error {status_code}: {response.text[:200]}")

        data = response.json()
        if data.get("status") == "error":
            raise ValueError(f"Gupshup API error: {data.get('message', 'Unknown')}")
        if not data.get("messageId"):
            raise ValueError(f"Gupshup response missing messageId: {data}")

        record.provider_message_id = data["messageId"]
        record.status = DeliveryRecord.Status.SENT
        record.save(update_fields=["provider_message_id", "status"])

        RatingPrompt.objects.filter(id=prompt_id, status='QUEUED').update(
            status='SENT', sent_at=now,
        )
    except Exception as exc:
        record.status = DeliveryRecord.Status.FAILED
        record.error_message = str(exc)[:500]
        record.save(update_fields=["status", "error_message"])
        if isinstance(exc, _TRANSIENT_ERRORS):
            raise self.retry(exc=exc)
        RatingPrompt.objects.filter(id=prompt_id, status='QUEUED').update(
            status='FAILED', failure_count=F('failure_count') + 1,
        )


# ---------------------------------------------------------------------------
# Low-Score Alert — WhatsApp + Email
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_low_score_whatsapp_task(self, hotel_id, rating_id):
    """Send low-score alert to hotel's on-call phone via WhatsApp.

    Checks WhatsAppServiceWindow — active window → session msg (free),
    no window → LOW_SCORE_ALERT template msg (paid).
    """
    from django.utils import timezone

    from concierge.models import (
        DeliveryRecord, Hotel, Rating, WhatsAppServiceWindow,
    )
    from concierge.services import get_template, resolve_template_params

    try:
        hotel = Hotel.objects.get(id=hotel_id)
    except Hotel.DoesNotExist:
        logger.warning('Hotel %s deleted — skipping low-score WA', hotel_id)
        return
    if not hotel.whatsapp_notifications_enabled:
        return
    phone = _normalize_phone(hotel.oncall_phone)
    if not phone:
        return

    try:
        rating = Rating.objects.select_related(
            'guest', 'guest_stay',
            'service_request__guest_stay',
            'service_request__department',
            'service_request__experience',
            'service_request__special_request_offering',
        ).get(id=rating_id)
    except Rating.DoesNotExist:
        logger.warning('Rating %s deleted — skipping low-score WA', rating_id)
        return

    guest_name = rating.guest.get_full_name() or rating.guest.phone or 'Guest'
    room = ''
    if rating.service_request and rating.service_request.guest_stay:
        room = rating.service_request.guest_stay.room_number or ''
    elif rating.guest_stay:
        room = rating.guest_stay.room_number or ''
    subject = 'overall stay'
    sr = rating.service_request
    if sr:
        subject = (
            (sr.special_request_offering.name if sr.special_request_offering_id else None)
            or (sr.experience.name if sr.experience_id else None)
            or (sr.department.name if sr.department_id else None)
            or 'their request'
        )

    idem_key = f'low_score:wa:{hotel_id}:{rating_id}'
    record, created = DeliveryRecord.objects.get_or_create(
        idempotency_key=idem_key,
        defaults={
            'hotel': hotel,
            'channel': 'WHATSAPP',
            'target': phone,
            'event_type': 'rating.low_score',
            'message_type': 'TEMPLATE',
            'status': DeliveryRecord.Status.QUEUED,
        },
    )
    if not created and record.status == DeliveryRecord.Status.SENT:
        return

    # Check for active service window
    window = WhatsAppServiceWindow.objects.filter(
        hotel=hotel, phone=phone,
    ).first()
    use_session = window and window.is_active

    if use_session:
        # Free session message
        record.message_type = 'SESSION'
        record.save(update_fields=['message_type'])

        text = (
            f"⚠️ *Low Rating Alert*\n\n"
            f"Guest {guest_name}"
        )
        if room:
            text += f" (Room {room})"
        text += f" rated {subject} {rating.score}/5."
        if rating.feedback:
            text += f'\n\nFeedback: "{rating.feedback[:200]}"'

        message = {
            "type": "text",
            "text": text,
        }
        try:
            response = http_requests.post(
                "https://api.gupshup.io/wa/api/v1/msg",
                headers={"apikey": settings.GUPSHUP_WA_API_KEY},
                data={
                    "channel": "whatsapp",
                    "source": settings.GUPSHUP_WA_SOURCE_PHONE,
                    "destination": phone,
                    "src.name": settings.GUPSHUP_WA_APP_NAME,
                    "message": json.dumps(message),
                },
                timeout=10,
            )
            status_code = response.status_code
            if status_code >= 500 or status_code == 429:
                raise RuntimeError(f"Gupshup server error {status_code}")
            if status_code >= 400:
                raise ValueError(f"Gupshup client error {status_code}: {response.text[:200]}")

            data = response.json()
            if data.get("status") == "error":
                # Window likely expired — fall through to template
                logger.warning("Session msg body error: %s — falling back to template", data)
                use_session = False
            else:
                if not data.get("messageId"):
                    raise ValueError(f"Missing messageId: {data}")
                record.provider_message_id = data["messageId"]
                record.status = DeliveryRecord.Status.SENT
                record.save(update_fields=["provider_message_id", "status"])
                return
        except Exception as exc:
            if isinstance(exc, _TRANSIENT_ERRORS):
                record.status = DeliveryRecord.Status.FAILED
                record.error_message = str(exc)[:500]
                record.save(update_fields=["status", "error_message"])
                raise self.retry(exc=exc)
            # Non-transient session error — fall through to template
            logger.warning("Session send failed, falling back to template: %s", exc)

    # Template message (paid)
    record.message_type = 'TEMPLATE'
    record.save(update_fields=['message_type'])

    wa_template = get_template(hotel, 'LOW_SCORE_ALERT')
    if not wa_template:
        logger.error('No LOW_SCORE_ALERT template for hotel %s', hotel.slug)
        record.status = DeliveryRecord.Status.FAILED
        record.error_message = 'Template not configured'
        record.save(update_fields=['status', 'error_message'])
        return

    guest_summary = guest_name
    if room:
        guest_summary = f'{guest_name} (Room {room})'
    rating_detail = f'{subject} {rating.score}/5'
    context = {
        'guest_summary': guest_summary,
        'rating_detail': rating_detail,
        'feedback_preview': (rating.feedback or 'No feedback')[:100],
        'dashboard_path': 'dashboard/ratings',
    }
    template_params = resolve_template_params(wa_template, context)

    try:
        response = http_requests.post(
            "https://api.gupshup.io/wa/api/v1/template/msg",
            headers={"apikey": settings.GUPSHUP_WA_API_KEY},
            data={
                "channel": "whatsapp",
                "source": settings.GUPSHUP_WA_SOURCE_PHONE,
                "destination": phone,
                "src.name": settings.GUPSHUP_WA_APP_NAME,
                "template": json.dumps({
                    "id": wa_template.gupshup_template_id,
                    "params": template_params,
                }),
            },
            timeout=10,
        )
        status_code = response.status_code
        if status_code >= 500 or status_code == 429:
            raise RuntimeError(f"Gupshup server error {status_code}")
        if status_code >= 400:
            raise ValueError(f"Gupshup client error {status_code}: {response.text[:200]}")

        data = response.json()
        if data.get("status") == "error":
            raise ValueError(f"Gupshup API error: {data.get('message', 'Unknown')}")
        if not data.get("messageId"):
            raise ValueError(f"Gupshup response missing messageId: {data}")

        record.provider_message_id = data["messageId"]
        record.status = DeliveryRecord.Status.SENT
        record.save(update_fields=["provider_message_id", "status"])
    except Exception as exc:
        record.status = DeliveryRecord.Status.FAILED
        record.error_message = str(exc)[:500]
        record.save(update_fields=["status", "error_message"])
        if isinstance(exc, _TRANSIENT_ERRORS):
            raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_low_score_email_task(self, hotel_id, rating_id):
    """Send low-score alert email to hotel's on-call email via Resend."""
    import resend
    from resend.exceptions import (
        ValidationError, MissingRequiredFieldsError,
        MissingApiKeyError, InvalidApiKeyError,
    )

    from concierge.models import Hotel, Rating

    try:
        hotel = Hotel.objects.get(id=hotel_id)
    except Hotel.DoesNotExist:
        logger.warning('Hotel %s deleted — skipping low-score email', hotel_id)
        return
    if not hotel.email_notifications_enabled:
        return
    if not hotel.oncall_email:
        return

    try:
        rating = Rating.objects.select_related(
            'guest', 'guest_stay',
            'service_request__guest_stay',
            'service_request__department',
            'service_request__experience',
            'service_request__special_request_offering',
        ).get(id=rating_id)
    except Rating.DoesNotExist:
        logger.warning('Rating %s deleted — skipping low-score email', rating_id)
        return

    guest_name = rating.guest.get_full_name() or rating.guest.phone or 'Guest'
    room = ''
    if rating.service_request and rating.service_request.guest_stay:
        room = rating.service_request.guest_stay.room_number or ''
    elif rating.guest_stay:
        room = rating.guest_stay.room_number or ''
    subject_detail = 'overall stay'
    sr = rating.service_request
    if sr:
        subject_detail = (
            (sr.special_request_offering.name if sr.special_request_offering_id else None)
            or (sr.experience.name if sr.experience_id else None)
            or (sr.department.name if sr.department_id else None)
            or 'their request'
        )

    from html import escape as esc
    color = esc(hotel.primary_color or '#1a1a1a')
    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 40px 24px;">
      <h2 style="color: {color}; font-size: 20px; margin-bottom: 8px;">⚠️ Low Rating Alert</h2>
      <p style="color: #555; font-size: 15px; line-height: 1.6; margin-bottom: 20px;">
        <strong>{esc(guest_name)}</strong>{f' (Room {esc(room)})' if room else ''} rated {esc(subject_detail)} <strong>{rating.score}/5</strong>.
        {f'<br/><br/>Feedback: &ldquo;{esc(rating.feedback[:300])}&rdquo;' if rating.feedback else ''}
      </p>
      <a href="{settings.FRONTEND_ORIGIN}/dashboard/ratings"
         style="display: inline-block; background: {color}; color: #ffffff; text-decoration: none;
                padding: 12px 28px; border-radius: 6px; font-size: 15px; font-weight: 500;">
        View Ratings
      </a>
      <hr style="border: none; border-top: 1px solid #eee; margin: 28px 0 16px;" />
      <p style="color: #aaa; font-size: 12px;">Sent by {esc(hotel.name)} via Refuje</p>
    </div>
    """

    resend.api_key = settings.RESEND_API_KEY
    _PERMANENT = (ValidationError, MissingRequiredFieldsError, MissingApiKeyError, InvalidApiKeyError)

    try:
        resend.Emails.send({
            'from': settings.RESEND_FROM_EMAIL,
            'to': [hotel.oncall_email],
            'subject': f'[{hotel.name}] Low Rating Alert — {guest_name} ({rating.score}/5)',
            'html': html,
            'tags': [
                {'name': 'type', 'value': 'low_score_alert'},
                {'name': 'hotel', 'value': hotel.slug},
            ],
        })
    except _PERMANENT as exc:
        logger.error('Low-score email permanent failure: %s', exc)
    except Exception as exc:
        logger.warning('Low-score email transient failure: %s', exc)
        raise self.retry(exc=exc)
