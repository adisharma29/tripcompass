from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from concierge.models import Department, Hotel, ServiceRequest


@dataclass
class NotificationEvent:
    """Canonical event passed to all adapters."""

    event_type: str  # "request.created", "escalation", "response_due", "daily_digest", "after_hours_fallback"
    hotel: Hotel
    department: Optional[Department] = None  # None for hotel-wide events (daily_digest)
    request: Optional[ServiceRequest] = None  # None for daily_digest
    event_obj: Optional["Event"] = None  # Set when request originated from an Event
    escalation_tier: Optional[int] = None  # For escalation events
    extra: dict = field(default_factory=dict)  # Adapter-specific data

    @property
    def is_request_event(self) -> bool:
        return self.request is not None and self.department is not None

    @property
    def display_name(self) -> str:
        """Human-readable name for the request subject."""
        if self.event_obj:
            return self.event_obj.name
        if self.request and self.request.experience:
            return self.request.experience.name
        return "General Request"


class ChannelAdapter(ABC):
    """Base class for notification channel adapters."""

    @abstractmethod
    def is_enabled(self, hotel: Hotel) -> bool:
        """Check if this channel is enabled for the hotel."""

    @abstractmethod
    def get_recipients(self, event: NotificationEvent) -> list:
        """Return list of delivery targets for this event."""

    @abstractmethod
    def send(self, recipient, event: NotificationEvent):
        """Send notification to a single recipient.
        Returns DeliveryRecord (WhatsApp/Email) or Notification (Push)."""
