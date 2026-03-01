"""Gupshup WhatsApp webhook handlers for notification ack + delivery status.

These handlers coexist with the existing OTP delivery tracking in services.py.
The webhook view dispatches to both: OTP handler first, then notification handlers.
"""
import json
import logging
import re

import requests as http_requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from concierge.models import (
    DeliveryRecord,
    HotelMembership,
    RequestActivity,
    ServiceRequest,
    WhatsAppServiceWindow,
)
from concierge.services import publish_invite_event, publish_request_event
from shortlinks.models import ShortLink

logger = logging.getLogger(__name__)


def _resolve_postback(msg_payload):
    """Extract postback text from all known Gupshup inbound message formats.

    Handles quick_reply, button_reply, and button types since Gupshup
    delivers template button taps inconsistently across API versions.
    The ``reply`` field may be a string or a dict like ``{"id": "ack:..."}``.
    """
    msg_type = msg_payload.get("type", "")
    if msg_type in ("quick_reply", "button_reply", "button"):
        # postbackText may be at this level or nested inside an inner "payload" dict
        postback = msg_payload.get("postbackText", "")
        if not postback:
            inner = msg_payload.get("payload", {})
            if isinstance(inner, dict):
                postback = inner.get("postbackText", "")
        if postback and isinstance(postback, str):
            return postback
        reply = msg_payload.get("reply", "")
        if isinstance(reply, dict):
            # Gupshup sometimes wraps the postback in {"id": "...", "title": "..."}
            return str(reply.get("id", "") or reply.get("title", ""))
        if isinstance(reply, str):
            return reply
    return ""


def _parse_public_id(postback):
    """Extract public_id from a postback string (ack:, esc_ack:, view:)."""
    if postback.startswith("ack:"):
        return postback.split(":", 1)[1]
    if postback.startswith("esc_ack:"):
        parts = postback.split(":")
        return parts[1] if len(parts) >= 2 else None
    if postback.startswith("view:"):
        return postback.split(":", 1)[1]
    return None


# Patterns that map free-text replies to postback actions.
# Checked case-insensitively against stripped message text.
_TEXT_TO_ACTION = {
    "acknowledge": "ack",
    "ack": "ack",
    "on it": "ack",
    "view details": "view",
    "view": "view",
}


def _resolve_request_from_delivery(phone):
    """Find the most recent unacknowledged WhatsApp notification for this phone.

    Used as fallback when no postback or service window exists — lets us
    handle template button taps that arrive as plain text.

    Scoped to hotels where this phone is an active member to avoid
    cross-hotel misattribution when one phone receives notifications
    from multiple hotels.
    """
    # Scope to hotels where this phone belongs to an active member
    member_hotel_ids = HotelMembership.objects.filter(
        user__phone=phone,
        is_active=True,
    ).values_list("hotel_id", flat=True)

    record = (
        DeliveryRecord.objects.filter(
            channel="WHATSAPP",
            target=phone,
            request__isnull=False,
            acknowledged_at__isnull=True,
            status__in=[DeliveryRecord.Status.SENT, DeliveryRecord.Status.DELIVERED],
            hotel_id__in=member_hotel_ids,
        )
        .select_related("request", "request__hotel")
        .order_by("-created_at")
        .first()
    )
    return record.request if record else None


def handle_inbound_message(payload):
    """Handle inbound messages from staff (quick-reply taps or free-text).

    Hotel resolution strategy (in order):
    1. Postback flows (ack/esc_ack/view): hotel derived from ServiceRequest.hotel
    2. Free-text with delivery fallback: hotel derived from most recent
       unacknowledged DeliveryRecord for this phone
    3. Free-text with service window: hotel derived from WhatsAppServiceWindow

    Persists acknowledgements at two levels:
    1. DeliveryRecord.acknowledged_at — per-notification ack
    2. ServiceRequest.acknowledged_at + status=ACKNOWLEDGED — request-level ack
    """
    source = payload.get("payload", {}).get("source", "") or payload.get("source", "")
    phone = re.sub(r"\D", "", source)
    if not phone:
        return

    msg_payload = payload.get("payload", {})
    postback = _resolve_postback(msg_payload)
    now = timezone.now()

    # Guest invite postbacks — handle separately and return early
    if postback.startswith('g_inv_access:') or postback.startswith('g_inv_ack:'):
        _handle_invite_postback(postback, payload, now)
        return

    # Parse postback — extract public_id from all postback types
    public_id = _parse_public_id(postback) if postback else None

    # --- Hotel resolution ---
    hotel = None
    req = None
    text_action = None

    if public_id:
        # Standard postback — resolve from request
        try:
            req = ServiceRequest.objects.select_related("hotel").get(public_id=public_id)
            hotel = req.hotel
        except ServiceRequest.DoesNotExist:
            logger.warning("WhatsApp postback for unknown request %s from %s", public_id, phone)
            return
    else:
        # No postback — check if free-text matches a button label,
        # then resolve hotel from recent delivery record or service window.
        inner = msg_payload.get("payload", {}) if isinstance(msg_payload.get("payload"), dict) else {}
        text = (msg_payload.get("text", "") or inner.get("text", "") or msg_payload.get("title", "")).strip()
        text_action = _TEXT_TO_ACTION.get(text.lower())

        # Try delivery record fallback first (covers template button taps
        # delivered as text and typed replies to recent notifications).
        req = _resolve_request_from_delivery(phone)
        if req:
            hotel = req.hotel
            public_id = str(req.public_id)
        else:
            # Fall back to service window
            window = WhatsAppServiceWindow.objects.filter(
                phone=phone,
            ).order_by("-last_inbound_at").first()
            if window:
                hotel = window.hotel
            else:
                logger.info("Free-text from %s with no service window or delivery record — skipping", phone)
                return

    # Update or create service window (opens/resets the 24h window)
    WhatsAppServiceWindow.objects.update_or_create(
        hotel=hotel,
        phone=phone,
        defaults={"last_inbound_at": now},
    )

    # Determine effective action: explicit postback or text-matched action
    action = None
    if postback.startswith("view:"):
        action = "view"
    elif postback.startswith(("ack:", "esc_ack:")):
        action = "ack"
    elif text_action and req:
        action = text_action

    if not action or not req:
        return  # Pure free-text with no actionable match — window updated, done

    # "View Details": send dashboard URL as follow-up session message
    if action == "view":
        try:
            url = f"{settings.FRONTEND_ORIGIN}/dashboard/requests/{public_id}"
            _send_session_text(phone, f"View request details:\n{url}")
        except Exception:
            logger.warning("Failed to send View Details URL to %s for request %s", phone, public_id)

    # 1. Mark matching DeliveryRecords as acknowledged
    DeliveryRecord.objects.filter(
        channel="WHATSAPP",
        target=phone,
        request=req,
        acknowledged_at__isnull=True,
    ).update(acknowledged_at=now)

    # 2. Acknowledge the ServiceRequest itself — all actions (ack, view)
    #    trigger request-level ack since any engagement indicates staff awareness.
    _acknowledge_request(req, phone, now)


def _acknowledge_request(req, phone, now):
    """Acknowledge a ServiceRequest via WhatsApp ack postback.

    Reuses the same CREATED→ACKNOWLEDGED transition as the dashboard acknowledge view.
    """
    with transaction.atomic():
        req = ServiceRequest.objects.select_for_update().get(pk=req.pk)

        if req.status != ServiceRequest.Status.CREATED:
            return  # Already acknowledged or in terminal state

        # Resolve phone → staff user for activity log (best-effort)
        User = get_user_model()
        user_table = User._meta.db_table
        membership = HotelMembership.objects.filter(
            hotel=req.hotel, is_active=True,
        ).select_related("user").extra(
            where=[f'REGEXP_REPLACE("{user_table}"."phone", \'[^0-9]\', \'\', \'g\') = %s'],
            params=[phone],
        ).first()
        actor = membership.user if membership else None

        req.status = ServiceRequest.Status.ACKNOWLEDGED
        req.acknowledged_at = now
        req.save(update_fields=["status", "acknowledged_at", "updated_at"])

        RequestActivity.objects.create(
            request=req,
            actor=actor,
            action=RequestActivity.Action.ACKNOWLEDGED,
            details={
                "status_from": "CREATED",
                "status_to": "ACKNOWLEDGED",
                "channel": "whatsapp",
                "phone": phone,
            },
        )

    publish_request_event(req.hotel, "request.updated", req)


def handle_message_event(payload):
    """Handle delivery status updates (delivered, read, failed).

    Coexists with existing OTP delivery tracking — both handlers
    run for each webhook event using different models.
    """
    message_id = payload.get("payload", {}).get("gsId", "") or payload.get("payload", {}).get("id", "")
    if not message_id:
        return

    event_type = payload.get("payload", {}).get("type", "")

    status_map = {
        "delivered": DeliveryRecord.Status.DELIVERED,
        "read": DeliveryRecord.Status.DELIVERED,  # read implies delivered
        "failed": DeliveryRecord.Status.FAILED,
    }
    new_status = status_map.get(event_type)
    if not new_status:
        return

    update_fields = {"status": new_status}
    if new_status == DeliveryRecord.Status.DELIVERED:
        update_fields["delivered_at"] = timezone.now()
    elif new_status == DeliveryRecord.Status.FAILED:
        inner = payload.get("payload", {})
        error = inner.get("payload", {}) if isinstance(inner.get("payload"), dict) else inner
        update_fields["error_message"] = f"{error.get('code', '')}: {error.get('reason', '')}"[:500]

    # Fetch record before update to check idempotency and get context for SSE
    record = (
        DeliveryRecord.objects
        .filter(provider_message_id=message_id, channel="WHATSAPP")
        .select_related('guest_invite')
        .first()
    )
    if not record:
        return
    status_changed = record.status != new_status
    if not status_changed and 'error_message' not in update_fields:
        return  # Truly idempotent — same status, no new error info

    DeliveryRecord.objects.filter(pk=record.pk).update(**update_fields)

    # Emit SSE event for guest invite deliveries so dashboard updates in real time
    if status_changed and record.guest_invite_id:
        hotel_id = record.guest_invite.hotel_id
        transaction.on_commit(lambda: publish_invite_event(
            hotel_id, record.id, record.guest_invite_id, new_status,
        ))


def _send_session_text(phone, text):
    """Send a plain text session message (free within 24h window)."""
    http_requests.post(
        "https://api.gupshup.io/wa/api/v1/msg",
        headers={"apikey": settings.GUPSHUP_WA_API_KEY},
        data={
            "channel": "whatsapp",
            "source": settings.GUPSHUP_WA_SOURCE_PHONE,
            "destination": phone,
            "src.name": settings.GUPSHUP_WA_APP_NAME,
            "message": json.dumps({
                "type": "text",
                "text": text,
            }),
        },
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Guest invite postback handlers
# ---------------------------------------------------------------------------

def _normalize_phone(raw):
    """Strip non-digits from phone number."""
    return re.sub(r'\D', '', raw)


def _resolve_invite_delivery(postback, payload):
    """Shared: resolve DeliveryRecord + verify phone. Returns (delivery, invite, phone) or None."""
    parts = postback.split(':', 1)
    if len(parts) < 2 or not parts[1].isdigit():
        logger.warning('Malformed invite postback: %s', postback)
        return None
    delivery_id = int(parts[1])
    delivery = (
        DeliveryRecord.objects
        .select_related('guest_invite', 'guest_invite__hotel')
        .filter(id=delivery_id, channel='WHATSAPP', guest_invite__isnull=False)
        .first()
    )
    if not delivery or not delivery.guest_invite:
        return None
    invite = delivery.guest_invite

    # Extract source phone via fallback chain
    raw_source = payload.get('source') or payload.get('payload', {}).get('source')
    if not raw_source:
        logger.warning('No source phone in invite postback payload')
        return None
    source_phone = _normalize_phone(raw_source)
    if source_phone != invite.guest_phone:
        logger.warning('Phone mismatch on invite postback')
        return None

    # Open session window (both buttons do this)
    WhatsAppServiceWindow.objects.update_or_create(
        hotel=invite.hotel,
        phone=source_phone,
        defaults={'last_inbound_at': timezone.now()},
    )
    # Mark this delivery acknowledged
    delivery.acknowledged_at = timezone.now()
    delivery.save(update_fields=['acknowledged_at'])
    return delivery, invite, source_phone


def _handle_invite_postback(postback, payload, now):
    """Route g_inv_access and g_inv_ack postbacks."""
    if postback.startswith('g_inv_access:'):
        result = _resolve_invite_delivery(postback, payload)
        if not result:
            return
        delivery, invite, source_phone = result

        # Guard: check both status AND expiry
        if invite.status != 'PENDING':
            return  # Already used/expired
        if invite.expires_at < now:
            invite.status = 'EXPIRED'
            invite.save(update_fields=['status'])
            try:
                _send_session_text(
                    source_phone,
                    "This invite has expired. Please ask the hotel front desk to resend it.",
                )
            except Exception:
                logger.warning('Failed to send expiry message to %s', source_phone)
            return

        # Look up the ShortLink created for this delivery
        short_link = ShortLink.objects.filter(
            metadata__delivery_id=delivery.id,
            is_active=True,
        ).first()
        if short_link:
            link_url = f'{settings.FRONTEND_ORIGIN}/s/{short_link.code}'
            try:
                _send_session_text(
                    source_phone,
                    f"Here's your concierge link \u2014 tap to get started:\n{link_url}",
                )
            except Exception:
                logger.warning('Failed to send invite link to %s', source_phone)
        else:
            logger.error('No active ShortLink for delivery %s', delivery.id)
            try:
                _send_session_text(
                    source_phone,
                    "Sorry, we couldn't generate your link. Please contact the hotel front desk for assistance.",
                )
            except Exception:
                logger.warning('Failed to send fallback message to %s', source_phone)

    elif postback.startswith('g_inv_ack:'):
        result = _resolve_invite_delivery(postback, payload)
        if not result:
            return
        delivery, invite, source_phone = result
        try:
            _send_session_text(
                source_phone,
                "You're all set! We'll keep you updated via WhatsApp.",
            )
        except Exception:
            logger.warning('Failed to send ack confirmation to %s', source_phone)
