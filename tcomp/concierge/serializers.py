import re

import bleach
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Hotel, HotelMembership, Department, Experience, ExperienceImage,
    GuestStay, ServiceRequest, RequestActivity,
    Notification, PushSubscription, QRCode,
)

ALLOWED_HTML_TAGS = [
    'p', 'br', 'strong', 'em', 's', 'h2', 'h3',
    'ul', 'ol', 'li', 'blockquote', 'hr',
]


# ---------------------------------------------------------------------------
# Image serializer (used by both public and admin)
# ---------------------------------------------------------------------------

class ExperienceImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExperienceImage
        fields = ['id', 'image', 'alt_text', 'display_order', 'created_at']
        read_only_fields = ['id', 'created_at']


# ---------------------------------------------------------------------------
# Public serializers
# ---------------------------------------------------------------------------

class ExperiencePublicSerializer(serializers.ModelSerializer):
    gallery_images = ExperienceImageSerializer(many=True, read_only=True)

    class Meta:
        model = Experience
        fields = [
            'id', 'name', 'slug', 'description', 'photo', 'cover_image',
            'price_display', 'category', 'timing', 'duration', 'capacity',
            'highlights', 'display_order', 'gallery_images',
        ]


class DepartmentPublicSerializer(serializers.ModelSerializer):
    experiences = ExperiencePublicSerializer(many=True, read_only=True)

    class Meta:
        model = Department
        fields = [
            'id', 'name', 'slug', 'description', 'photo', 'icon',
            'display_order', 'schedule', 'is_ops', 'experiences',
        ]


class HotelPublicSerializer(serializers.ModelSerializer):
    departments = DepartmentPublicSerializer(many=True, read_only=True)

    class Meta:
        model = Hotel
        fields = [
            'id', 'name', 'slug', 'description', 'tagline',
            'logo', 'cover_image', 'timezone', 'departments',
        ]


# ---------------------------------------------------------------------------
# Admin serializers
# ---------------------------------------------------------------------------

class HotelSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hotel
        fields = [
            'timezone', 'room_number_pattern', 'blocked_room_numbers',
            'room_number_min', 'room_number_max',
            'escalation_enabled', 'escalation_fallback_channel',
            'oncall_email', 'oncall_phone', 'require_frontdesk_kiosk',
            'escalation_tier_minutes',
        ]

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
        return data


class DepartmentSerializer(serializers.ModelSerializer):
    experiences = ExperiencePublicSerializer(many=True, read_only=True)

    class Meta:
        model = Department
        fields = [
            'id', 'name', 'slug', 'description', 'photo', 'icon',
            'display_order', 'schedule', 'is_ops', 'is_active',
            'experiences', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'created_at', 'updated_at']

    def validate_description(self, value):
        if value:
            return bleach.clean(value, tags=ALLOWED_HTML_TAGS, attributes={}, strip=True)
        return value


class ExperienceSerializer(serializers.ModelSerializer):
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(),
    )
    gallery_images = ExperienceImageSerializer(many=True, read_only=True)

    class Meta:
        model = Experience
        fields = [
            'id', 'department', 'name', 'slug', 'description',
            'photo', 'cover_image', 'price', 'price_display',
            'category', 'timing', 'duration', 'capacity',
            'highlights', 'is_active', 'display_order',
            'gallery_images', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'created_at', 'updated_at']

    def validate_department(self, value):
        hotel = self.context.get('hotel')
        if hotel and value.hotel != hotel:
            raise serializers.ValidationError(
                'Department does not belong to this hotel.'
            )
        return value

    def validate_description(self, value):
        if value:
            return bleach.clean(value, tags=ALLOWED_HTML_TAGS, attributes={}, strip=True)
        return value


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

    class Meta:
        model = ServiceRequest
        fields = [
            'experience', 'department', 'request_type',
            'guest_name', 'guest_notes', 'guest_date',
            'guest_time', 'guest_count',
        ]

    def validate(self, data):
        hotel = self.context.get('hotel')
        experience = data.get('experience')
        department = data.get('department')

        if experience:
            # Validate experience belongs to this hotel
            if experience.department.hotel != hotel:
                raise serializers.ValidationError(
                    {'experience': 'Experience does not belong to this hotel.'}
                )
            # Derive department from experience
            derived_dept = experience.department
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
        else:
            raise serializers.ValidationError(
                {'department': 'Either experience or department is required.'}
            )

        return data


class ServiceRequestListSerializer(serializers.ModelSerializer):
    guest_name = serializers.SerializerMethodField()
    room_number = serializers.CharField(source='guest_stay.room_number', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True)
    experience_name = serializers.SerializerMethodField()

    class Meta:
        model = ServiceRequest
        fields = [
            'id', 'public_id', 'status', 'request_type',
            'guest_name', 'room_number', 'department_name',
            'experience_name', 'guest_notes', 'guest_date',
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


class ServiceRequestDetailSerializer(ServiceRequestListSerializer):
    activities = RequestActivitySerializer(many=True, read_only=True)

    class Meta(ServiceRequestListSerializer.Meta):
        fields = ServiceRequestListSerializer.Meta.fields + [
            'staff_notes', 'confirmation_reason', 'activities',
        ]


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


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

class DashboardStatsSerializer(serializers.Serializer):
    total_requests = serializers.IntegerField()
    pending = serializers.IntegerField()
    acknowledged = serializers.IntegerField()
    confirmed = serializers.IntegerField()
    conversion_rate = serializers.FloatField()
    by_department = serializers.ListField()
