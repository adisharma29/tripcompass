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
