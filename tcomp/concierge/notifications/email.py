import logging

from django.conf import settings
from django.db import connection
from django.db.models import Q

from concierge.models import DeliveryRecord, NotificationRoute

from .base import ChannelAdapter

logger = logging.getLogger(__name__)


class EmailAdapter(ChannelAdapter):
    """Email notifications via Resend API.

    Same route-based targeting and dedupe pattern as WhatsAppAdapter.
    Sends via Celery task for async delivery with retry logic.
    """

    def is_enabled(self, hotel):
        return (
            hotel.email_notifications_enabled
            and getattr(settings, 'RESEND_API_KEY', None)
        )

    def get_recipients(self, event):
        if not event.is_request_event:
            return []  # Email only fires for request events

        experience = event.request.experience if event.request else None
        routes = NotificationRoute.objects.filter(
            department=event.department,
            channel=NotificationRoute.Channel.EMAIL,
            is_active=True,
        ).filter(
            Q(experience__isnull=True)
            | Q(experience=experience)
        ).order_by("target", "id")

        # Dedupe by target (same pattern as WhatsApp)
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
        from .tasks import send_email_notification

        idempotency_key = (
            f"email:{event.event_type}:{event.request.public_id}"
            f":{event.escalation_tier or 0}:{route.id}"
        )

        record, created = DeliveryRecord.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "hotel": event.hotel,
                "route": route,
                "request": event.request,
                "channel": "EMAIL",
                "target": route.target,
                "event_type": event.event_type,
                "status": DeliveryRecord.Status.QUEUED,
                "message_type": "TEMPLATE",
            },
        )
        if not created:
            return record

        params = self._build_params(event)
        send_email_notification.delay(record.id, params)
        return record

    def _build_params(self, event):
        req = event.request
        dept_name = event.extra.get("original_department_name") or event.department.name
        return {
            "hotel_name": event.hotel.name,
            "primary_color": event.hotel.primary_color or "#1a1a1a",
            "guest_name": req.guest_stay.guest.get_full_name(),
            "room_number": req.guest_stay.room_number,
            "department": dept_name,
            "subject": event.display_name,
            "request_type": req.get_request_type_display(),
            "public_id": str(req.public_id),
            "event_type": event.event_type,
            "escalation_tier": event.escalation_tier,
        }
