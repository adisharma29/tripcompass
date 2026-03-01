import logging
import re
import secrets
from zoneinfo import ZoneInfo

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    Hotel, HotelMembership, Department, Experience, Event,
    GuestStay, OTPCode, ServiceRequest, RequestActivity,
    Notification, PushSubscription, QRCode, EscalationHeartbeat,
    BookingEmailTemplate, SpecialRequestOffering, SpecialRequestOfferingImage,
    WhatsAppTemplate, GuestInvite, Rating, RatingPrompt,
    ContentStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hotel provisioning form & logic
# ---------------------------------------------------------------------------

class HotelProvisionForm(forms.Form):
    # Hotel fields
    name = forms.CharField(max_length=200, help_text='Hotel display name.')
    slug = forms.SlugField(
        max_length=100, required=False,
        help_text='URL slug (auto-generated from name if blank). Will auto-deduplicate if taken.',
    )
    city = forms.CharField(max_length=100, required=False)
    timezone = forms.CharField(
        max_length=50, initial='Asia/Kolkata',
        help_text='IANA timezone, e.g. Asia/Kolkata, America/New_York.',
    )

    # Owner fields
    owner_email = forms.EmailField(required=False, help_text='At least one of email or phone is required.')
    owner_phone_code = forms.ChoiceField(
        choices=[
            ('+91', 'IN +91'), ('+1', 'US +1'), ('+44', 'UK +44'),
            ('+971', 'AE +971'), ('+61', 'AU +61'), ('+49', 'DE +49'),
            ('+33', 'FR +33'), ('+81', 'JP +81'), ('+86', 'CN +86'),
            ('+65', 'SG +65'), ('+966', 'SA +966'), ('+974', 'QA +974'),
            ('+968', 'OM +968'), ('+973', 'BH +973'), ('+977', 'NP +977'),
            ('+94', 'LK +94'), ('+66', 'TH +66'), ('+62', 'ID +62'),
            ('+60', 'MY +60'), ('+82', 'KR +82'), ('+39', 'IT +39'),
            ('+34', 'ES +34'), ('+31', 'NL +31'), ('+41', 'CH +41'),
            ('+7', 'RU +7'), ('+55', 'BR +55'), ('+52', 'MX +52'),
            ('+27', 'ZA +27'), ('+254', 'KE +254'), ('+63', 'PH +63'),
        ],
        initial='+91',
        required=False,
    )
    owner_phone_number = forms.CharField(
        max_length=15, required=False,
        help_text='Local number without country code, e.g. 9876543210',
        widget=forms.TextInput(attrs={'placeholder': '9876543210'}),
    )
    owner_first_name = forms.CharField(max_length=150, required=False)
    owner_last_name = forms.CharField(max_length=150, required=False)

    # Options
    send_invite = forms.BooleanField(
        initial=True, required=False,
        help_text='Send WhatsApp + email invite to the owner.',
    )

    def clean_slug(self):
        """Validate slug format only; deduplication happens in provision_hotel()."""
        return self.cleaned_data.get('slug')

    def clean_timezone(self):
        tz = self.cleaned_data.get('timezone', '').strip()
        if not tz:
            return 'Asia/Kolkata'
        try:
            ZoneInfo(tz)
        except (KeyError, ValueError):
            raise forms.ValidationError(f'Invalid timezone: {tz}')
        return tz

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get('owner_email', '').strip()
        local = re.sub(r'\D', '', cleaned.get('owner_phone_number', '').strip())
        code = cleaned.get('owner_phone_code', '+91')

        if local:
            code_digits = code.replace('+', '')
            combined = code_digits + local
            if not (11 <= len(combined) <= 15):
                self.add_error(
                    'owner_phone_number',
                    f'Full number must be 11-15 digits (currently {len(combined)}). '
                    'Check country code and local number.',
                )
            else:
                cleaned['owner_phone'] = combined
        else:
            cleaned['owner_phone'] = ''

        if not email and not cleaned.get('owner_phone'):
            raise forms.ValidationError('At least one of owner email or phone is required.')
        return cleaned


def provision_hotel(form_data):
    """Create a fully functional hotel with fallback department and SUPERADMIN owner.

    Returns (hotel, user, membership, trace_messages).
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    trace = []

    with transaction.atomic():
        # 1. Create Hotel
        name = form_data['name']
        slug = form_data.get('slug') or slugify(name) or 'hotel'
        base_slug = slug[:100]
        slug = base_slug
        counter = 1
        while Hotel.objects.filter(slug=slug).exists():
            suffix = f'-{counter}'
            slug = f'{base_slug[:100 - len(suffix)]}{suffix}'
            counter += 1

        hotel = Hotel(
            name=name,
            slug=slug,
            city=form_data.get('city', ''),
            timezone=form_data.get('timezone', 'Asia/Kolkata'),
            settings_configured=True,
        )
        hotel.save()  # populates blocked_room_numbers
        trace.append(f'Created hotel "{hotel.name}" (slug: {hotel.slug})')

        # 2. Create "General" fallback department
        hotel_tz = form_data.get('timezone', 'Asia/Kolkata')
        dept = Department(
            hotel=hotel,
            name='General',
            icon='concierge-bell',
            is_ops=False,
            status=ContentStatus.PUBLISHED,
            schedule={
                'timezone': hotel_tz,
                'default': [['00:00', '23:59']],
            },
        )
        dept.save()  # auto-generates slug; schedule already set
        hotel.fallback_department = dept
        hotel.save(update_fields=['fallback_department'])
        trace.append(f'Created fallback department "{dept.name}" (slug: {dept.slug})')

        # 3. Find or create User
        user = None
        email = form_data.get('owner_email', '').strip()
        phone = form_data.get('owner_phone', '').strip()

        if email:
            try:
                user = User.objects.get(email__iexact=email)
                trace.append(f'Found existing user by email: {email}')
            except User.MultipleObjectsReturned:
                user = User.objects.filter(email__iexact=email).order_by('date_joined').first()
                trace.append(f'Multiple users matched email (case variants) — using oldest: {user.email}')
            except User.DoesNotExist:
                pass

        if user is None and phone:
            normalized = re.sub(r'\D', '', phone)
            try:
                user = User.objects.get(phone=normalized)
                trace.append(f'Found existing user by phone: {normalized}')
            except User.DoesNotExist:
                try:
                    user = User.objects.get(phone=f'+{normalized}')
                    trace.append(f'Found existing user by phone (+prefix): +{normalized}')
                except User.DoesNotExist:
                    pass

        if user is not None:
            changed_fields = []
            if not user.is_active:
                user.is_active = True
                changed_fields.append('is_active')
                trace.append('Reactivated inactive user')
            if user.user_type == 'GUEST':
                user.user_type = 'STAFF'
                changed_fields.append('user_type')
                trace.append('Promoted user from GUEST to STAFF')
            if not user.phone and phone:
                user.phone = phone
                changed_fields.append('phone')
            if not user.email and email:
                user.email = email
                changed_fields.append('email')
            if not user.first_name and form_data.get('owner_first_name'):
                user.first_name = form_data['owner_first_name']
                changed_fields.append('first_name')
            if not user.last_name and form_data.get('owner_last_name'):
                user.last_name = form_data['owner_last_name']
                changed_fields.append('last_name')
            # Email-only reused user with no usable password — set temp password
            # as break-glass fallback (same as new email-only user path).
            if email and not (user.phone or phone) and not user.has_usable_password():
                temp_pw = secrets.token_urlsafe(12)
                user.set_password(temp_pw)
                changed_fields.append('password')
                trace.append(f'Temp password (share only if invite email fails): {temp_pw}')
            if changed_fields:
                user.save(update_fields=changed_fields)
                trace.append(f'Updated user fields: {", ".join(changed_fields)}')
        else:
            user = User(
                email=email,
                phone=phone,
                first_name=form_data.get('owner_first_name', ''),
                last_name=form_data.get('owner_last_name', ''),
                user_type='STAFF',
            )
            if email and not phone:
                # Email-only user: set a temp password as break-glass fallback.
                # Primary onboarding is via set-password link in the invite email,
                # but if email delivery fails the admin has this to share manually.
                temp_pw = secrets.token_urlsafe(12)
                user.set_password(temp_pw)
                user.save()
                trace.append(f'Created new user: {user.email}')
                trace.append(f'Temp password (share only if invite email fails): {temp_pw}')
            else:
                user.set_unusable_password()
                user.save()
                trace.append(f'Created new user: {user.email or user.phone}')

        # 4. Create SUPERADMIN membership
        membership = HotelMembership.objects.create(
            user=user,
            hotel=hotel,
            role=HotelMembership.Role.SUPERADMIN,
        )
        trace.append(f'Created SUPERADMIN membership for {user.email or user.phone}')

    # 5. Enqueue invite (after transaction commits)
    if form_data.get('send_invite', True):
        def _enqueue_invite():
            try:
                from .tasks import send_staff_invite_notification_task
                send_staff_invite_notification_task.delay(user.id, hotel.id, 'SUPERADMIN')
                logger.info('Enqueued staff invite for user=%s hotel=%s', user.id, hotel.id)
            except Exception:
                logger.warning(
                    'Failed to enqueue staff invite notification (broker down?)',
                    exc_info=True,
                )
        transaction.on_commit(_enqueue_invite)
        trace.append('Invite notification will be sent after save (queued on commit)')
    else:
        trace.append('Invite notification skipped (send_invite=False)')

    return hotel, user, membership, trace


# ---------------------------------------------------------------------------
# Admin registrations
# ---------------------------------------------------------------------------

@admin.register(Hotel)
class HotelAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'is_active', 'escalation_enabled', 'created_at']
    list_filter = ['is_active', 'escalation_enabled']
    search_fields = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}

    def get_urls(self):
        custom_urls = [
            path(
                'provision/',
                self.admin_site.admin_view(self.provision_hotel_view),
                name='concierge_hotel_provision',
            ),
        ]
        return custom_urls + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        if self.has_add_permission(request):
            extra_context['provision_url'] = reverse('admin:concierge_hotel_provision')
        return super().changelist_view(request, extra_context=extra_context)

    def provision_hotel_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied
        if request.method == 'POST':
            form = HotelProvisionForm(request.POST)
            if form.is_valid():
                try:
                    hotel, user, membership, trace = provision_hotel(form.cleaned_data)
                    for msg in trace:
                        messages.success(request, msg)
                    if self.has_change_permission(request):
                        url = reverse('admin:concierge_hotel_change', args=[hotel.pk])
                    else:
                        url = reverse('admin:concierge_hotel_changelist')
                    return redirect(url)
                except IntegrityError as exc:
                    messages.error(request, f'Database error: {exc}')
                except Exception as exc:
                    logger.exception('Hotel provisioning failed')
                    messages.error(request, f'Provisioning failed: {exc}')
        else:
            form = HotelProvisionForm()

        context = {
            **self.admin_site.each_context(request),
            'title': 'Provision New Hotel',
            'form': form,
            'opts': self.model._meta,
            'has_view_permission': True,
        }
        return render(request, 'admin/concierge/hotel/provision.html', context)


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


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ['name', 'hotel', 'status', 'is_featured', 'event_start', 'event_end', 'is_recurring', 'display_order']
    list_filter = ['status', 'is_featured', 'is_recurring', 'auto_expire', 'hotel']
    search_fields = ['name', 'slug']
    readonly_fields = ['slug', 'is_active', 'created_at', 'updated_at']


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


@admin.register(BookingEmailTemplate)
class BookingEmailTemplateAdmin(admin.ModelAdmin):
    list_display = ['hotel', 'subject', 'updated_at']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(SpecialRequestOffering)
class SpecialRequestOfferingAdmin(admin.ModelAdmin):
    list_display = ['name', 'hotel', 'category', 'status', 'display_order']
    list_filter = ['category', 'status', 'hotel']
    search_fields = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(SpecialRequestOfferingImage)
class SpecialRequestOfferingImageAdmin(admin.ModelAdmin):
    list_display = ['offering', 'display_order', 'created_at']
    list_filter = ['offering__hotel']


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


@admin.register(WhatsAppTemplate)
class WhatsAppTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'template_type', 'scope', 'is_active', 'gupshup_template_id', 'created_at']
    list_filter = ['template_type', 'is_active', 'hotel']
    search_fields = ['name', 'gupshup_template_id']
    readonly_fields = ['created_at']
    fieldsets = [
        (None, {
            'fields': ('template_type', 'name', 'hotel', 'is_active'),
            'description': (
                'Leave <strong>Hotel</strong> empty to create a global default '
                'used by all hotels. Set a hotel to override the default for '
                'that hotel only.'
            ),
        }),
        ('Gupshup / Meta', {
            'fields': ('gupshup_template_id',),
        }),
        ('Template Content (shown in frontend preview)', {
            'fields': ('body_text', 'footer_text', 'buttons', 'variables'),
        }),
        ('Metadata', {
            'fields': ('created_at',),
        }),
    ]

    @admin.display(description='Scope')
    def scope(self, obj):
        if obj.hotel:
            return obj.hotel.name
        return '\u2728 Global default'


@admin.register(GuestInvite)
class GuestInviteAdmin(admin.ModelAdmin):
    list_display = ['guest_phone', 'guest_name', 'hotel', 'status', 'created_at', 'expires_at']
    list_filter = ['status', 'hotel']
    search_fields = ['guest_phone', 'guest_name']
    readonly_fields = ['token_version', 'created_at', 'used_at']


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ['id', 'hotel', 'guest', 'rating_type', 'score', 'created_at']
    list_filter = ['rating_type', 'score', 'hotel']
    readonly_fields = ['created_at']


@admin.register(RatingPrompt)
class RatingPromptAdmin(admin.ModelAdmin):
    list_display = ['id', 'hotel', 'guest', 'prompt_type', 'status', 'eligible_at', 'created_at']
    list_filter = ['status', 'prompt_type', 'hotel']
    readonly_fields = ['created_at']
