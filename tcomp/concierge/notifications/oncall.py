import logging
import re
from dataclasses import dataclass

from django.conf import settings

from concierge.models import DeliveryRecord, WhatsAppServiceWindow

from .base import ChannelAdapter

logger = logging.getLogger(__name__)


@dataclass
class OncallTarget:
    """A single on-call delivery target (one per channel)."""
    channel: str  # "WHATSAPP" or "EMAIL"
    target: str   # phone number or email address


class OncallAdapter(ChannelAdapter):
    """On-call fallback for escalation notifications.

    When a guest request escalates, this adapter sends a notification to the
    hotel's designated on-call contact in addition to the route-based
    notifications. Acts as a safety net — even if no department-level routes
    are configured, the on-call person always gets alerted.

    Only fires for ``event_type == "escalation"``.
    """

    def is_enabled(self, hotel):
        return (
            hotel.escalation_fallback_channel != "NONE"
            and (hotel.oncall_email or hotel.oncall_phone)
        )

    def get_recipients(self, event):
        if event.event_type != "escalation":
            return []

        hotel = event.hotel
        channel = hotel.escalation_fallback_channel
        targets = []

        if channel in ("EMAIL", "EMAIL_WHATSAPP") and hotel.oncall_email:
            targets.append(OncallTarget(channel="EMAIL", target=hotel.oncall_email))

        if channel in ("WHATSAPP", "EMAIL_WHATSAPP") and hotel.oncall_phone:
            # WhatsApp requires Gupshup API key
            if settings.GUPSHUP_WA_API_KEY:
                # Normalize to digits-only, matching NotificationRoute.save()
                phone = re.sub(r"\D", "", hotel.oncall_phone)
                targets.append(OncallTarget(channel="WHATSAPP", target=phone))

        if not targets or not event.request:
            return targets

        # Dedupe against route-based adapters that already ran (Push → WA → Email
        # fire before Oncall in the dispatcher). If a route-based adapter already
        # queued a DeliveryRecord for the same target at this escalation tier,
        # skip the on-call target to avoid duplicate messages.
        #
        # Route-based idempotency keys embed the tier as ":{tier}:{route_id}",
        # so we filter by the public_id + tier segment to match this specific
        # escalation level without cross-tier false positives.
        tier = event.escalation_tier or 0
        tier_segment = f":{event.request.public_id}:{tier}:"
        already_covered = set(
            DeliveryRecord.objects.filter(
                request=event.request,
                event_type="escalation",
                idempotency_key__contains=tier_segment,
                target__in=[t.target for t in targets],
            ).values_list("channel", "target")
        )

        return [
            t for t in targets
            if (t.channel, t.target) not in already_covered
        ]

    def send(self, oncall_target, event):
        if oncall_target.channel == "WHATSAPP":
            return self._send_whatsapp(oncall_target, event)
        return self._send_email(oncall_target, event)

    def _send_whatsapp(self, oncall_target, event):
        from .tasks import (
            send_whatsapp_session_notification,
            send_whatsapp_template_notification,
        )

        idempotency_key = (
            f"oncall:wa:{event.request.public_id}"
            f":{event.escalation_tier or 0}"
        )

        window = WhatsAppServiceWindow.objects.filter(
            hotel=event.hotel, phone=oncall_target.target,
        ).first()
        use_session = window and window.is_active

        record, created = DeliveryRecord.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "hotel": event.hotel,
                "route": None,
                "request": event.request,
                "channel": "WHATSAPP",
                "target": oncall_target.target,
                "event_type": event.event_type,
                "status": DeliveryRecord.Status.QUEUED,
                "message_type": "SESSION" if use_session else "TEMPLATE",
            },
        )
        if not created:
            return record

        params = self._build_params(event)

        if use_session:
            send_whatsapp_session_notification.delay(record.id, params)
        else:
            send_whatsapp_template_notification.delay(record.id, params)

        return record

    def _send_email(self, oncall_target, event):
        from .tasks import send_email_notification

        idempotency_key = (
            f"oncall:email:{event.request.public_id}"
            f":{event.escalation_tier or 0}"
        )

        record, created = DeliveryRecord.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "hotel": event.hotel,
                "route": None,
                "request": event.request,
                "channel": "EMAIL",
                "target": oncall_target.target,
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
