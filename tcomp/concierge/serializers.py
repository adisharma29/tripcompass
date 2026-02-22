import re

import bleach
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.utils import timezone
from rest_framework import serializers

from .models import (
    ContentStatus, Hotel, HotelMembership, Department, Experience,
    ExperienceImage, Event, GuestStay, ServiceRequest, RequestActivity,
    Notification, PushSubscription, QRCode, HotelInfoSection,
)
from .validators import validate_image_upload

ALLOWED_HTML_TAGS = [
    'p', 'br', 'strong', 'em', 'u', 's', 'h2', 'h3',
    'ul', 'ol', 'li', 'blockquote', 'hr', 'a',
]
ALLOWED_HTML_ATTRIBUTES = {
    'a': ['href'],
}


def _clean_image(file):
    """Validate size/type via magic bytes and return sanitized (EXIF-stripped, resized) file."""
    if not file:
        return file
    buf, fmt = validate_image_upload(file)
    ext = 'png' if fmt == 'png' else 'jpg'
    name = getattr(file, 'name', 'upload')
    clean_name = f'{name.rsplit(".", 1)[0]}.{ext}'
    return InMemoryUploadedFile(
        buf, 'image', clean_name,
        f'image/{fmt}', buf.getbuffer().nbytes, None,
    )


# ---------------------------------------------------------------------------
# Image serializer (used by both public and admin)
# ---------------------------------------------------------------------------

class ExperienceImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExperienceImage
        fields = ['id', 'image', 'alt_text', 'display_order', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate_image(self, value):
        return _clean_image(value)


# ---------------------------------------------------------------------------
# Public serializers
# ---------------------------------------------------------------------------

class ExperiencePublicSerializer(serializers.ModelSerializer):
    gallery_images = ExperienceImageSerializer(many=True, read_only=True)
    is_deal_active = serializers.SerializerMethodField()

    class Meta:
        model = Experience
        fields = [
            'id', 'name', 'slug', 'description', 'photo', 'cover_image',
            'price_display', 'category', 'timing', 'duration', 'capacity',
            'highlights', 'display_order', 'gallery_images',
            'is_top_deal', 'deal_price_display', 'deal_ends_at',
            'is_deal_active',
        ]

    def get_is_deal_active(self, obj):
        if not obj.is_top_deal:
            return False
        if obj.deal_ends_at is None:
            return True
        return obj.deal_ends_at > timezone.now()


class TopDealSerializer(ExperiencePublicSerializer):
    """Extended public serializer for /top-deals/ endpoint with routing fields."""
    department_slug = serializers.CharField(source='department.slug', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True)

    class Meta(ExperiencePublicSerializer.Meta):
        fields = ExperiencePublicSerializer.Meta.fields + [
            'department_slug', 'department_name',
        ]


class DepartmentPublicSerializer(serializers.ModelSerializer):
    experiences = ExperiencePublicSerializer(many=True, read_only=True)

    class Meta:
        model = Department
        fields = [
            'id', 'name', 'slug', 'description', 'photo', 'icon',
            'display_order', 'schedule', 'is_ops', 'experiences',
        ]


class HotelInfoSectionPublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = HotelInfoSection
        fields = ['id', 'title', 'body', 'icon', 'display_order']


class HotelInfoSectionSerializer(serializers.ModelSerializer):
    """Admin serializer — full CRUD with validation."""
    ALLOWED_ICONS = HotelInfoSection.ALLOWED_ICONS

    class Meta:
        model = HotelInfoSection
        fields = [
            'id', 'title', 'body', 'icon', 'display_order', 'is_visible',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_icon(self, value):
        if value and value not in self.ALLOWED_ICONS:
            raise serializers.ValidationError(
                f'Invalid icon key. Allowed: {sorted(self.ALLOWED_ICONS)}'
            )
        return value

    def validate_body(self, value):
        if value:
            return bleach.clean(value, tags=ALLOWED_HTML_TAGS, attributes=ALLOWED_HTML_ATTRIBUTES, strip=True)
        return value


class HotelPublicSerializer(serializers.ModelSerializer):
    departments = DepartmentPublicSerializer(many=True, read_only=True)
    info_sections = HotelInfoSectionPublicSerializer(many=True, read_only=True)

    class Meta:
        model = Hotel
        fields = [
            'id', 'name', 'slug', 'description', 'tagline',
            'logo', 'cover_image', 'timezone',
            # Brand
            'primary_color', 'secondary_color', 'accent_color',
            'heading_font', 'body_font', 'favicon', 'og_image',
            # Social
            'instagram_url', 'facebook_url', 'twitter_url', 'whatsapp_number',
            # Footer & Legal
            'footer_text', 'terms_url', 'privacy_url',
            # Relations
            'departments', 'info_sections',
        ]


# ---------------------------------------------------------------------------
# Admin serializers
# ---------------------------------------------------------------------------

class HotelSettingsSerializer(serializers.ModelSerializer):
    # Write-only flags to clear image fields via multipart or JSON
    favicon_clear = serializers.BooleanField(write_only=True, required=False, default=False)
    og_image_clear = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Hotel
        fields = [
            'timezone', 'room_number_pattern', 'blocked_room_numbers',
            'room_number_min', 'room_number_max',
            'escalation_enabled', 'escalation_fallback_channel',
            'oncall_email', 'oncall_phone', 'require_frontdesk_kiosk',
            'escalation_tier_minutes', 'settings_configured',
            # Booking window defaults
            'default_booking_opens_hours', 'default_booking_closes_hours',
            # Brand
            'primary_color', 'secondary_color', 'accent_color',
            'heading_font', 'body_font', 'favicon', 'og_image',
            'favicon_clear', 'og_image_clear',
            # Social
            'instagram_url', 'facebook_url', 'twitter_url', 'whatsapp_number',
            # Footer & Legal
            'footer_text', 'terms_url', 'privacy_url',
        ]
        read_only_fields = ['settings_configured']

    def validate_favicon(self, value):
        return _clean_image(value)

    def validate_og_image(self, value):
        return _clean_image(value)

    def update(self, instance, validated_data):
        if not instance.settings_configured:
            validated_data['settings_configured'] = True
        # Handle image field clearing
        for field in ('favicon', 'og_image'):
            if validated_data.pop(f'{field}_clear', False):
                file_field = getattr(instance, field)
                if file_field:
                    file_field.delete(save=False)
                setattr(instance, field, '')
        return super().update(instance, validated_data)

    def validate(self, data):
        escalation_enabled = data.get(
            'escalation_enabled',
            self.instance.escalation_enabled if self.instance else False,
        )
        if escalation_enabled:
            channel = data.get(
                'escalation_fallback_channel',
                getattr(self.instance, 'escalation_fallback_channel', 'NONE'),
            )
            oncall_email = data.get(
                'oncall_email',
                getattr(self.instance, 'oncall_email', ''),
            )
            oncall_phone = data.get(
                'oncall_phone',
                getattr(self.instance, 'oncall_phone', ''),
            )
            kiosk = data.get(
                'require_frontdesk_kiosk',
                getattr(self.instance, 'require_frontdesk_kiosk', True),
            )
            has_fallback = channel != 'NONE' and (oncall_email or oncall_phone)
            if not has_fallback and not kiosk:
                raise serializers.ValidationError(
                    'Escalation enabled requires either a fallback channel with '
                    'contact info or require_frontdesk_kiosk=True.'
                )

        # Booking window defaults: upper bounds + opens >= closes
        opens = data.get(
            'default_booking_opens_hours',
            getattr(self.instance, 'default_booking_opens_hours', 0),
        )
        closes = data.get(
            'default_booking_closes_hours',
            getattr(self.instance, 'default_booking_closes_hours', 0),
        )
        if opens > 8760:
            raise serializers.ValidationError(
                {'default_booking_opens_hours': 'Cannot exceed 8760 hours (1 year).'}
            )
        if closes > 720:
            raise serializers.ValidationError(
                {'default_booking_closes_hours': 'Cannot exceed 720 hours (30 days).'}
            )
        if opens > 0 and closes > 0 and opens < closes:
            raise serializers.ValidationError(
                'Default booking opens must be >= default booking closes.'
            )

        return data


class _ExperienceNestedSerializer(serializers.ModelSerializer):
    """Lightweight serializer for experiences nested inside admin DepartmentSerializer.
    Includes status/is_active fields that ExperiencePublicSerializer omits."""
    gallery_images = ExperienceImageSerializer(many=True, read_only=True)
    is_deal_active = serializers.SerializerMethodField()

    class Meta:
        model = Experience
        fields = [
            'id', 'name', 'slug', 'description', 'photo', 'cover_image',
            'price', 'price_display', 'category', 'timing', 'duration',
            'capacity', 'highlights', 'display_order', 'gallery_images',
            'is_active', 'status', 'published_at', 'created_at', 'updated_at',
            'is_top_deal', 'deal_price_display', 'deal_ends_at',
            'is_deal_active',
        ]

    def get_is_deal_active(self, obj):
        if not obj.is_top_deal:
            return False
        if obj.deal_ends_at is None:
            return True
        return obj.deal_ends_at > timezone.now()


class DepartmentSerializer(serializers.ModelSerializer):
    experiences = _ExperienceNestedSerializer(many=True, read_only=True)
    icon_clear = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Department
        fields = [
            'id', 'name', 'slug', 'description', 'photo', 'icon', 'icon_clear',
            'display_order', 'schedule', 'is_ops', 'is_active',
            'status', 'published_at',
            'experiences', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'is_active', 'published_at', 'created_at', 'updated_at']

    def validate_photo(self, value):
        return _clean_image(value)

    def validate_icon(self, value):
        return _clean_image(value)

    def validate_description(self, value):
        if value:
            return bleach.clean(value, tags=ALLOWED_HTML_TAGS, attributes=ALLOWED_HTML_ATTRIBUTES, strip=True)
        return value

    _VALID_DAY_KEYS = frozenset([
        'monday', 'tuesday', 'wednesday', 'thursday',
        'friday', 'saturday', 'sunday',
    ])
    _LEGACY_DAY_MAP = {
        'mon': 'monday', 'tue': 'tuesday', 'wed': 'wednesday',
        'thu': 'thursday', 'fri': 'friday', 'sat': 'saturday', 'sun': 'sunday',
    }
    _TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')

    def validate_schedule(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('Schedule must be a JSON object.')
        # timezone
        tz = value.get('timezone')
        if tz and not isinstance(tz, str):
            raise serializers.ValidationError('timezone must be a string.')
        # default slots
        default = value.get('default')
        if default is not None:
            self._validate_slots(default, 'default')
        # overrides — normalize legacy abbreviated keys (MON → monday, etc.)
        overrides = value.get('overrides')
        if overrides is not None:
            if not isinstance(overrides, dict):
                raise serializers.ValidationError('overrides must be a JSON object.')
            normalized = {}
            for key, slots in overrides.items():
                canonical = self._LEGACY_DAY_MAP.get(key.lower(), key.lower())
                if canonical not in self._VALID_DAY_KEYS:
                    raise serializers.ValidationError(
                        f'Invalid override key "{key}". Must be a day name '
                        f'(e.g. "monday").'
                    )
                if canonical in normalized:
                    raise serializers.ValidationError(
                        f'Duplicate override for "{canonical}" '
                        f'(check for both abbreviated and full day names).'
                    )
                self._validate_slots(slots, f'overrides.{canonical}')
                normalized[canonical] = slots
            value['overrides'] = normalized
        # Reject unknown top-level keys
        allowed = {'timezone', 'default', 'overrides'}
        extra = set(value.keys()) - allowed
        if extra:
            raise serializers.ValidationError(
                f'Unknown schedule keys: {", ".join(sorted(extra))}. '
                f'Allowed: timezone, default, overrides.'
            )
        return value

    def _validate_slots(self, slots, field_name):
        if not isinstance(slots, list):
            raise serializers.ValidationError(f'{field_name} must be an array of [open, close] pairs.')
        for i, slot in enumerate(slots):
            if not isinstance(slot, (list, tuple)) or len(slot) != 2:
                raise serializers.ValidationError(f'{field_name}[{i}] must be a [open, close] pair.')
            for t in slot:
                if not isinstance(t, str) or not self._TIME_RE.match(t):
                    raise serializers.ValidationError(
                        f'{field_name}[{i}] contains invalid time "{t}". Use HH:MM format.'
                    )

    def validate_status(self, value):
        instance = self.instance
        if instance and instance.status != ContentStatus.PUBLISHED and value == ContentStatus.PUBLISHED:
            # First publish — published_at will be set in update/create
            pass
        return value

    def update(self, instance, validated_data):
        new_status = validated_data.get('status')
        if new_status == ContentStatus.PUBLISHED and instance.status != ContentStatus.PUBLISHED:
            validated_data['published_at'] = timezone.now()
        if validated_data.pop('icon_clear', False):
            if instance.icon:
                instance.icon.delete(save=False)
            setattr(instance, 'icon', '')
        return super().update(instance, validated_data)

    def create(self, validated_data):
        validated_data.pop('icon_clear', None)
        if validated_data.get('status') == ContentStatus.PUBLISHED:
            validated_data['published_at'] = timezone.now()
        return super().create(validated_data)


class ExperienceSerializer(serializers.ModelSerializer):
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(),
    )
    gallery_images = ExperienceImageSerializer(many=True, read_only=True)
    is_deal_active = serializers.SerializerMethodField()

    class Meta:
        model = Experience
        fields = [
            'id', 'department', 'name', 'slug', 'description',
            'photo', 'cover_image', 'price', 'price_display',
            'category', 'timing', 'duration', 'capacity',
            'highlights', 'is_active', 'display_order',
            'status', 'published_at',
            'is_top_deal', 'deal_price_display', 'deal_ends_at',
            'is_deal_active',
            'gallery_images', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'is_active', 'published_at', 'created_at', 'updated_at', 'is_deal_active']

    def get_is_deal_active(self, obj):
        if not obj.is_top_deal:
            return False
        if obj.deal_ends_at is None:
            return True
        return obj.deal_ends_at > timezone.now()

    def validate_department(self, value):
        hotel = self.context.get('hotel')
        if hotel and value.hotel != hotel:
            raise serializers.ValidationError(
                'Department does not belong to this hotel.'
            )
        return value

    def validate_photo(self, value):
        return _clean_image(value)

    def validate_cover_image(self, value):
        return _clean_image(value)

    def validate_description(self, value):
        if value:
            return bleach.clean(value, tags=ALLOWED_HTML_TAGS, attributes=ALLOWED_HTML_ATTRIBUTES, strip=True)
        return value

    def validate_deal_ends_at(self, value):
        # Coerce FormData empty string to None
        if value == '' or value is None:
            return None
        return value

    def validate(self, data):
        is_top_deal = data.get('is_top_deal', getattr(self.instance, 'is_top_deal', False))

        if is_top_deal:
            deal_price = data.get(
                'deal_price_display',
                getattr(self.instance, 'deal_price_display', ''),
            )
            if not deal_price or not deal_price.strip():
                raise serializers.ValidationError(
                    {'deal_price_display': 'Deal price is required when marking as a top deal.'}
                )
            # Only enforce future check when deal_ends_at is explicitly in this request
            if 'deal_ends_at' in data and data['deal_ends_at'] is not None:
                if data['deal_ends_at'] <= timezone.now():
                    raise serializers.ValidationError(
                        {'deal_ends_at': 'Deal end time must be in the future.'}
                    )
        else:
            # Auto-clear deal fields when toggled off
            data['deal_price_display'] = ''
            data['deal_ends_at'] = None

        return data

    def update(self, instance, validated_data):
        new_status = validated_data.get('status')
        if new_status == ContentStatus.PUBLISHED and instance.status != ContentStatus.PUBLISHED:
            validated_data['published_at'] = timezone.now()
        return super().update(instance, validated_data)

    def create(self, validated_data):
        if validated_data.get('status') == ContentStatus.PUBLISHED:
            validated_data['published_at'] = timezone.now()
        return super().create(validated_data)


# ---------------------------------------------------------------------------
# Event serializers
# ---------------------------------------------------------------------------

class _NullableIntegerField(serializers.IntegerField):
    """IntegerField that coerces '' to None — needed for multipart/FormData.

    min_value is enforced here (not via DRF's MinValueValidator) to avoid
    the validator crashing when the coerced value is None.
    """
    def __init__(self, **kwargs):
        self._min = kwargs.pop('min_value', None)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if data == '' or data is None:
            return None
        value = super().to_internal_value(data)
        if self._min is not None and value < self._min:
            self.fail('min_value', min_value=self._min)
        return value


class EventSerializer(serializers.ModelSerializer):
    """Admin serializer for Event CRUD."""
    experience = serializers.PrimaryKeyRelatedField(
        queryset=Experience.objects.all(), required=False, allow_null=True,
    )
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(), required=False, allow_null=True,
    )
    booking_opens_hours = _NullableIntegerField(
        required=False, allow_null=True, min_value=0,
    )
    booking_closes_hours = _NullableIntegerField(
        required=False, allow_null=True, min_value=0,
    )
    photo_clear = serializers.BooleanField(write_only=True, required=False, default=False)
    cover_image_clear = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Event
        fields = [
            'id', 'hotel', 'experience', 'department',
            'name', 'slug', 'description', 'photo', 'cover_image',
            'photo_clear', 'cover_image_clear',
            'price_display', 'highlights', 'category',
            'event_start', 'event_end', 'is_all_day',
            'is_recurring', 'recurrence_rule',
            'booking_opens_hours', 'booking_closes_hours',
            'is_featured', 'status', 'is_active', 'published_at',
            'auto_expire', 'display_order',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'hotel', 'slug', 'is_active', 'published_at',
            'created_at', 'updated_at',
        ]

    def validate_experience(self, value):
        if value is None:
            return value
        hotel = self.context.get('hotel')
        if hotel and value.department.hotel != hotel:
            raise serializers.ValidationError(
                'Experience does not belong to this hotel.'
            )
        return value

    def validate_department(self, value):
        if value is None:
            return value
        hotel = self.context.get('hotel')
        if hotel and value.hotel != hotel:
            raise serializers.ValidationError(
                'Department does not belong to this hotel.'
            )
        if value.is_ops:
            raise serializers.ValidationError(
                'Cannot route event requests to an ops department.'
            )
        return value

    def validate_photo(self, value):
        return _clean_image(value)

    def validate_cover_image(self, value):
        return _clean_image(value)

    def validate_description(self, value):
        if value:
            return bleach.clean(value, tags=ALLOWED_HTML_TAGS, attributes=ALLOWED_HTML_ATTRIBUTES, strip=True)
        return value

    def validate(self, data):
        # Cross-field: event_end >= event_start
        event_start = data.get('event_start', getattr(self.instance, 'event_start', None))
        event_end = data.get('event_end', getattr(self.instance, 'event_end', None))
        if event_start and event_end and event_end < event_start:
            raise serializers.ValidationError(
                {'event_end': 'End date/time must be on or after start date/time.'}
            )

        # Cross-field: recurrence
        is_recurring = data.get('is_recurring', getattr(self.instance, 'is_recurring', False))
        recurrence_rule = data.get('recurrence_rule', getattr(self.instance, 'recurrence_rule', None))
        if is_recurring and not recurrence_rule:
            raise serializers.ValidationError(
                {'recurrence_rule': 'Recurrence rule is required when is_recurring is True.'}
            )
        if is_recurring and recurrence_rule:
            # Validate schema via model clean()
            temp = Event(
                is_recurring=True,
                recurrence_rule=recurrence_rule,
                event_start=data.get('event_start', getattr(self.instance, 'event_start', None)),
            )
            try:
                temp.clean()
            except Exception as e:
                if hasattr(e, 'message_dict'):
                    raise serializers.ValidationError(e.message_dict)
                raise serializers.ValidationError({'recurrence_rule': str(e)})

        # Cross-field: booking window opens >= closes
        opens = data.get('booking_opens_hours')
        closes = data.get('booking_closes_hours')
        if opens is not None and opens > 8760:
            raise serializers.ValidationError(
                {'booking_opens_hours': 'Cannot exceed 8760 hours (1 year).'}
            )
        if closes is not None and closes > 720:
            raise serializers.ValidationError(
                {'booking_closes_hours': 'Cannot exceed 720 hours (30 days).'}
            )
        # Resolve effective values for cross-field check
        hotel = self.context.get('hotel') or (self.instance.hotel if self.instance else None)
        eff_opens = opens if opens is not None else (
            self.instance.booking_opens_hours if self.instance and self.instance.booking_opens_hours is not None
            else (hotel.default_booking_opens_hours if hotel else 0)
        )
        eff_closes = closes if closes is not None else (
            self.instance.booking_closes_hours if self.instance and self.instance.booking_closes_hours is not None
            else (hotel.default_booking_closes_hours if hotel else 0)
        )
        if eff_opens > 0 and eff_closes > 0 and eff_opens < eff_closes:
            raise serializers.ValidationError(
                'Booking opens window must be >= closes window.'
            )

        return data

    def _handle_image_clears(self, instance, validated_data):
        for field in ('photo', 'cover_image'):
            if validated_data.pop(f'{field}_clear', False):
                file_field = getattr(instance, field)
                if file_field:
                    file_field.delete(save=False)
                setattr(instance, field, '')

    def update(self, instance, validated_data):
        new_status = validated_data.get('status')
        if new_status == ContentStatus.PUBLISHED and instance.status != ContentStatus.PUBLISHED:
            validated_data['published_at'] = timezone.now()
        self._handle_image_clears(instance, validated_data)
        return super().update(instance, validated_data)

    def create(self, validated_data):
        validated_data.pop('photo_clear', None)
        validated_data.pop('cover_image_clear', None)
        if validated_data.get('status') == ContentStatus.PUBLISHED:
            validated_data['published_at'] = timezone.now()
        validated_data['hotel'] = self.context['hotel']
        return super().create(validated_data)


def _get_safe_routing_department(event):
    """Resolve routing department with safety checks.
    Returns Department or None. Used by both EventPublicSerializer
    and ServiceRequestCreateSerializer.
    """
    dept = event.get_routing_department()
    if dept is None:
        return None
    if dept.hotel_id != event.hotel_id:
        return None
    if dept.is_ops:
        return None
    return dept


class EventPublicSerializer(serializers.ModelSerializer):
    """Guest-facing serializer for published events."""
    next_occurrence = serializers.SerializerMethodField()
    booking_opens_at = serializers.SerializerMethodField()
    booking_closes_at = serializers.SerializerMethodField()
    is_bookable = serializers.SerializerMethodField()
    routing_department_slug = serializers.SerializerMethodField()
    routing_department_name = serializers.SerializerMethodField()
    experience_name = serializers.SerializerMethodField()
    experience_slug = serializers.SerializerMethodField()
    experience_department_slug = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            'id', 'name', 'slug', 'description', 'photo', 'cover_image',
            'price_display', 'highlights', 'category',
            'event_start', 'event_end', 'is_all_day',
            'is_recurring', 'recurrence_rule',
            'is_featured', 'next_occurrence',
            'booking_opens_at', 'booking_closes_at', 'is_bookable',
            'department',
            'routing_department_slug', 'routing_department_name',
            'experience_name', 'experience_slug', 'experience_department_slug',
        ]

    def get_next_occurrence(self, obj):
        dt = obj.get_next_occurrence()
        return dt.isoformat() if dt else None

    def get_booking_opens_at(self, obj):
        """Window for next_occurrence (display default). Frontend uses this for CTA state."""
        target = obj.resolve_target_datetime()
        if target is None:
            return None
        opens_at, _ = obj.get_booking_window_for(target)
        return opens_at.isoformat() if opens_at else None

    def get_booking_closes_at(self, obj):
        target = obj.resolve_target_datetime()
        if target is None:
            return None
        _, closes_at = obj.get_booking_window_for(target)
        return closes_at.isoformat() if closes_at else None

    def get_is_bookable(self, obj):
        """Bookable right now for the next occurrence."""
        target = obj.resolve_target_datetime()
        return obj.is_bookable_for(target)

    def get_routing_department_slug(self, obj):
        dept = _get_safe_routing_department(obj)
        return dept.slug if dept else None

    def get_routing_department_name(self, obj):
        dept = _get_safe_routing_department(obj)
        return dept.name if dept else None

    def get_experience_name(self, obj):
        return obj.experience.name if obj.experience else None

    def get_experience_slug(self, obj):
        return obj.experience.slug if obj.experience else None

    def get_experience_department_slug(self, obj):
        if obj.experience and obj.experience.department:
            return obj.experience.department.slug
        return None


class HotelMinimalSerializer(serializers.ModelSerializer):
    """Lightweight hotel serializer for embedding in membership responses."""

    class Meta:
        model = Hotel
        fields = ['id', 'name', 'slug']


class MemberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    phone = serializers.CharField(source='user.phone', read_only=True)
    hotel = HotelMinimalSerializer(read_only=True)

    class Meta:
        model = HotelMembership
        fields = [
            'id', 'hotel', 'email', 'first_name', 'last_name', 'phone',
            'role', 'department', 'is_active', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']

    def validate_department(self, value):
        hotel = self.context.get('hotel')
        if value and hotel and value.hotel != hotel:
            raise serializers.ValidationError(
                'Department does not belong to this hotel.'
            )
        return value

    def validate(self, data):
        role = data.get('role', getattr(self.instance, 'role', None))
        department = data.get('department', getattr(self.instance, 'department', None))
        if role == HotelMembership.Role.STAFF and not department:
            raise serializers.ValidationError(
                {'department': 'Department is required for STAFF role.'}
            )
        return data


class MemberCreateSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, default='')
    phone = serializers.CharField(required=False, default='')
    first_name = serializers.CharField(required=False, allow_blank=True, default='')
    last_name = serializers.CharField(required=False, allow_blank=True, default='')
    role = serializers.ChoiceField(choices=HotelMembership.Role.choices)
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(), required=False, allow_null=True,
    )

    def validate_department(self, value):
        hotel = self.context.get('hotel')
        if value and hotel and value.hotel != hotel:
            raise serializers.ValidationError(
                'Department does not belong to this hotel.'
            )
        return value

    def validate(self, data):
        if not data.get('email') and not data.get('phone'):
            raise serializers.ValidationError(
                'At least one of email or phone is required.'
            )
        if data['role'] == HotelMembership.Role.STAFF and not data.get('department'):
            raise serializers.ValidationError(
                {'department': 'Department is required for STAFF role.'}
            )
        return data


class QRCodeSerializer(serializers.ModelSerializer):
    target_url = serializers.SerializerMethodField()
    stay_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = QRCode
        fields = [
            'id', 'code', 'placement', 'label', 'department',
            'qr_image', 'target_url', 'is_active', 'stay_count',
            'created_at',
        ]
        read_only_fields = ['id', 'code', 'qr_image', 'created_at']

    def get_target_url(self, obj):
        from django.conf import settings
        return f'{settings.FRONTEND_ORIGIN}/h/{obj.hotel.slug}?qr={obj.code}'

    def validate_department(self, value):
        hotel = self.context.get('hotel')
        if value and hotel and value.hotel != hotel:
            raise serializers.ValidationError(
                'Department does not belong to this hotel.'
            )
        return value


# ---------------------------------------------------------------------------
# Guest serializers
# ---------------------------------------------------------------------------

class GuestStayUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = GuestStay
        fields = ['room_number']

    def validate_room_number(self, value):
        stay = self.instance
        if not stay:
            raise serializers.ValidationError('Stay not found.')

        hotel = stay.hotel

        # Pattern validation
        pattern = hotel.room_number_pattern
        if pattern and not re.match(pattern, value):
            raise serializers.ValidationError(
                f'Room number does not match expected format.'
            )

        # Blocked room numbers
        if value in (hotel.blocked_room_numbers or []):
            raise serializers.ValidationError(
                'This room number is not allowed.'
            )

        # Range validation
        try:
            room_int = int(value)
            if hotel.room_number_min is not None and room_int < hotel.room_number_min:
                raise serializers.ValidationError('Room number below minimum.')
            if hotel.room_number_max is not None and room_int > hotel.room_number_max:
                raise serializers.ValidationError('Room number above maximum.')
        except ValueError:
            pass  # Non-numeric rooms skip range check

        return value


class GuestStaySerializer(serializers.ModelSerializer):
    class Meta:
        model = GuestStay
        fields = ['id', 'hotel', 'room_number', 'is_active', 'created_at', 'expires_at']
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Service Request serializers
# ---------------------------------------------------------------------------

class RequestActivitySerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = RequestActivity
        fields = ['action', 'actor_name', 'details', 'created_at']

    def get_actor_name(self, obj):
        if obj.actor:
            return f'{obj.actor.first_name} {obj.actor.last_name}'.strip() or obj.actor.email
        return None


class ServiceRequestCreateSerializer(serializers.ModelSerializer):
    guest_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    # department is optional when event is provided (backend resolves via fallback chain)
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(), required=False, allow_null=True,
    )
    event = serializers.PrimaryKeyRelatedField(
        queryset=Event.objects.all(), required=False, allow_null=True,
    )
    occurrence_date = serializers.DateField(required=False, allow_null=True)

    class Meta:
        model = ServiceRequest
        fields = [
            'experience', 'department', 'event', 'occurrence_date',
            'request_type',
            'guest_name', 'guest_notes', 'guest_date',
            'guest_time', 'guest_count',
        ]

    def validate(self, data):
        hotel = self.context.get('hotel')
        event = data.get('event')
        experience = data.get('experience')
        department = data.get('department')

        if event:
            # --- Event-based request ---
            if event.hotel != hotel:
                raise serializers.ValidationError(
                    {'event': 'Event does not belong to this hotel.'}
                )
            if event.status != ContentStatus.PUBLISHED:
                raise serializers.ValidationError(
                    {'event': 'This event is not currently published.'}
                )
            # Freshness guard: reject ended events before hourly expiry runs
            now = timezone.now()
            if not event.is_recurring and event.event_end and event.event_end < now:
                raise serializers.ValidationError(
                    {'event': 'This event has already ended.'}
                )
            if event.is_recurring and event.get_next_occurrence() is None:
                raise serializers.ValidationError(
                    {'event': 'This recurring event has no upcoming occurrences.'}
                )
            # Resolve department via fallback chain with safety checks
            resolved_dept = _get_safe_routing_department(event)
            if resolved_dept is None:
                raise serializers.ValidationError(
                    {'event': 'This event cannot accept requests — no valid department configured.'}
                )
            data['department'] = resolved_dept
            # Experience override: always use event's experience
            if event.experience:
                data['experience'] = event.experience
            else:
                data.pop('experience', None)
            # Occurrence date validation
            occurrence_date = data.get('occurrence_date')
            if event.is_recurring:
                if not occurrence_date:
                    raise serializers.ValidationError(
                        {'occurrence_date': 'Occurrence date is required for recurring events.'}
                    )
                import zoneinfo
                hotel_tz = zoneinfo.ZoneInfo(event.hotel.timezone)
                hotel_now = timezone.now().astimezone(hotel_tz)
                hotel_today = hotel_now.date()
                if occurrence_date < hotel_today:
                    raise serializers.ValidationError(
                        {'occurrence_date': 'Occurrence date cannot be in the past.'}
                    )
                # For non-all-day events, reject today's occurrence if event time has passed
                if occurrence_date == hotel_today and not event.is_all_day:
                    event_time = event.event_start.astimezone(hotel_tz).time()
                    if hotel_now.time() > event_time:
                        raise serializers.ValidationError(
                            {'occurrence_date': "Today's occurrence has already passed."}
                        )
                if not event.is_valid_occurrence(occurrence_date):
                    raise serializers.ValidationError(
                        {'occurrence_date': 'This date does not match the event schedule.'}
                    )

            # --- Booking window enforcement ---
            if event.is_recurring and occurrence_date:
                target_dt = event.resolve_target_datetime(occurrence_date=occurrence_date)
            else:
                target_dt = event.resolve_target_datetime()

            if target_dt is not None and not event.is_bookable_for(target_dt):
                import zoneinfo
                hotel_tz = zoneinfo.ZoneInfo(event.hotel.timezone or 'UTC')
                opens_at, closes_at = event.get_booking_window_for(target_dt)
                bw_now = timezone.now()
                if opens_at is not None and bw_now < opens_at:
                    opens_local = opens_at.astimezone(hotel_tz)
                    raise serializers.ValidationError(
                        {'event': f"Bookings for this event open on {opens_local.strftime('%b %d at %I:%M %p')}."}
                    )
                closes_local = closes_at.astimezone(hotel_tz)
                raise serializers.ValidationError(
                    {'event': f"Bookings for this event closed on {closes_local.strftime('%b %d at %I:%M %p')}."}
                )

            if not event.is_recurring:
                # Strip occurrence_date for non-recurring events
                data.pop('occurrence_date', None)
        else:
            # --- Non-event request (existing logic) ---
            data.pop('occurrence_date', None)
            data.pop('event', None)

            if experience:
                if experience.department.hotel != hotel:
                    raise serializers.ValidationError(
                        {'experience': 'Experience does not belong to this hotel.'}
                    )
                derived_dept = experience.department
                if derived_dept.is_ops:
                    raise serializers.ValidationError(
                        {'experience': 'This experience is not available for guest requests.'}
                    )
                if department and department != derived_dept:
                    raise serializers.ValidationError(
                        {'department': 'Department does not match the experience.'}
                    )
                data['department'] = derived_dept
            elif department:
                if department.hotel != hotel:
                    raise serializers.ValidationError(
                        {'department': 'Department does not belong to this hotel.'}
                    )
                if department.is_ops:
                    raise serializers.ValidationError(
                        {'department': 'This department is not available for guest requests.'}
                    )
            else:
                raise serializers.ValidationError(
                    {'department': 'Either experience, event, or department is required.'}
                )

        return data


class ServiceRequestListSerializer(serializers.ModelSerializer):
    guest_name = serializers.SerializerMethodField()
    room_number = serializers.CharField(source='guest_stay.room_number', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True)
    experience_name = serializers.SerializerMethodField()
    event_name = serializers.SerializerMethodField()

    class Meta:
        model = ServiceRequest
        fields = [
            'id', 'public_id', 'status', 'request_type',
            'guest_name', 'room_number', 'department_name',
            'experience_name', 'event_name', 'guest_notes', 'guest_date',
            'guest_time', 'guest_count', 'after_hours',
            'response_due_at', 'created_at', 'acknowledged_at',
            'confirmed_at',
        ]
        # confirmation_token explicitly excluded

    def get_guest_name(self, obj):
        user = obj.guest_stay.guest
        return f'{user.first_name} {user.last_name}'.strip()

    def get_experience_name(self, obj):
        return obj.experience.name if obj.experience else None

    def get_event_name(self, obj):
        return obj.event.name if obj.event else None


class ServiceRequestDetailSerializer(ServiceRequestListSerializer):
    activities = RequestActivitySerializer(many=True, read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    assigned_to_id = serializers.IntegerField(source='assigned_to.id', read_only=True, default=None)
    guest_stay_id = serializers.IntegerField(source='guest_stay.id', read_only=True)
    event_id = serializers.IntegerField(source='event.id', read_only=True, default=None)
    occurrence_date = serializers.DateField(read_only=True)

    class Meta(ServiceRequestListSerializer.Meta):
        fields = ServiceRequestListSerializer.Meta.fields + [
            'staff_notes', 'confirmation_reason', 'activities',
            'assigned_to_name', 'assigned_to_id', 'guest_stay_id',
            'event_id', 'occurrence_date',
        ]

    def get_assigned_to_name(self, obj):
        if not obj.assigned_to:
            return None
        return f'{obj.assigned_to.first_name} {obj.assigned_to.last_name}'.strip() or obj.assigned_to.email


ALLOWED_CONFIRMATION_REASONS = {
    'CONFIRMED': {'WALK_IN'},
    'NOT_AVAILABLE': {'SOLD_OUT', 'MAINTENANCE', 'SEASONAL'},
    'NO_SHOW': {'GUEST_UNREACHABLE'},
    'ALREADY_BOOKED_OFFLINE': {'WALK_IN'},
}


class ServiceRequestUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceRequest
        fields = ['status', 'staff_notes', 'confirmation_reason']

    def validate_status(self, value):
        current = self.instance.status
        valid = ServiceRequest.VALID_TRANSITIONS.get(current, [])
        if value not in valid:
            raise serializers.ValidationError(
                f'Cannot transition from {current} to {value}. '
                f'Valid transitions: {valid}'
            )
        return value

    def validate(self, data):
        status = data.get('status')
        reason = data.get('confirmation_reason', '')

        # Reject confirmation_reason without a status transition
        if reason and not status:
            raise serializers.ValidationError(
                {'confirmation_reason': 'Cannot set reason without a status transition.'}
            )

        if status and status not in ServiceRequest.TERMINAL_STATUSES and reason:
            raise serializers.ValidationError(
                {'confirmation_reason': 'Reason must be blank for non-terminal states.'}
            )

        if reason and status:
            allowed = ALLOWED_CONFIRMATION_REASONS.get(status, set())
            if reason not in allowed:
                raise serializers.ValidationError(
                    {'confirmation_reason': f'Invalid reason for status {status}. Allowed: {allowed}'}
                )
            if reason == status:
                raise serializers.ValidationError(
                    {'confirmation_reason': 'Reason must not duplicate the status value.'}
                )

        return data


# ---------------------------------------------------------------------------
# Notification / Push serializers
# ---------------------------------------------------------------------------

class NotificationSerializer(serializers.ModelSerializer):
    request_public_id = serializers.UUIDField(
        source='request.public_id', read_only=True, default=None,
    )

    class Meta:
        model = Notification
        fields = [
            'id', 'title', 'body', 'notification_type',
            'is_read', 'created_at', 'request_public_id',
        ]


class PushSubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PushSubscription
        fields = ['id', 'subscription_info', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate_subscription_info(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('Must be a JSON object.')
        if not value.get('endpoint'):
            raise serializers.ValidationError('Missing required key "endpoint".')
        keys = value.get('keys')
        if not isinstance(keys, dict) or not keys.get('p256dh') or not keys.get('auth'):
            raise serializers.ValidationError('Missing required "keys.p256dh" and "keys.auth".')
        return value


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

class SetupFlagsSerializer(serializers.Serializer):
    settings_configured = serializers.BooleanField()
    has_departments = serializers.BooleanField()
    has_experiences = serializers.BooleanField()
    has_photos = serializers.BooleanField()
    has_team = serializers.BooleanField()
    has_qr_codes = serializers.BooleanField()
    has_published = serializers.BooleanField()


class DashboardStatsSerializer(serializers.Serializer):
    total_requests = serializers.IntegerField()
    pending = serializers.IntegerField()
    acknowledged = serializers.IntegerField()
    confirmed = serializers.IntegerField()
    conversion_rate = serializers.FloatField()
    by_department = serializers.ListField()
    setup = SetupFlagsSerializer()
