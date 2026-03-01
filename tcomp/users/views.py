from django.conf import settings
from django.contrib.auth import get_user_model
from django.middleware.csrf import get_token
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import (
    AuthProfileSerializer, AuthProfileUpdateSerializer,
    EmailOTPSendSerializer, EmailOTPVerifySerializer,
    ForgotPasswordSerializer, SetPasswordSerializer,
    OTPSendSerializer, OTPVerifySerializer,
    UserSerializer,
)

User = get_user_model()


def _set_auth_cookies(response, user, *, update_last_login=False):
    """Set httpOnly JWT cookies on the response."""
    if update_last_login:
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])
    refresh = RefreshToken.for_user(user)
    access_token = str(refresh.access_token)
    refresh_token = str(refresh)

    jwt_settings = settings.SIMPLE_JWT

    response.set_cookie(
        key=jwt_settings.get('AUTH_COOKIE', 'access_token'),
        value=access_token,
        httponly=jwt_settings.get('AUTH_COOKIE_HTTP_ONLY', True),
        secure=jwt_settings.get('AUTH_COOKIE_SECURE', True),
        samesite=jwt_settings.get('AUTH_COOKIE_SAMESITE', 'Lax'),
        path=jwt_settings.get('AUTH_COOKIE_PATH', '/'),
        domain=jwt_settings.get('AUTH_COOKIE_DOMAIN'),
        max_age=int(jwt_settings['ACCESS_TOKEN_LIFETIME'].total_seconds()),
    )
    response.set_cookie(
        key=jwt_settings.get('REFRESH_COOKIE', 'refresh_token'),
        value=refresh_token,
        httponly=True,
        secure=jwt_settings.get('AUTH_COOKIE_SECURE', True),
        samesite=jwt_settings.get('AUTH_COOKIE_SAMESITE', 'Lax'),
        path=jwt_settings.get('REFRESH_COOKIE_PATH', '/api/v1/auth/token/refresh/'),
        domain=jwt_settings.get('AUTH_COOKIE_DOMAIN'),
        max_age=int(jwt_settings['REFRESH_TOKEN_LIFETIME'].total_seconds()),
    )

    return response


def _clear_auth_cookies(response):
    """Clear auth cookies."""
    jwt_settings = settings.SIMPLE_JWT
    domain = jwt_settings.get('AUTH_COOKIE_DOMAIN')

    response.delete_cookie(
        jwt_settings.get('AUTH_COOKIE', 'access_token'),
        path=jwt_settings.get('AUTH_COOKIE_PATH', '/'),
        domain=domain,
    )
    response.delete_cookie(
        jwt_settings.get('REFRESH_COOKIE', 'refresh_token'),
        path=jwt_settings.get('REFRESH_COOKIE_PATH', '/api/v1/auth/token/refresh/'),
        domain=domain,
    )
    return response


class CSRFTokenView(APIView):
    """Sets the csrftoken cookie (non-httpOnly, readable by JS for double-submit)."""
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        get_token(request)  # Forces Django to set the CSRF cookie
        return Response({'detail': 'CSRF cookie set.'})


class CookieTokenObtainPairView(APIView):
    """Staff login via email + password. Sets httpOnly JWT cookies."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip()
        password = request.data.get('password', '')

        if not email:
            return Response(
                {'detail': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            user = User.objects.get(email__iexact=email)
        except User.MultipleObjectsReturned:
            user = User.objects.filter(email__iexact=email).order_by('date_joined').first()
        except User.DoesNotExist:
            return Response(
                {'detail': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.check_password(password):
            return Response(
                {'detail': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            return Response(
                {'detail': 'Account is disabled.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        data = AuthProfileSerializer(user).data
        response = Response(data)
        return _set_auth_cookies(response, user, update_last_login=True)


class CookieTokenRefreshView(APIView):
    """Refresh access token using refresh cookie. Rotates both cookies."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        refresh_cookie = request.COOKIES.get(
            settings.SIMPLE_JWT.get('REFRESH_COOKIE', 'refresh_token')
        )
        if not refresh_cookie:
            return Response(
                {'detail': 'No refresh token.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            refresh = RefreshToken(refresh_cookie)
            user_id = refresh.payload.get('user_id')
            user = User.objects.get(id=user_id)
        except Exception:
            return Response(
                {'detail': 'Invalid or expired refresh token.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            return Response(
                {'detail': 'Account is disabled.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Blacklist old refresh token (rotation)
        refresh.blacklist()

        response = Response({'detail': 'Token refreshed.'})
        return _set_auth_cookies(response, user)


class LogoutView(APIView):
    """Blacklists refresh token and clears auth cookies."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        refresh_cookie = request.COOKIES.get(
            settings.SIMPLE_JWT.get('REFRESH_COOKIE', 'refresh_token')
        )
        if refresh_cookie:
            try:
                token = RefreshToken(refresh_cookie)
                token.blacklist()
            except Exception:
                pass  # already expired or blacklisted

        response = Response({'detail': 'Logged out.'})
        return _clear_auth_cookies(response)


class OTPSendView(APIView):
    """Phone login step 1. Sends OTP via WhatsApp (primary) + SMS (fallback)."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = OTPSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data['phone']
        hotel_slug = serializer.validated_data.get('hotel_slug', '')

        # Rate limiting (per phone, with DB fallback)
        from concierge.services import check_otp_rate_limit_phone, check_otp_rate_limit_ip, _hash_ip
        if not check_otp_rate_limit_phone(phone, settings.OTP_SEND_RATE_PER_PHONE, 3600):
            return Response(
                {'detail': 'Too many OTP requests. Try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Rate limiting (per IP, with DB fallback)
        # Use REMOTE_ADDR (set by the trusted reverse proxy) rather than
        # X-Forwarded-For which can be spoofed by the client.
        ip = request.META.get('REMOTE_ADDR', '')
        if ip:
            ip_hash = _hash_ip(ip)
            if not check_otp_rate_limit_ip(ip_hash, settings.OTP_SEND_RATE_PER_IP, 3600):
                return Response(
                    {'detail': 'Too many OTP requests. Try again later.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

        # Resolve hotel if provided
        hotel = None
        if hotel_slug:
            from concierge.models import Hotel
            try:
                hotel = Hotel.objects.get(slug=hotel_slug, is_active=True)
            except Hotel.DoesNotExist:
                return Response(
                    {'detail': 'Hotel not found.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        from concierge.services import send_otp, OTPDeliveryError
        try:
            send_otp(phone, ip_address=ip, hotel=hotel)
        except OTPDeliveryError:
            return Response(
                {'detail': 'Unable to send OTP. Please try again later.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({'detail': 'OTP sent.'})


class OTPVerifyView(APIView):
    """Phone login step 2. Verifies OTP, branches on user type."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data['phone']
        code = serializer.validated_data['code']
        hotel_slug = serializer.validated_data.get('hotel_slug', '')
        qr_code_str = serializer.validated_data.get('qr_code', '')

        # Resolve hotel
        hotel = None
        if hotel_slug:
            from concierge.models import Hotel
            try:
                hotel = Hotel.objects.get(slug=hotel_slug, is_active=True)
            except Hotel.DoesNotExist:
                return Response(
                    {'detail': 'Hotel not found.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        from concierge.services import verify_otp
        from rest_framework.exceptions import ValidationError as DRFValidationError
        from django.core.exceptions import ValidationError as DjangoValidationError
        try:
            user, stay = verify_otp(
                phone, code,
                hotel=hotel,
                qr_code_str=qr_code_str or None,
            )
        except (DRFValidationError, DjangoValidationError) as e:
            detail = e.detail if hasattr(e, 'detail') else e.messages
            return Response(
                {'detail': detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            return Response(
                {'detail': 'OTP verification failed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = AuthProfileSerializer(user).data
        if stay:
            data['stay_id'] = stay.id
            data['stay_room_number'] = stay.room_number or ''
            data['stay_expires_at'] = stay.expires_at.isoformat()

        response = Response(data)
        return _set_auth_cookies(response, user, update_last_login=True)


class EmailOTPSendView(APIView):
    """Email login step 1. Sends OTP code via email."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = EmailOTPSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email'].lower()

        from concierge.services import check_otp_rate_limit_email, check_otp_rate_limit_ip, _hash_ip
        if not check_otp_rate_limit_email(email, settings.OTP_SEND_RATE_PER_EMAIL, 3600):
            return Response(
                {'detail': 'Too many requests. Try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        ip = request.META.get('REMOTE_ADDR', '')
        if ip:
            ip_hash = _hash_ip(ip)
            if not check_otp_rate_limit_ip(ip_hash, settings.OTP_SEND_RATE_PER_IP, 3600):
                return Response(
                    {'detail': 'Too many requests. Try again later.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

        from concierge.services import send_email_otp, OTPDeliveryError
        try:
            send_email_otp(email, ip_address=ip)
        except OTPDeliveryError:
            return Response(
                {'detail': 'Unable to send verification code. Please try again later.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Always return generic message to prevent email enumeration
        return Response({'detail': 'If this email is registered, a verification code has been sent.'})


class EmailOTPVerifyView(APIView):
    """Email login step 2. Verifies email OTP code, sets auth cookies."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = EmailOTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email'].lower()
        code = serializer.validated_data['code']

        from concierge.services import verify_email_otp
        from rest_framework.exceptions import ValidationError as DRFValidationError
        from django.core.exceptions import ValidationError as DjangoValidationError
        try:
            user = verify_email_otp(email, code)
        except (DRFValidationError, DjangoValidationError) as e:
            detail = e.detail if hasattr(e, 'detail') else e.messages
            return Response(
                {'detail': detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            return Response(
                {'detail': 'Verification failed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = AuthProfileSerializer(user).data
        response = Response(data)
        return _set_auth_cookies(response, user, update_last_login=True)


class SetPasswordView(APIView):
    """Set password using a signed token (from invite or reset email)."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        from concierge.services import verify_password_token
        user = verify_password_token(
            serializer.validated_data['uid'],
            serializer.validated_data['token'],
        )
        if not user:
            return Response(
                {'detail': 'This link has expired or already been used.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not user.is_active:
            return Response(
                {'detail': 'Account is disabled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(serializer.validated_data['password'])
        user.save()

        data = AuthProfileSerializer(user).data
        response = Response(data)
        return _set_auth_cookies(response, user, update_last_login=True)


class ForgotPasswordView(APIView):
    """Request a password reset email."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email'].lower()

        # Rate limit by email
        from concierge.services import check_otp_rate_limit_email, check_otp_rate_limit_ip, _hash_ip
        if not check_otp_rate_limit_email(email, settings.OTP_SEND_RATE_PER_EMAIL, 3600):
            return Response(
                {'detail': 'Too many requests. Try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        ip = request.META.get('REMOTE_ADDR', '')
        if ip:
            ip_hash = _hash_ip(ip)
            if not check_otp_rate_limit_ip(ip_hash, settings.OTP_SEND_RATE_PER_IP, 3600):
                return Response(
                    {'detail': 'Too many requests. Try again later.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

        # Create a tracking row so DB-fallback rate limiting works when Redis is down.
        from concierge.models import OTPCode
        from concierge.services import _hash_ip
        OTPCode.objects.create(
            email=email,
            code_hash='',
            channel=OTPCode.Channel.EMAIL,
            ip_hash=_hash_ip(ip) if ip else '',
            is_used=True,   # not a real OTP — just a counter row
            expires_at=timezone.now(),
        )

        # Look up staff user — send email if found, silent no-op if not
        user = User.objects.filter(email__iexact=email, user_type='STAFF', is_active=True).order_by('date_joined').first()
        if user:
            from concierge.services import send_password_reset_email
            send_password_reset_email(user)

        # Always return generic message to prevent email enumeration
        return Response({'detail': 'If this email is registered, a reset link has been sent.'})


class AuthProfileView(generics.RetrieveUpdateAPIView):
    """GET: returns current user + memberships/stays.
    PATCH: updates phone, first_name, last_name."""
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method == 'PATCH':
            return AuthProfileUpdateSerializer
        return AuthProfileSerializer

    def get_object(self):
        return self.request.user


# ---------------------------------------------------------------------------
# WhatsApp Invite — Verify endpoint (plain Django view, not DRF)
# ---------------------------------------------------------------------------

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from concierge.models import GuestInvite, GuestStay
from concierge.services import verify_invite_token

logger = logging.getLogger(__name__)


def _render_error(request, message, action_url=None, action_label=None):
    return render(request, 'users/wa_invite_error.html', {
        'message': message,
        'action_url': action_url,
        'action_label': action_label or 'Continue',
    })


def _validate_invite_status(invite, version, write=False):
    """Check version, hotel, status, expiry. Returns None if valid, or error context dict."""
    if invite.token_version != version:
        return 'version_mismatch'
    if not invite.hotel.is_active:
        return 'hotel_inactive'
    if invite.status == 'USED':
        return 'already_used'
    if invite.status == 'EXPIRED':
        return 'revoked'
    if invite.expires_at < timezone.now():
        if write:
            invite.status = 'EXPIRED'
            invite.save(update_fields=['status'])
        return 'expired'
    return None


@require_http_methods(['GET', 'POST'])
def verify_wa_invite(request, token):
    """Two-step WhatsApp invite verification.

    GET:  Read-only validation + confirm page (safe for link scanners).
    POST: Performs login — creates stay, sets JWT cookies, redirects.
    """
    from django.core.signing import BadSignature

    # 1. Verify signature (shared by both GET and POST)
    try:
        invite_id, version = verify_invite_token(token)
    except BadSignature:
        return _render_error(request, "This link is invalid. Please ask the hotel for a new invite.")

    # 2. GET — read-only validation + confirm page
    if request.method == 'GET':
        try:
            invite = GuestInvite.objects.select_related('hotel').get(id=invite_id)
        except GuestInvite.DoesNotExist:
            return _render_error(request, "This link is invalid.")

        error = _validate_invite_status(invite, version, write=False)
        if error:
            return _render_invite_error(request, error, invite)

        return render(request, 'users/wa_invite_confirm.html', {
            'hotel_name': invite.hotel.name,
            'guest_name': invite.guest_name,
            'token': token,
        })

    # 3. POST — perform login (all state changes happen here only)
    with transaction.atomic():
        try:
            invite = (
                GuestInvite.objects
                .select_for_update()
                .select_related('hotel')
                .get(id=invite_id)
            )
        except GuestInvite.DoesNotExist:
            return _render_error(request, "This link is invalid.")

        error = _validate_invite_status(invite, version, write=True)
        if error:
            return _render_invite_error(request, error, invite)

        # Staff phone conflict
        phone = invite.guest_phone
        existing = (
            User.objects.filter(phone=phone).first()
            or User.objects.filter(phone=f'+{phone}').first()
        )
        if existing and existing.user_type != 'GUEST':
            return _render_error(
                request,
                "This phone number is registered as a staff account.",
                action_url=f'{settings.FRONTEND_ORIGIN}/login',
                action_label="Log in",
            )

        # Disabled guest check
        if existing and existing.user_type == 'GUEST' and not existing.is_active:
            return _render_error(request, "This account has been disabled. Please contact the hotel.")

        # Find or create guest user
        if existing and existing.user_type == 'GUEST':
            user = existing
            if not user.first_name and invite.guest_name:
                parts = invite.guest_name.split(maxsplit=1)
                user.first_name = parts[0]
                user.last_name = parts[1] if len(parts) > 1 else ''
                user.save(update_fields=['first_name', 'last_name'])
        else:
            parts = invite.guest_name.split(maxsplit=1)
            try:
                user = User.objects.create_guest_user(
                    phone=phone,
                    first_name=parts[0],
                    last_name=parts[1] if len(parts) > 1 else '',
                )
            except IntegrityError:
                user = (
                    User.objects.filter(phone=phone, user_type='GUEST').first()
                    or User.objects.filter(phone=f'+{phone}', user_type='GUEST').first()
                )
                if not user:
                    return _render_error(
                        request,
                        "This phone number is registered as a staff account.",
                        action_url=f'{settings.FRONTEND_ORIGIN}/login',
                        action_label="Log in",
                    )

        # Reuse active stay or create new
        new_expiry = timezone.now() + timedelta(hours=24)
        existing_stay = (
            GuestStay.objects
            .select_for_update()
            .filter(guest=user, hotel=invite.hotel, is_active=True)
            .order_by('-created_at')
            .first()
        )
        if existing_stay:
            existing_stay.expires_at = new_expiry
            update_fields = ['expires_at']
            if invite.room_number and not existing_stay.room_number:
                existing_stay.room_number = invite.room_number
                update_fields.append('room_number')
            existing_stay.save(update_fields=update_fields)
            stay = existing_stay
        else:
            stay = GuestStay.objects.create(
                guest=user,
                hotel=invite.hotel,
                room_number=invite.room_number,
                is_active=True,
                expires_at=new_expiry,
            )

        # Mark invite used
        invite.status = 'USED'
        invite.used_at = timezone.now()
        invite.guest_user = user
        invite.guest_stay = stay
        invite.save(update_fields=['status', 'used_at', 'guest_user', 'guest_stay'])

    # Issue JWT + redirect (outside atomic — cookies don't need rollback)
    redirect_url = f'{settings.FRONTEND_ORIGIN}/h/{invite.hotel.slug}/'
    response = HttpResponseRedirect(redirect_url)
    _set_auth_cookies(response, user, update_last_login=True)
    return response


def _render_invite_error(request, error_type, invite):
    """Map validation error type to user-facing error page."""
    if error_type == 'version_mismatch':
        return _render_error(request, "This link is no longer valid. A newer invite was sent.")
    if error_type == 'hotel_inactive':
        return _render_error(request, "This hotel is no longer available on our platform.")
    if error_type == 'already_used':
        return _render_error(
            request,
            "You've already checked in!",
            action_url=f'{settings.FRONTEND_ORIGIN}/h/{invite.hotel.slug}/',
            action_label="Open Concierge",
        )
    if error_type == 'revoked':
        return _render_error(request, "This invitation was cancelled. Please contact the hotel.")
    if error_type == 'expired':
        return _render_error(request, "This link has expired. Ask the hotel front desk to resend.")
    return _render_error(request, "Something went wrong. Please try again.")
