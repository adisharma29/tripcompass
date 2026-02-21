from django.contrib import admin
from django.utils import timezone

from .models import (
    Hotel, HotelMembership, Department, Experience,
    GuestStay, OTPCode, ServiceRequest, RequestActivity,
    Notification, PushSubscription, QRCode, EscalationHeartbeat,
)


@admin.register(Hotel)
class HotelAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'is_active', 'escalation_enabled', 'created_at']
    list_filter = ['is_active', 'escalation_enabled']
    search_fields = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(HotelMembership)
class HotelMembershipAdmin(admin.ModelAdmin):
    list_display = ['user', 'hotel', 'role', 'department', 'is_active']
    list_filter = ['role', 'is_active', 'hotel']
    search_fields = ['user__email', 'user__phone']


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ['name', 'hotel', 'is_ops', 'status', 'is_active', 'display_order']
    list_filter = ['hotel', 'is_ops', 'status', 'is_active']
    search_fields = ['name']


@admin.register(Experience)
class ExperienceAdmin(admin.ModelAdmin):
    list_display = ['name', 'department', 'category', 'status', 'is_active', 'display_order']
    list_filter = ['category', 'status', 'is_active', 'department__hotel']
    search_fields = ['name']


@admin.register(GuestStay)
class GuestStayAdmin(admin.ModelAdmin):
    list_display = ['guest', 'hotel', 'room_number', 'is_active', 'created_at', 'expires_at']
    list_filter = ['is_active', 'hotel']
    search_fields = ['guest__phone', 'room_number']


@admin.register(OTPCode)
class OTPCodeAdmin(admin.ModelAdmin):
    list_display = ['phone', 'channel', 'is_used', 'attempts', 'created_at', 'expires_at']
    list_filter = ['channel', 'is_used']
    search_fields = ['phone']
    readonly_fields = ['code_hash', 'ip_hash']


@admin.register(ServiceRequest)
class ServiceRequestAdmin(admin.ModelAdmin):
    list_display = ['public_id', 'hotel', 'status', 'department', 'created_at']
    list_filter = ['status', 'hotel', 'department']
    search_fields = ['public_id']
    readonly_fields = ['public_id', 'confirmation_token']
    # confirmation_token is hidden from list_display/export but visible in detail for debugging
    exclude = []  # confirmation_token visible in admin detail only


@admin.register(RequestActivity)
class RequestActivityAdmin(admin.ModelAdmin):
    list_display = ['request', 'action', 'actor', 'escalation_tier', 'created_at']
    list_filter = ['action']
    readonly_fields = ['request', 'actor', 'action', 'details', 'created_at']


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['user', 'hotel', 'notification_type', 'is_read', 'created_at']
    list_filter = ['notification_type', 'is_read', 'hotel']


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ['user', 'is_active', 'created_at']
    list_filter = ['is_active']


@admin.register(QRCode)
class QRCodeAdmin(admin.ModelAdmin):
    list_display = ['code', 'hotel', 'placement', 'label', 'is_active', 'created_at']
    list_filter = ['placement', 'is_active', 'hotel']
    search_fields = ['code', 'label']
    readonly_fields = ['code']


@admin.register(EscalationHeartbeat)
class EscalationHeartbeatAdmin(admin.ModelAdmin):
    list_display = ['task_name', 'status', 'last_run', 'is_healthy']
    readonly_fields = ['task_name', 'last_run', 'status', 'details']

    @admin.display(boolean=True, description='Healthy')
    def is_healthy(self, obj):
        if not obj.last_run:
            return False
        age = (timezone.now() - obj.last_run).total_seconds()
        return age < 600 and obj.status == 'OK'  # 10 min threshold
