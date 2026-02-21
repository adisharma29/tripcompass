import secrets
import uuid
import zoneinfo
from datetime import date, timedelta

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
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


class Event(models.Model):
    """Hotel-scoped event with optional experience link, scheduling, and recurrence."""

    VALID_DAYS = {'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'}
    DAY_MAP = {'MON': 0, 'TUE': 1, 'WED': 2, 'THU': 3, 'FRI': 4, 'SAT': 5, 'SUN': 6}

    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name='events')
    experience = models.ForeignKey(
        Experience, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='events',
    )
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='events',
        help_text='For request routing. Null = hotel-wide (falls back to hotel.fallback_department)',
    )

    # Display fields
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220)
    description = models.TextField(blank=True)
    photo = models.ImageField(upload_to='event_photos/', blank=True)
    cover_image = models.ImageField(upload_to='event_covers/', blank=True)
    price_display = models.CharField(max_length=100, blank=True)
    highlights = models.JSONField(default=list, blank=True)
    category = models.CharField(
        max_length=20, choices=Experience.Category.choices, default='OTHER',
    )

    # Event timing
    event_start = models.DateTimeField()
    event_end = models.DateTimeField(null=True, blank=True)
    is_all_day = models.BooleanField(default=False)

    # Recurrence (simple JSON — not RFC-5545)
    # e.g. {"freq": "weekly", "days": ["SAT"], "until": "2026-04-01"}
    #      {"freq": "daily", "interval": 2, "until": "2026-03-15"}
    is_recurring = models.BooleanField(default=False)
    recurrence_rule = models.JSONField(null=True, blank=True)

    # Visibility & status
    is_featured = models.BooleanField(default=False)
    status = models.CharField(
        max_length=12, choices=ContentStatus.choices, default=ContentStatus.DRAFT,
    )
    is_active = models.BooleanField(default=True)  # dual-write from status
    published_at = models.DateTimeField(null=True, blank=True)
    auto_expire = models.BooleanField(default=True)
    display_order = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'event_start']
        unique_together = [('hotel', 'slug')]
        indexes = [
            models.Index(fields=['hotel', 'status', 'is_featured', 'event_start']),
            models.Index(fields=['hotel', 'status', 'auto_expire', 'event_end']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(event_end__isnull=True) | models.Q(event_end__gte=models.F('event_start')),
                name='event_end_gte_start',
            ),
        ]

    def save(self, *args, **kwargs):
        self.is_active = self.status == ContentStatus.PUBLISHED
        if not self.slug:
            base = slugify(self.name) or 'event'
            slug = base[:200]
            counter = 1
            while Event.objects.filter(hotel=self.hotel, slug=slug).exclude(pk=self.pk).exists():
                suffix = f'-{counter}'
                slug = f'{base[:200 - len(suffix)]}{suffix}'
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def clean(self):
        """Validate recurrence rule schema."""
        if self.is_recurring:
            if not self.recurrence_rule or not isinstance(self.recurrence_rule, dict):
                raise ValidationError(
                    {'recurrence_rule': 'Recurrence rule is required when is_recurring is True.'}
                )
            rule = self.recurrence_rule
            freq = rule.get('freq')
            if freq not in ('daily', 'weekly', 'monthly'):
                raise ValidationError(
                    {'recurrence_rule': 'freq must be one of: daily, weekly, monthly.'}
                )
            interval = rule.get('interval', 1)
            if not isinstance(interval, int) or interval < 1:
                raise ValidationError(
                    {'recurrence_rule': 'interval must be a positive integer.'}
                )
            if freq == 'weekly':
                days = rule.get('days', [])
                if not days or not isinstance(days, list):
                    raise ValidationError(
                        {'recurrence_rule': 'days is required for weekly recurrence.'}
                    )
                if not all(d in self.VALID_DAYS for d in days):
                    raise ValidationError(
                        {'recurrence_rule': f'days must be a list of: {", ".join(sorted(self.VALID_DAYS))}'}
                    )
            until_str = rule.get('until')
            if until_str is not None:
                try:
                    date.fromisoformat(until_str)
                except (ValueError, TypeError):
                    raise ValidationError(
                        {'recurrence_rule': 'until must be a valid ISO date (YYYY-MM-DD).'}
                    )

    def get_next_occurrence(self, after=None):
        """Return the next datetime this event occurs, evaluated in hotel timezone.

        For non-recurring events: returns event_start if future, else None.
        For recurring events: iterates forward from event_start using the rule.
        Hard cap of 365 days to prevent infinite loops.
        """
        if after is None:
            after = timezone.now()

        if not self.is_recurring:
            return self.event_start if self.event_start > after else None

        rule = self.recurrence_rule
        if not rule or not isinstance(rule, dict):
            return None

        tz = zoneinfo.ZoneInfo(self.hotel.timezone)
        freq = rule.get('freq')
        interval = max(rule.get('interval', 1), 1)
        days = rule.get('days', [])
        until_str = rule.get('until')

        until_date = None
        if until_str:
            try:
                until_date = date.fromisoformat(until_str)
            except (ValueError, TypeError):
                pass

        start_local = self.event_start.astimezone(tz)
        start_time = start_local.time()
        start_date = start_local.date()

        # Start checking from after's date in hotel tz
        after_local = after.astimezone(tz)
        check_date = max(start_date, after_local.date())
        max_date = check_date + timedelta(days=365)

        while check_date <= max_date:
            if until_date and check_date > until_date:
                return None

            candidate_dt = timezone.datetime.combine(
                check_date, start_time, tzinfo=tz,
            )

            is_match = False
            if freq == 'daily':
                days_diff = (check_date - start_date).days
                is_match = days_diff >= 0 and days_diff % interval == 0
            elif freq == 'weekly':
                valid_weekdays = {self.DAY_MAP[d] for d in days if d in self.DAY_MAP}
                if check_date.weekday() in valid_weekdays:
                    weeks_diff = (check_date - start_date).days // 7
                    is_match = weeks_diff >= 0 and weeks_diff % interval == 0
            elif freq == 'monthly':
                if check_date.day == start_date.day:
                    months_diff = (
                        (check_date.year - start_date.year) * 12
                        + (check_date.month - start_date.month)
                    )
                    is_match = months_diff >= 0 and months_diff % interval == 0

            if is_match and candidate_dt > after:
                return candidate_dt

            check_date += timedelta(days=1)

        return None

    def get_routing_department(self):
        """Resolve concrete department for request routing.

        Returns Department or None. Caller (serializer) must guard
        for same-hotel and not-is_ops — this method only resolves.
        """
        if self.department_id:
            return self.department
        if self.experience_id and self.experience.department_id:
            return self.experience.department
        if self.hotel.fallback_department_id:
            return self.hotel.fallback_department
        return None

    def is_valid_occurrence(self, check_date):
        """Return True if check_date falls on a valid recurrence instance.

        Evaluated in hotel timezone. Used by serializer to validate
        occurrence_date on recurring event requests.
        """
        if not self.is_recurring or not self.recurrence_rule:
            return False

        tz = zoneinfo.ZoneInfo(self.hotel.timezone)
        rule = self.recurrence_rule
        freq = rule.get('freq')
        interval = max(rule.get('interval', 1), 1)
        days = rule.get('days', [])
        until_str = rule.get('until')

        # Check until bound
        if until_str:
            try:
                until_date_val = date.fromisoformat(until_str)
                if check_date > until_date_val:
                    return False
            except (ValueError, TypeError):
                pass

        # check_date must be >= event_start date (in hotel tz)
        start_local = self.event_start.astimezone(tz).date()
        if check_date < start_local:
            return False

        if freq == 'weekly':
            valid_days = {self.DAY_MAP[d] for d in days if d in self.DAY_MAP}
            if check_date.weekday() not in valid_days:
                return False
            weeks_diff = (check_date - start_local).days // 7
            return weeks_diff % interval == 0
        elif freq == 'daily':
            days_diff = (check_date - start_local).days
            return days_diff % interval == 0
        elif freq == 'monthly':
            if check_date.day != start_local.day:
                return False
            months_diff = (
                (check_date.year - start_local.year) * 12
                + (check_date.month - start_local.month)
            )
            return months_diff >= 0 and months_diff % interval == 0

        return False

    def __str__(self):
        return f'{self.name} ({self.hotel.name})'


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
    event = models.ForeignKey(
        'Event', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='requests',
    )
    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name='requests',
    )
    occurrence_date = models.DateField(
        null=True, blank=True,
        help_text='For recurring events: which occurrence the guest selected',
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
