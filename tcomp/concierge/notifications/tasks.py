import json
import logging

import requests as http_requests
from celery import shared_task
from django.conf import settings

from concierge.models import DeliveryRecord
from concierge.services import send_push_notification

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
