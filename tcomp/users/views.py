from django.conf import settings
from django.contrib.auth import get_user_model
from django.middleware.csrf import get_token
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import (
    AuthProfileSerializer, AuthProfileUpdateSerializer,
    OTPSendSerializer, OTPVerifySerializer,
    UserSerializer,
)

User = get_user_model()


def _set_auth_cookies(response, user):
    """Set httpOnly JWT cookies on the response."""
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
            user = User.objects.get(email=email)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
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
        return _set_auth_cookies(response, user)


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
        return _set_auth_cookies(response, user)


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
