import logging

from django.conf import settings
from django.db import connection
from django.db.models import Q

from concierge.models import (
    DeliveryRecord,
    NotificationRoute,
    WhatsAppServiceWindow,
)

from .base import ChannelAdapter

logger = logging.getLogger(__name__)


class WhatsAppAdapter(ChannelAdapter):
    """WhatsApp notifications via Gupshup. Uses two-path dispatch for cost optimization.

    Path 1 — No active service window: send a PAID utility template with
             2 quick-reply buttons ("Acknowledge"/"On it" + "View Details").
             Either tap opens the 24h service window.
    Path 2 — Active service window: send a FREE session message (rich text
             with interactive buttons). No Meta charge, only Gupshup's
             ₹0.08 markup.
    """

    def is_enabled(self, hotel):
        return (
            hotel.whatsapp_notifications_enabled
            and settings.GUPSHUP_WA_API_KEY
        )

    def get_recipients(self, event):
        if not event.is_request_event:
            return []  # WhatsApp only fires for request events, not daily_digest

        # Two-level routing: dept-wide + experience-specific, deduplicated by target
        experience = event.request.experience if event.request else None
        routes = NotificationRoute.objects.filter(
            department=event.department,
            channel=NotificationRoute.Channel.WHATSAPP,
            is_active=True,
        ).filter(
            Q(experience__isnull=True)
            | Q(experience=experience)
        ).order_by("target", "id")

        # DB-level dedupe: deterministic ordering (target ASC, id ASC) then
        # Distinct on target — keeps the lowest-ID route per phone number.
        # On PostgreSQL this uses .distinct("target"); on SQLite (tests) we
        # fall back to Python-side dedupe with the same deterministic order.
        if connection.vendor == "postgresql":
            return list(routes.distinct("target"))
        seen = set()
        unique = []
        for route in routes:
            if route.target not in seen:
                seen.add(route.target)
                unique.append(route)
        return unique

    def send(self, route, event):
        from .tasks import (
            send_whatsapp_session_notification,
            send_whatsapp_template_notification,
        )

        # Idempotency: one delivery per (request, escalation_tier, route)
        idempotency_key = (
            f"{event.event_type}:{event.request.public_id}"
            f":{event.escalation_tier or 0}:{route.id}"
        )

        # Check if this phone has an active service window
        window = WhatsAppServiceWindow.objects.filter(
            hotel=event.hotel, phone=route.target,
        ).first()
        use_session = window and window.is_active

        record, created = DeliveryRecord.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "hotel": event.hotel,
                "route": route,
                "request": event.request,
                "channel": "WHATSAPP",
                "target": route.target,
                "event_type": event.event_type,
                "status": DeliveryRecord.Status.QUEUED,
                "message_type": "SESSION" if use_session else "TEMPLATE",
            },
        )
        if not created:
            return record  # Already sent/queued — skip duplicate

        params = self._build_params(event)

        if use_session:
            send_whatsapp_session_notification.delay(record.id, params)
        else:
            send_whatsapp_template_notification.delay(record.id, params)

        return record

    def _build_params(self, event):
        """Build notification params (used by both template and session tasks)."""
        req = event.request
        # For after-hours, show the original department name (not the fallback)
        dept_name = event.extra.get("original_department_name") or event.department.name
        params = {
            "guest_name": req.guest_stay.guest.get_full_name(),
            "room_number": req.guest_stay.room_number,
            "department": dept_name,
            "subject": event.display_name,
            "request_type": req.get_request_type_display(),
            "public_id": str(req.public_id),
        }
        if event.escalation_tier is not None:
            params["escalation_tier"] = event.escalation_tier
        return params
