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
from concierge.services import publish_request_event

logger = logging.getLogger(__name__)


def handle_inbound_message(payload):
    """Handle inbound messages from staff (quick-reply taps or free-text).

    Hotel resolution strategy:
    - Postback flows (ack/esc_ack/view): hotel derived from ServiceRequest.hotel
    - Free-text: hotel derived from existing WhatsAppServiceWindow for this phone

    Persists acknowledgements at two levels:
    1. DeliveryRecord.acknowledged_at — per-notification ack
    2. ServiceRequest.acknowledged_at + status=ACKNOWLEDGED — request-level ack
    """
    source = payload.get("payload", {}).get("source", "") or payload.get("source", "")
    phone = re.sub(r"\D", "", source)
    if not phone:
        return

    msg_payload = payload.get("payload", {})
    postback = ""
    msg_type = msg_payload.get("type", "")
    if msg_type == "quick_reply":
        postback = msg_payload.get("postbackText", "")
    elif msg_type == "button_reply":
        postback = msg_payload.get("postbackText", "")

    now = timezone.now()

    # Parse postback — extract public_id from all postback types
    public_id = None
    if postback.startswith("ack:"):
        public_id = postback.split(":", 1)[1]
    elif postback.startswith("esc_ack:"):
        parts = postback.split(":")
        public_id = parts[1] if len(parts) >= 2 else None
    elif postback.startswith("view:"):
        public_id = postback.split(":", 1)[1]

    # --- Hotel resolution ---
    hotel = None
    req = None
    if public_id:
        try:
            req = ServiceRequest.objects.select_related("hotel").get(public_id=public_id)
            hotel = req.hotel
        except ServiceRequest.DoesNotExist:
            logger.warning("WhatsApp postback for unknown request %s from %s", public_id, phone)
            return
    else:
        window = WhatsAppServiceWindow.objects.filter(
            phone=phone,
        ).order_by("-last_inbound_at").first()
        if window:
            hotel = window.hotel
        else:
            logger.info("Free-text from %s with no service window — skipping", phone)
            return

    # Update or create service window (opens/resets the 24h window)
    WhatsAppServiceWindow.objects.update_or_create(
        hotel=hotel,
        phone=phone,
        defaults={"last_inbound_at": now},
    )

    if not public_id:
        return  # Free-text — window updated, nothing more to do

    # "View Details" postback: send dashboard URL as follow-up session message
    if postback.startswith("view:"):
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

    # 2. Acknowledge the ServiceRequest itself — all postback types (ack, esc_ack, view)
    #    trigger request-level ack since any tap indicates staff engagement.
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
        error = payload.get("payload", {})
        update_fields["error_message"] = f"{error.get('code', '')}: {error.get('reason', '')}"[:500]

    DeliveryRecord.objects.filter(
        provider_message_id=message_id,
        channel="WHATSAPP",
    ).update(**update_fields)


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
