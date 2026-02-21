import secrets
import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.core.validators import RegexValidator
from django.db import models
from django.utils.text import slugify

hex_color_validator = RegexValidator(
    regex=r'^#[0-9a-fA-F]{6}$',
    message='Enter a valid hex color (e.g. #1a1a1a)',
)


class ContentStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    PUBLISHED = 'PUBLISHED', 'Published'
    UNPUBLISHED = 'UNPUBLISHED', 'Unpublished'


class Hotel(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=100)
    description = models.TextField(blank=True)
    tagline = models.CharField(max_length=255, blank=True)
    logo = models.ImageField(upload_to='hotel_logos/', blank=True)
    cover_image = models.ImageField(upload_to='hotel_covers/', blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pin_code = models.CharField(max_length=10, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)
    location = gis_models.PointField(srid=4326, null=True, blank=True)
    timezone = models.CharField(max_length=50, default='Asia/Kolkata')

    fallback_department = models.ForeignKey(
        'Department', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='fallback_for_hotels',
    )
    room_number_pattern = models.CharField(
        max_length=100, default=r'^\d{3,4}$',
        help_text='Regex for valid room numbers',
    )
    blocked_room_numbers = models.JSONField(
        default=list,
        help_text='Room numbers that are always rejected',
    )
    room_number_min = models.IntegerField(null=True, blank=True)
    room_number_max = models.IntegerField(null=True, blank=True)

    escalation_enabled = models.BooleanField(default=False)

    class EscalationChannel(models.TextChoices):
        NONE = 'NONE', 'None'
        EMAIL = 'EMAIL', 'Email'
        SMS = 'SMS', 'SMS'
        EMAIL_SMS = 'EMAIL_SMS', 'Email + SMS'

    escalation_fallback_channel = models.CharField(
        max_length=10,
        choices=EscalationChannel.choices,
        default=EscalationChannel.NONE,
    )
    oncall_email = models.EmailField(blank=True)
    oncall_phone = models.CharField(max_length=20, blank=True)
    require_frontdesk_kiosk = models.BooleanField(default=True)
    settings_configured = models.BooleanField(
        default=False,
        help_text='Set to True when admin first saves hotel settings',
    )
    escalation_tier_minutes = models.JSONField(
        null=True, blank=True,
        help_text='Per-hotel override, e.g. [15, 30, 60]. Falls back to settings default.',
    )

    # --- Brand Colors ---
    primary_color = models.CharField(
        max_length=7, default='#1a1a1a', validators=[hex_color_validator],
        help_text='Primary brand color (hex)',
    )
    secondary_color = models.CharField(
        max_length=7, default='#f5f5f4', validators=[hex_color_validator],
        help_text='Secondary brand color (hex)',
    )
    accent_color = models.CharField(
        max_length=7, default='#b45309', validators=[hex_color_validator],
        help_text='Accent/CTA color (hex)',
    )

    # --- Typography ---
    heading_font = models.CharField(
        max_length=100, blank=True, default='BioRhyme',
        help_text='Font name for headings',
    )
    body_font = models.CharField(
        max_length=100, blank=True, default='Brinnan',
        help_text='Font name for body text',
    )

    # --- Favicon & OG ---
    favicon = models.ImageField(upload_to='hotel_favicons/', blank=True)
    og_image = models.ImageField(upload_to='hotel_og/', blank=True)

    # --- Social Links ---
    instagram_url = models.URLField(blank=True, default='')
    facebook_url = models.URLField(blank=True, default='')
    twitter_url = models.URLField(blank=True, default='')
    whatsapp_number = models.CharField(
        max_length=20, blank=True, default='',
        help_text='With country code, e.g. +919876543210',
    )

    # --- Legal & Footer ---
    footer_text = models.CharField(max_length=500, blank=True, default='')
    terms_url = models.URLField(blank=True, default='')
    privacy_url = models.URLField(blank=True, default='')

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.blocked_room_numbers:
            self.blocked_room_numbers = ['0', '00', '000', '999', '9999']
        super().save(*args, **kwargs)

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.escalation_enabled:
            has_fallback = (
                self.escalation_fallback_channel != self.EscalationChannel.NONE
                and (self.oncall_email or self.oncall_phone)
            )
            if not has_fallback and not self.require_frontdesk_kiosk:
                raise ValidationError(
                    'Escalation enabled requires either a fallback channel with '
                    'contact info or require_frontdesk_kiosk=True.'
                )

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']


class HotelMembership(models.Model):
    class Role(models.TextChoices):
        SUPERADMIN = 'SUPERADMIN', 'Super Admin'
        ADMIN = 'ADMIN', 'Admin'
        STAFF = 'STAFF', 'Staff'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='hotel_memberships',
    )
    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, related_name='memberships',
    )
    role = models.CharField(max_length=12, choices=Role.choices)
    department = models.ForeignKey(
        'Department', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='staff_memberships',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'hotel')

    def __str__(self):
        return f'{self.user} - {self.hotel.name} ({self.role})'


class Department(models.Model):
    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, related_name='departments',
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100)
    description = models.TextField(blank=True)
    photo = models.ImageField(upload_to='department_photos/', blank=True)
    icon = models.ImageField(upload_to='department_icons/', blank=True)
    display_order = models.IntegerField(default=0)
    schedule = models.JSONField(
        default=dict,
        help_text='Schedule JSON with timezone, default hours, and overrides',
    )
    is_ops = models.BooleanField(
        default=False,
        help_text='Ops departments never pause escalation',
    )
    is_active = models.BooleanField(default=True)  # deprecated — transitional, computed from status
    status = models.CharField(
        max_length=12,
        choices=ContentStatus.choices,
        default=ContentStatus.DRAFT,
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # Keep is_active in sync with status (dual-write)
        self.is_active = self.status == ContentStatus.PUBLISHED
        if not self.slug:
            base = slugify(self.name) or 'department'
            slug = base[:100]
            counter = 1
            while Department.objects.filter(hotel=self.hotel, slug=slug).exclude(pk=self.pk).exists():
                suffix = f'-{counter}'
                slug = f'{base[:100 - len(suffix)]}{suffix}'
                counter += 1
            self.slug = slug
        if not self.schedule:
            self.schedule = {
                'timezone': 'Asia/Kolkata',
                'default': [['00:00', '23:59']],
            }
        super().save(*args, **kwargs)

    class Meta:
        unique_together = ('hotel', 'slug')
        ordering = ['display_order', 'name']

    def __str__(self):
        return f'{self.name} ({self.hotel.name})'


class Experience(models.Model):
    """Hotel experiences — separate from guides.Experience."""

    class Category(models.TextChoices):
        DINING = 'DINING', 'Dining'
        SPA = 'SPA', 'Spa'
        ACTIVITY = 'ACTIVITY', 'Activity'
        TOUR = 'TOUR', 'Tour'
        TRANSPORT = 'TRANSPORT', 'Transport'
        OTHER = 'OTHER', 'Other'

    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name='experiences',
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100)
    description = models.TextField(blank=True)
    photo = models.ImageField(upload_to='experience_photos/', blank=True)
    cover_image = models.ImageField(upload_to='experience_covers/', blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_display = models.CharField(max_length=100, blank=True)
    category = models.CharField(max_length=20, choices=Category.choices)
    timing = models.CharField(max_length=100, blank=True)
    duration = models.CharField(max_length=100, blank=True)
    capacity = models.CharField(max_length=100, blank=True)
    highlights = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)  # deprecated — transitional, computed from status
    status = models.CharField(
        max_length=12,
        choices=ContentStatus.choices,
        default=ContentStatus.DRAFT,
    )
    published_at = models.DateTimeField(null=True, blank=True)
    display_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # Keep is_active in sync with status (dual-write)
        self.is_active = self.status == ContentStatus.PUBLISHED
        if not self.slug:
            base = slugify(self.name) or 'experience'
            slug = base[:100]
            counter = 1
            while Experience.objects.filter(department=self.department, slug=slug).exclude(pk=self.pk).exists():
                suffix = f'-{counter}'
                slug = f'{base[:100 - len(suffix)]}{suffix}'
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    class Meta:
        unique_together = ('department', 'slug')
        ordering = ['display_order', 'name']

    def __str__(self):
        return f'{self.name} ({self.department.name})'


class ExperienceImage(models.Model):
    """Gallery images for an experience (separate from thumbnail/cover)."""
    experience = models.ForeignKey(
        Experience, on_delete=models.CASCADE, related_name='gallery_images',
    )
    image = models.ImageField(upload_to='experience_gallery/')
    alt_text = models.CharField(max_length=255, blank=True)
    display_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'created_at']

    def __str__(self):
        return f'Image {self.id} for {self.experience.name}'


class GuestStay(models.Model):
    guest = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='stays',
    )
    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, related_name='guest_stays',
    )
    room_number = models.CharField(max_length=20, blank=True)
    qr_code = models.ForeignKey(
        'QRCode', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='guest_stays',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=['hotel', 'room_number', 'created_at']),
            models.Index(fields=['guest', 'hotel']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['guest', 'hotel'],
                condition=models.Q(is_active=True),
                name='unique_active_stay_per_guest_hotel',
            ),
        ]

    def __str__(self):
        return f'Stay: {self.guest} @ {self.hotel.name} room {self.room_number}'


class OTPCode(models.Model):
    class Channel(models.TextChoices):
        WHATSAPP = 'WHATSAPP', 'WhatsApp'
        SMS = 'SMS', 'SMS'

    phone = models.CharField(max_length=20, db_index=True)
    code_hash = models.CharField(max_length=128)
    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE,
        null=True, blank=True, related_name='otp_codes',
    )
    channel = models.CharField(
        max_length=10, choices=Channel.choices,
        default=Channel.WHATSAPP,
    )
    gupshup_message_id = models.CharField(max_length=100, blank=True)
    sms_fallback_sent = models.BooleanField(default=False)
    sms_fallback_claimed_at = models.DateTimeField(null=True, blank=True)
    wa_delivered = models.BooleanField(default=False)
    ip_hash = models.CharField(max_length=128, blank=True)
    attempts = models.IntegerField(default=0)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=['phone', 'created_at']),
        ]

    def __str__(self):
        return f'OTP for {self.phone} ({self.channel})'


class ServiceRequest(models.Model):
    class Status(models.TextChoices):
        CREATED = 'CREATED', 'Created'
        ACKNOWLEDGED = 'ACKNOWLEDGED', 'Acknowledged'
        CONFIRMED = 'CONFIRMED', 'Confirmed'
        NOT_AVAILABLE = 'NOT_AVAILABLE', 'Not Available'
        NO_SHOW = 'NO_SHOW', 'No Show'
        ALREADY_BOOKED_OFFLINE = 'ALREADY_BOOKED_OFFLINE', 'Already Booked Offline'
        EXPIRED = 'EXPIRED', 'Expired'

    class RequestType(models.TextChoices):
        BOOKING = 'BOOKING', 'Booking'
        INQUIRY = 'INQUIRY', 'Inquiry'
        CUSTOM = 'CUSTOM', 'Custom'

    VALID_TRANSITIONS = {
        'CREATED': ['ACKNOWLEDGED'],
        'ACKNOWLEDGED': ['CONFIRMED', 'NOT_AVAILABLE', 'NO_SHOW', 'ALREADY_BOOKED_OFFLINE'],
    }

    TERMINAL_STATUSES = {'CONFIRMED', 'NOT_AVAILABLE', 'NO_SHOW', 'ALREADY_BOOKED_OFFLINE', 'EXPIRED'}

    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, related_name='service_requests',
    )
    public_id = models.UUIDField(unique=True, default=uuid.uuid4, db_index=True)
    guest_stay = models.ForeignKey(
        GuestStay, on_delete=models.CASCADE, related_name='requests',
    )
    experience = models.ForeignKey(
        Experience, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='requests',
    )
    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name='requests',
    )
    request_type = models.CharField(
        max_length=20, choices=RequestType.choices,
    )
    status = models.CharField(
        max_length=25, choices=Status.choices, default=Status.CREATED,
    )
    guest_notes = models.TextField(blank=True)
    guest_date = models.DateField(null=True, blank=True)
    guest_time = models.TimeField(null=True, blank=True)
    guest_count = models.IntegerField(null=True, blank=True)
    staff_notes = models.TextField(blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='assigned_requests',
    )
    confirmation_token = models.UUIDField(unique=True, default=uuid.uuid4)
    confirmation_reason = models.CharField(max_length=50, blank=True)
    after_hours = models.BooleanField(default=False)
    response_due_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['hotel', 'status', 'created_at']),
            models.Index(fields=['hotel', 'created_at']),
            models.Index(fields=['guest_stay', 'created_at']),
            models.Index(fields=['hotel', 'updated_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'Request {self.public_id} ({self.status})'


class RequestActivity(models.Model):
    class Action(models.TextChoices):
        CREATED = 'CREATED', 'Created'
        VIEWED = 'VIEWED', 'Viewed'
        ACKNOWLEDGED = 'ACKNOWLEDGED', 'Acknowledged'
        CONFIRMED = 'CONFIRMED', 'Confirmed'
        CLOSED = 'CLOSED', 'Closed'
        ESCALATED = 'ESCALATED', 'Escalated'
        NOTE_ADDED = 'NOTE_ADDED', 'Note Added'
        OWNERSHIP_TAKEN = 'OWNERSHIP_TAKEN', 'Ownership Taken'
        EXPIRED = 'EXPIRED', 'Expired'

    ALLOWED_DETAIL_KEYS = {
        'status_from', 'status_to', 'note_length',
        'assigned_to_id', 'department_id',
    }

    request = models.ForeignKey(
        ServiceRequest, on_delete=models.CASCADE, related_name='activities',
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    escalation_tier = models.PositiveSmallIntegerField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['request', 'action']),
        ]
        constraints = [
            # One escalation per (request, tier)
            models.UniqueConstraint(
                fields=['request', 'escalation_tier'],
                condition=models.Q(action='ESCALATED'),
                name='unique_escalation_per_tier',
            ),
            # escalation_tier required when action=ESCALATED
            models.CheckConstraint(
                condition=(
                    models.Q(action='ESCALATED', escalation_tier__isnull=False)
                    | ~models.Q(action='ESCALATED')
                ),
                name='escalated_requires_tier',
            ),
            # claimed_at/notified_at only for ESCALATED
            models.CheckConstraint(
                condition=(
                    models.Q(action='ESCALATED')
                    | models.Q(claimed_at__isnull=True, notified_at__isnull=True)
                ),
                name='claim_notify_only_for_escalated',
            ),
        ]
        ordering = ['created_at']

    def clean(self):
        """Strip disallowed keys from details to prevent PII leakage."""
        if self.details:
            self.details = {
                k: v for k, v in self.details.items()
                if k in self.ALLOWED_DETAIL_KEYS
            }

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.action} on {self.request.public_id}'


class Notification(models.Model):
    class NotificationType(models.TextChoices):
        NEW_REQUEST = 'NEW_REQUEST', 'New Request'
        ESCALATION = 'ESCALATION', 'Escalation'
        DAILY_DIGEST = 'DAILY_DIGEST', 'Daily Digest'
        SYSTEM = 'SYSTEM', 'System'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='notifications',
    )
    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, related_name='notifications',
    )
    request = models.ForeignKey(
        ServiceRequest, on_delete=models.CASCADE,
        null=True, blank=True, related_name='notifications',
    )
    title = models.CharField(max_length=255)
    body = models.TextField()
    notification_type = models.CharField(
        max_length=20, choices=NotificationType.choices,
    )
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'is_read', 'created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.notification_type}: {self.title}'


class PushSubscription(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='push_subscriptions',
    )
    subscription_info = models.JSONField(
        help_text='{ endpoint, keys: { p256dh, auth } } — no user_agent, no IP',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Push sub for {self.user}'


class QRCode(models.Model):
    class Placement(models.TextChoices):
        LOBBY = 'LOBBY', 'Lobby'
        ROOM = 'ROOM', 'Room'
        RESTAURANT = 'RESTAURANT', 'Restaurant'
        SPA = 'SPA', 'Spa'
        POOL = 'POOL', 'Pool'
        BAR = 'BAR', 'Bar'
        GYM = 'GYM', 'Gym'
        GARDEN = 'GARDEN', 'Garden'
        OTHER = 'OTHER', 'Other'

    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, related_name='qr_codes',
    )
    code = models.CharField(
        max_length=20, unique=True, db_index=True,
        help_text='Short non-sequential public identifier',
    )
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='qr_codes',
    )
    placement = models.CharField(max_length=20, choices=Placement.choices)
    label = models.CharField(max_length=255)
    qr_image = models.ImageField(upload_to='qr_codes/')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name='created_qr_codes',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = secrets.token_urlsafe(6)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'QR {self.code} ({self.hotel.name} - {self.placement})'


class EscalationHeartbeat(models.Model):
    class HeartbeatStatus(models.TextChoices):
        OK = 'OK', 'OK'
        FAILED = 'FAILED', 'Failed'

    task_name = models.CharField(max_length=100, unique=True)
    last_run = models.DateTimeField()
    status = models.CharField(
        max_length=10, choices=HeartbeatStatus.choices,
    )
    details = models.TextField(blank=True)

    def __str__(self):
        return f'{self.task_name}: {self.status} ({self.last_run})'
