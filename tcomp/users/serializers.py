import re

from django.db import models
from rest_framework import serializers
from django.contrib.auth import get_user_model

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ['email', 'password', 'first_name', 'last_name']

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'phone', 'avatar', 'bio']
        read_only_fields = ['id', 'email']


class UserMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'phone']
        read_only_fields = fields


class AuthProfileSerializer(serializers.ModelSerializer):
    """User profile + hotel memberships (staff) or stays (guest)."""
    memberships = serializers.SerializerMethodField()
    stays = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'phone',
            'avatar', 'user_type', 'memberships', 'stays',
        ]
        read_only_fields = fields

    def get_memberships(self, obj):
        if obj.user_type != 'STAFF':
            return []
        from concierge.serializers import MemberSerializer
        memberships = obj.hotel_memberships.filter(
            is_active=True,
        ).select_related('hotel', 'department')
        return MemberSerializer(memberships, many=True).data

    def get_stays(self, obj):
        if obj.user_type != 'GUEST':
            return []
        from concierge.serializers import GuestStaySerializer
        stays = obj.stays.order_by('-created_at')[:5]
        return GuestStaySerializer(stays, many=True).data


class AuthProfileUpdateSerializer(serializers.ModelSerializer):
    """Allows authenticated user to update phone, first_name, last_name."""

    class Meta:
        model = User
        fields = ['phone', 'first_name', 'last_name']

    def validate_phone(self, value):
        if not value:
            return value
        digits = re.sub(r'\D', '', value)
        if not (11 <= len(digits) <= 15):
            raise serializers.ValidationError(
                'Enter 11-15 digits including country code.'
            )
        # Check uniqueness against normalized value (+ pre-backfill +prefixed)
        user = self.instance
        qs = User.objects.filter(
            models.Q(phone=digits) | models.Q(phone=f'+{digits}')
        ).exclude(pk=user.pk).exclude(phone='')
        if qs.exists():
            raise serializers.ValidationError('This phone number is already in use.')
        return digits


class OTPSendSerializer(serializers.Serializer):
    phone = serializers.CharField(max_length=20)
    hotel_slug = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_phone(self, value):
        digits = re.sub(r'\D', '', value)
        if not (11 <= len(digits) <= 15):
            raise serializers.ValidationError(
                'Enter 11-15 digits including country code.'
            )
        return digits


class OTPVerifySerializer(serializers.Serializer):
    phone = serializers.CharField(max_length=20)
    code = serializers.CharField(max_length=10, write_only=True)
    hotel_slug = serializers.CharField(required=False, allow_blank=True, default='')
    qr_code = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_phone(self, value):
        digits = re.sub(r'\D', '', value)
        if not (11 <= len(digits) <= 15):
            raise serializers.ValidationError(
                'Enter 11-15 digits including country code.'
            )
        return digits
