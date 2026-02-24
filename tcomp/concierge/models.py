import secrets
import uuid
import zoneinfo
from datetime import date, timedelta

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
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

    # --- Notification channel toggles ---
    whatsapp_notifications_enabled = models.BooleanField(default=False)
    email_notifications_enabled = models.BooleanField(default=False)

    class EscalationChannel(models.TextChoices):
        NONE = 'NONE', 'None'
        EMAIL = 'EMAIL', 'Email'
        WHATSAPP = 'WHATSAPP', 'WhatsApp'
        EMAIL_WHATSAPP = 'EMAIL_WHATSAPP', 'Email + WhatsApp'

    escalation_fallback_channel = models.CharField(
        max_length=14,
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

    # --- Booking Window Defaults ---
    default_booking_opens_hours = models.PositiveIntegerField(
        default=0,
        help_text='Default hours before event start when bookings open. 0 = always open.',
    )
    default_booking_closes_hours = models.PositiveIntegerField(
        default=0,
        help_text='Default hours before event start when bookings close. 0 = no cutoff.',
    )

    # --- Explore Tab (fieldguide destination link) ---
    destination = models.ForeignKey(
        'guides.Destination', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='hotels',
        help_text='Linked fieldguide destination for Explore tab',
    )

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
    ALLOWED_DEPARTMENT_ICONS = frozenset({
        # Dining
        'utensils-crossed', 'coffee', 'wine', 'beer', 'pizza', 'cake',
        'chef-hat', 'cup-soda', 'ice-cream-cone', 'salad', 'soup',
        # Wellness / Spa
        'waves', 'droplets', 'flower-2', 'heart-pulse', 'bath', 'flame',
        # Activities / Tours
        'mountain', 'bike', 'tent', 'compass', 'map', 'camera',
        'binoculars', 'footprints', 'trees', 'sailboat', 'fish',
        # Transport
        'car', 'bus', 'plane', 'ship', 'train-front',
        # Fitness
        'dumbbell', 'trophy',
        # Accommodation
        'bed-double', 'key', 'door-open', 'lamp',
        # Services
        'concierge-bell', 'phone', 'mail', 'printer', 'shirt', 'scissors',
        'briefcase', 'gift', 'ticket', 'calendar-days', 'clock',
        # Entertainment
        'music', 'tv', 'gamepad-2', 'palette', 'book-open',
        # Shopping
        'shopping-bag', 'store', 'gem',
        # Kids / Family
        'baby', 'dog', 'puzzle',
        # Nature
        'sun', 'moon', 'leaf', 'flower',
        # General
        'shield-check', 'info', 'map-pin', 'wifi', 'credit-card',
        'luggage', 'cigarette-off', 'heart', 'star', 'sparkles',
    })
    icon = models.CharField(max_length=50, blank=True)
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

    # Slugs reserved for frontend route segments
    RESERVED_SLUGS = frozenset({
        'explore', 'events', 'request', 'requests',
        'confirmation', 'verify', 'api', 'manifest-json',
    })

    def clean(self):
        from django.core.exceptions import ValidationError
        super().clean()
        if self.slug and self.slug.lower() in self.RESERVED_SLUGS:
            raise ValidationError(
                {'slug': f'"{self.slug}" is a reserved word and cannot be used as a department slug.'}
            )

    def save(self, *args, **kwargs):
        # Keep is_active in sync with status (dual-write)
        self.is_active = self.status == ContentStatus.PUBLISHED
        if not self.slug:
            base = slugify(self.name) or 'department'
            slug = base[:100]
            counter = 1
            while (
                slug.lower() in self.RESERVED_SLUGS
                or Department.objects.filter(hotel=self.hotel, slug=slug).exclude(pk=self.pk).exists()
            ):
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

    # --- Top Deals ---
    is_top_deal = models.BooleanField(default=False)
    deal_price_display = models.CharField(max_length=100, blank=True, default='')
    deal_ends_at = models.DateTimeField(null=True, blank=True)

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
        indexes = [
            models.Index(fields=['is_top_deal', 'deal_ends_at']),
        ]

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


class DepartmentImage(models.Model):
    """Gallery images for a department."""
    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name='gallery_images',
    )
    image = models.ImageField(upload_to='department_gallery/')
    alt_text = models.CharField(max_length=255, blank=True)
    display_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'created_at']

    def __str__(self):
        return f'Image {self.id} for dept {self.department.name}'


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

    # Booking window (null = use hotel default)
    booking_opens_hours = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Hours before event start when bookings open. Null = use hotel default.',
    )
    booking_closes_hours = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Hours before event start when bookings close. Null = use hotel default.',
    )

    # Notification routing
    notify_department = models.BooleanField(
        default=True,
        help_text='When True, also notify the resolved department contacts for requests on this event.',
    )

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
        """Validate recurrence rule schema and booking window cross-field constraints."""
        # Booking window: opens must be >= closes when both non-zero.
        # Guard: skip when hotel is missing (temp instances from serializer validation).
        if self.hotel_id:
            opens = self.get_effective_booking_opens_hours()
            closes = self.get_effective_booking_closes_hours()
            if opens > 0 and closes > 0 and opens < closes:
                raise ValidationError(
                    'Booking opens window must be >= closes window '
                    '(e.g. opens 48h before, closes 2h before).'
                )

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

    def get_effective_booking_opens_hours(self) -> int:
        """Resolve opens window: event override > hotel default > 0 (no limit)."""
        if self.booking_opens_hours is not None:
            return self.booking_opens_hours
        return self.hotel.default_booking_opens_hours

    def get_effective_booking_closes_hours(self) -> int:
        """Resolve closes window: event override > hotel default > 0 (no cutoff)."""
        if self.booking_closes_hours is not None:
            return self.booking_closes_hours
        return self.hotel.default_booking_closes_hours

    def get_booking_window_for(self, target_dt):
        """
        Returns (opens_at, closes_at) datetimes for a specific occurrence.

        Boundary semantics (canonical, used everywhere):
          opens_at is inclusive  — now >= opens_at  → bookable
          closes_at is exclusive — now < closes_at  → bookable

        Args:
            target_dt: The datetime of the occurrence being booked.

        Returns:
            (opens_at, closes_at) — both are timezone-aware datetimes.
            opens_at is None when opens_hours == 0 (always open).
            closes_at falls back to target_dt when closes_hours == 0.
        """
        opens_hours = self.get_effective_booking_opens_hours()
        closes_hours = self.get_effective_booking_closes_hours()

        opens_at = (target_dt - timedelta(hours=opens_hours)) if opens_hours > 0 else None
        closes_at = (target_dt - timedelta(hours=closes_hours)) if closes_hours > 0 else target_dt

        return (opens_at, closes_at)

    def is_bookable_for(self, target_dt, now=None):
        """
        Check if the event is currently within its booking window for a
        specific occurrence datetime.

        Returns False if target_dt is None (no valid occurrence).
        """
        if target_dt is None:
            return False
        now = now or timezone.now()
        opens_at, closes_at = self.get_booking_window_for(target_dt)

        if opens_at is not None and now < opens_at:
            return False  # Too early
        if now >= closes_at:
            return False  # Too late (or event started)
        return True

    def resolve_target_datetime(self, occurrence_date=None):
        """
        Resolve the target occurrence datetime for booking window checks.

        For one-time events: returns event_start.
        For recurring events with occurrence_date: combines occurrence_date
            with event_start time in hotel timezone.
        For recurring events without occurrence_date: returns next_occurrence.
        Returns None if no valid target exists.

        DST ambiguity handling:
            Uses datetime.replace(tzinfo=...) which applies the zone's offset
            for that date. For "fall back" ambiguous times (e.g. 1:30 AM occurs
            twice), Python's zoneinfo picks the FIRST (earlier/DST) occurrence
            (fold=0). For "spring forward" nonexistent times (e.g. 2:30 AM is
            skipped), zoneinfo folds to the post-transition offset. Tests should
            use these same semantics for boundary assertions.
        """
        if not self.is_recurring:
            return self.event_start

        if occurrence_date:
            hotel_tz = zoneinfo.ZoneInfo(self.hotel.timezone or 'UTC')
            event_local_time = self.event_start.astimezone(hotel_tz).time()
            from datetime import datetime as dt
            naive = dt.combine(occurrence_date, event_local_time)
            # fold=0 → first occurrence for ambiguous times (DST fall-back)
            return naive.replace(tzinfo=hotel_tz)

        return self.get_next_occurrence()

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


class EventImage(models.Model):
    """Gallery images for an event."""
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name='gallery_images',
    )
    image = models.ImageField(upload_to='event_gallery/')
    alt_text = models.CharField(max_length=255, blank=True)
    display_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'created_at']

    def __str__(self):
        return f'Image {self.id} for event {self.event.name}'


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
        'channel', 'phone',  # For WhatsApp ack activities
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
        BOOKING = 'BOOKING', 'Booking Email'
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

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['hotel'],
                condition=Q(placement='BOOKING'),
                name='unique_booking_qr_per_hotel',
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = secrets.token_urlsafe(6)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'QR {self.code} ({self.hotel.name} - {self.placement})'


class QRScanDaily(models.Model):
    """Daily aggregate of raw QR code scans (pre-verification)."""
    qr_code = models.ForeignKey(QRCode, on_delete=models.CASCADE, related_name='daily_scans')
    date = models.DateField()  # Hotel-local date (not UTC)
    scan_count = models.PositiveIntegerField(default=0)
    unique_visitors = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('qr_code', 'date')
        indexes = [
            models.Index(fields=['date', 'qr_code']),
        ]

    def __str__(self):
        return f'QR {self.qr_code.code} on {self.date}: {self.scan_count} scans'


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


class HotelInfoSection(models.Model):
    ALLOWED_ICONS = frozenset({
        'wifi', 'clock', 'utensils', 'phone', 'car', 'map-pin', 'shield',
        'info', 'coffee', 'dumbbell', 'waves', 'luggage', 'credit-card',
        'calendar', 'key', 'shirt', 'baby', 'dog', 'cigarette', 'heart',
    })

    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, related_name='info_sections',
    )
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True)
    display_order = models.IntegerField(default=0)
    is_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'id']
        indexes = [
            models.Index(fields=['hotel', 'display_order']),
        ]

    def __str__(self):
        return f'{self.title} ({self.hotel.name})'


def _hex_color_validator(value):
    """Allow empty string or valid #RRGGBB hex color."""
    import re
    from django.core.exceptions import ValidationError
    if value and not re.match(r'^#[0-9a-fA-F]{6}$', value):
        raise ValidationError('Must be a valid hex color (e.g. #FF5733) or empty.')


class BookingEmailTemplate(models.Model):
    """One per hotel. Stores customizable text for the post-booking welcome email."""
    hotel = models.OneToOneField(
        Hotel, on_delete=models.CASCADE, related_name='booking_email_template',
    )
    qr_code = models.ForeignKey(
        QRCode, on_delete=models.SET_NULL, null=True, blank=True,
        help_text='Auto-created BOOKING QR code. Links to hotel landing page.',
    )
    subject = models.CharField(max_length=200, blank=True)
    heading = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True, help_text='Welcome message paragraph(s)')
    features = models.JSONField(default=list, blank=True, help_text='List of feature highlight strings')
    cta_text = models.CharField(max_length=100, blank=True)
    footer_text = models.CharField(max_length=500, blank=True)
    primary_color = models.CharField(
        max_length=7, blank=True, default='',
        validators=[_hex_color_validator],
        help_text='Override hotel primary color for this email. Empty = use hotel default.',
    )
    accent_color = models.CharField(
        max_length=7, blank=True, default='',
        validators=[_hex_color_validator],
        help_text='Override hotel accent color for this email. Empty = use hotel default.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Booking Email Template'

    def clean(self):
        import re
        from django.core.exceptions import ValidationError
        if self.qr_code_id:
            qr = self.qr_code
            if qr.hotel_id != self.hotel_id:
                raise ValidationError({'qr_code': 'QR code must belong to the same hotel.'})
            if qr.placement != 'BOOKING':
                raise ValidationError({'qr_code': 'Only BOOKING QR codes can be linked.'})
        errors = {}
        for field in ('primary_color', 'accent_color'):
            val = getattr(self, field)
            if val and not re.match(r'^#[0-9a-fA-F]{6}$', val):
                errors[field] = 'Must be a valid hex color (e.g. #FF5733) or empty.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        import re
        from django.core.exceptions import ValidationError
        errors = {}
        for field in ('primary_color', 'accent_color'):
            val = getattr(self, field)
            if val and not re.match(r'^#[0-9a-fA-F]{6}$', val):
                errors[field] = 'Must be a valid hex color (e.g. #FF5733) or empty.'
        if errors:
            raise ValidationError(errors)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'Booking email — {self.hotel.name}'


class NotificationRoute(models.Model):
    """Routes external notifications (WhatsApp, Email) to specific contacts per department/experience."""

    class Channel(models.TextChoices):
        WHATSAPP = 'WHATSAPP'
        EMAIL = 'EMAIL'

    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, related_name='notification_routes',
    )
    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, null=True, blank=True,
        related_name='notification_routes',
    )
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, null=True, blank=True,
        related_name='notification_routes',
    )
    experience = models.ForeignKey(
        Experience, on_delete=models.CASCADE, null=True, blank=True,
        related_name='notification_routes',
        help_text='If set, route only fires for this experience. If null, fires for all requests in the department.',
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)

    # Target contact — either linked to a team member or free-text for external contacts
    member = models.ForeignKey(
        HotelMembership, on_delete=models.CASCADE, null=True, blank=True,
        related_name='notification_routes',
        help_text='Link to an existing team member. When set, target is auto-derived from member contact info.',
    )
    target = models.CharField(
        max_length=255,
        help_text='Phone (E.164) or email. Auto-filled from member if linked, or manually entered for external contacts.',
    )
    label = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # --- Department-scoped routes (existing) ---
            # Experience-specific routes: one per (dept, exp, channel, target)
            models.UniqueConstraint(
                fields=['department', 'experience', 'channel', 'target'],
                condition=Q(experience__isnull=False),
                name='unique_route_with_experience',
            ),
            # Department-wide routes: one per (dept, channel, target) where experience IS NULL
            models.UniqueConstraint(
                fields=['department', 'channel', 'target'],
                condition=Q(experience__isnull=True, event__isnull=True),
                name='unique_route_dept_wide',
            ),
            # --- Event-scoped routes (new) ---
            # Event routes: one per (event, channel, target)
            models.UniqueConstraint(
                fields=['event', 'channel', 'target'],
                condition=Q(event__isnull=False),
                name='unique_event_channel_target',
            ),
            # --- Scope exclusivity ---
            # Exactly one scope: department XOR event
            models.CheckConstraint(
                check=(
                    Q(department__isnull=False, event__isnull=True)
                    | Q(department__isnull=True, event__isnull=False)
                ),
                name='route_scope_dept_xor_event',
            ),
            # Experience only valid on department-scoped routes
            models.CheckConstraint(
                check=Q(event__isnull=True) | Q(experience__isnull=True),
                name='route_no_experience_on_event_scope',
            ),
        ]

    def clean(self):
        """Cross-hotel FK consistency and scope validation."""
        from django.core.exceptions import ValidationError
        errors = {}
        # Scope: exactly one of department or event
        has_dept = self.department_id is not None
        has_event = self.event_id is not None
        if has_dept == has_event:
            errors['__all__'] = 'Exactly one of department or event must be set.'
        if has_dept and self.department.hotel_id != self.hotel_id:
            errors['department'] = 'Department must belong to this hotel.'
        if has_event and self.event.hotel_id != self.hotel_id:
            errors['event'] = 'Event must belong to this hotel.'
        if has_event and self.experience_id:
            errors['experience'] = 'Experience cannot be set on event-scoped routes.'
        if self.experience_id and self.experience.department_id != self.department_id:
            errors['experience'] = 'Experience must belong to the selected department.'
        if self.member_id and self.member.hotel_id != self.hotel_id:
            errors['member'] = 'Team member must belong to this hotel.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        import re
        # Normalize phone to digits-only on write (canonical E.164 without +)
        if self.channel == self.Channel.WHATSAPP:
            self.target = re.sub(r'\D', '', self.target)
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        scope = self.event.name if self.event_id else self.department.name if self.department_id else '?'
        return f'{self.channel} → {self.target} ({scope})'


class DeliveryRecord(models.Model):
    """Audit log for channel deliveries (WhatsApp, Email). Push uses existing Notification model."""

    class Status(models.TextChoices):
        QUEUED = 'QUEUED'
        SENT = 'SENT'
        DELIVERED = 'DELIVERED'
        FAILED = 'FAILED'
        SKIPPED = 'SKIPPED'

    class MessageType(models.TextChoices):
        TEMPLATE = 'TEMPLATE'
        SESSION = 'SESSION'

    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, db_index=True,
        help_text='Denormalized for efficient per-hotel log queries without joining through nullable route.',
    )
    route = models.ForeignKey(
        'NotificationRoute', on_delete=models.SET_NULL, null=True,
    )
    request = models.ForeignKey(
        ServiceRequest, on_delete=models.CASCADE, null=True,
    )
    channel = models.CharField(max_length=20, choices=NotificationRoute.Channel.choices)
    target = models.CharField(max_length=255)
    event_type = models.CharField(max_length=50)
    message_type = models.CharField(
        max_length=20, choices=MessageType.choices, default=MessageType.TEMPLATE,
        help_text='TEMPLATE = paid utility template, SESSION = free session message',
    )
    idempotency_key = models.CharField(
        max_length=200, unique=True, null=True, blank=True,
        help_text="'{event_type}:{public_id}:{escalation_tier}:{route_id}' — prevents duplicate sends on retries.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    provider_message_id = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When recipient tapped a quick-reply button (ack/esc_ack/view).',
    )

    class Meta:
        indexes = [
            models.Index(fields=['channel', 'status', '-created_at']),
            models.Index(
                fields=['channel', 'provider_message_id'],
                name='idx_dlvr_chan_provmsg',
                condition=Q(provider_message_id__gt=''),
            ),
            models.Index(
                fields=['hotel', '-created_at'],
                name='idx_dlvr_hotel_created',
            ),
            models.Index(
                fields=['hotel', 'channel', 'status', '-created_at'],
                name='idx_dlvr_hotel_filtered',
            ),
            models.Index(
                fields=['channel', 'target', 'request'],
                name='idx_dlvr_ack_lookup',
                condition=Q(acknowledged_at__isnull=True),
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['channel', 'provider_message_id'],
                name='uniq_chan_provider_msg',
                condition=Q(provider_message_id__gt=''),
            ),
        ]

    def __str__(self):
        return f'{self.channel} → {self.target} [{self.status}]'


class WhatsAppServiceWindow(models.Model):
    """Tracks 24-hour WhatsApp service windows per phone number per hotel.

    A window opens when a staff member sends ANY inbound message to our
    WhatsApp Business number (including tapping a quick-reply button on
    a template). While the window is active, we can send free session
    messages instead of paid utility templates — reducing costs by ~92%.
    """
    hotel = models.ForeignKey(
        Hotel, on_delete=models.CASCADE, related_name='wa_service_windows',
    )
    phone = models.CharField(max_length=20, help_text='Digits-only E.164 without +')
    last_inbound_at = models.DateTimeField(
        help_text='When we last received an inbound message from this phone',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('hotel', 'phone')]
        indexes = [
            models.Index(fields=['phone', 'last_inbound_at']),
        ]

    @property
    def is_active(self):
        """True if the 24h window is still open (with 5-minute safety margin)."""
        from datetime import timedelta
        return self.last_inbound_at > timezone.now() - timedelta(hours=23, minutes=55)

    def __str__(self):
        return f'WA window: {self.phone} @ {self.hotel.name}'
