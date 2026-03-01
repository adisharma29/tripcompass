import logging

from .base import NotificationEvent
from .email import EmailAdapter
from .oncall import OncallAdapter
from .push import PushAdapter
from .whatsapp import WhatsAppAdapter

logger = logging.getLogger(__name__)

# Registry of all adapters (order = dispatch order).
# OncallAdapter runs last — safety-net for escalations after all route-based adapters.
ADAPTERS = [
    PushAdapter(),
    WhatsAppAdapter(),
    EmailAdapter(),
    OncallAdapter(),
]


def dispatch_notification(event: NotificationEvent):
    """Fan out a notification event to all enabled channel adapters.

    Called from: request creation, escalation, response-due reminder,
    daily digest, after-hours fallback.

    Note: daily_digest and after_hours_fallback have adapter-level guards
    (WhatsApp/Email adapters skip non-request events via is_request_event).
    """
    for adapter in ADAPTERS:
        if not adapter.is_enabled(event.hotel):
            continue
        try:
            recipients = adapter.get_recipients(event)
        except Exception:
            logger.exception(
                "%s.get_recipients() failed for %s",
                adapter.__class__.__name__,
                event.event_type,
            )
            continue  # Never let one adapter failure block others

        for recipient in recipients:
            try:
                adapter.send(recipient, event)
            except Exception:
                logger.exception(
                    "%s.send() failed for recipient %s on %s",
                    adapter.__class__.__name__,
                    getattr(recipient, "target", recipient),
                    event.event_type,
                )
                # Continue to next recipient — one bad recipient must not abort the rest
