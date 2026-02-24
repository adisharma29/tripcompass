import logging

from django.db.models import Q

from concierge.models import HotelMembership, Notification

from .base import ChannelAdapter, NotificationEvent

logger = logging.getLogger(__name__)


class PushAdapter(ChannelAdapter):
    """Web push + in-app notification. Always enabled.

    Notification record is written synchronously (instant bell UI).
    Web-push delivery is enqueued asynchronously (avoids blocking the request
    lifecycle on per-subscription HTTP calls).
    """

    def is_enabled(self, hotel):
        return True  # Push is always on

    def get_recipients(self, event):
        base = HotelMembership.objects.filter(hotel=event.hotel, is_active=True)

        if event.event_type == "daily_digest":
            # Digest is admin/superadmin-only (preserves current behavior)
            return base.filter(
                role__in=[HotelMembership.Role.ADMIN, HotelMembership.Role.SUPERADMIN]
            ).select_related("user")

        admin_q = Q(role__in=[HotelMembership.Role.ADMIN, HotelMembership.Role.SUPERADMIN])

        # If event has notify_department=False, only admins get push (no dept staff)
        if event.event_obj and not event.event_obj.notify_department:
            return base.filter(admin_q).select_related("user")

        # Standard: STAFF in dept + ADMIN/SUPERADMIN
        return base.filter(
            Q(department=event.department) | admin_q
        ).select_related("user")

    def send(self, membership, event):
        # 1. Create Notification record synchronously (for bell UI + SSE)
        title = self._build_title(event)
        notification = Notification.objects.create(
            user=membership.user,
            hotel=event.hotel,
            request=event.request,
            title=title,
            body=self._build_notification_body(event),
            notification_type=self._map_type(event.event_type),
        )
        # 2. Enqueue web-push delivery asynchronously via Celery
        # Skip web push for daily_digest — bell-icon notification only
        if event.event_type != "daily_digest":
            from .tasks import send_push_notification_task

            send_push_notification_task.delay(
                user_id=membership.user.id,
                title=title,
                body=self._build_push_body(event),
                url=(
                    f"/dashboard/requests/{event.request.public_id}"
                    if event.request
                    else "/dashboard"
                ),
            )
        return notification

    # ------------------------------------------------------------------
    # Title / body builders
    # ------------------------------------------------------------------

    def _build_title(self, event):
        """Backward-compatible title format.

        Current notify_department_staff() uses: f'New request: {department.name}'
        Preserve this exact format for request.created to avoid confusing staff.
        """
        if event.event_type == "daily_digest":
            return "Daily Summary"
        if event.event_type == "request.created":
            return f"New request: {event.department.name}"
        if event.event_type == "escalation":
            return f"Escalation: {event.department.name}"
        if event.event_type == "response_due":
            return f"Reminder: {event.department.name}"
        if event.event_type == "after_hours_fallback":
            dept_name = event.extra.get("original_department_name") or event.department.name
            return f"After-hours request: {dept_name}"
        if event.department:
            return f"Notification: {event.department.name}"
        return "Notification"

    def _build_notification_body(self, event):
        """Body for in-app Notification record.

        Current behavior: f'Room {room_number} - {request_type}'
        Preserves exact format for backward compatibility.
        """
        if event.event_type == "daily_digest":
            extra = event.extra
            return (
                f"{extra.get('total_requests', 0)} requests today — "
                f"{extra.get('confirmed', 0)} confirmed, "
                f"{extra.get('pending', 0)} pending"
            )
        if event.request:
            room = event.request.guest_stay.room_number
            return f"Room {room} - {event.request.request_type}"
        return ""

    def _build_push_body(self, event):
        """Body for web push notification.

        Current behavior: f'Room {room_number}' (shorter than Notification body —
        no request_type). Intentional: push payloads should be concise.
        """
        if event.request:
            return f"Room {event.request.guest_stay.room_number}"
        return ""

    def _map_type(self, event_type):
        return {
            "request.created": Notification.NotificationType.NEW_REQUEST,
            "escalation": Notification.NotificationType.ESCALATION,
            "daily_digest": Notification.NotificationType.DAILY_DIGEST,
            "response_due": Notification.NotificationType.NEW_REQUEST,
            "after_hours_fallback": Notification.NotificationType.NEW_REQUEST,
        }.get(event_type, Notification.NotificationType.SYSTEM)
