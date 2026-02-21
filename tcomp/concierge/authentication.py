from django.conf import settings
from django.middleware.csrf import CsrfViewMiddleware
from rest_framework import exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


class _CSRFCheck(CsrfViewMiddleware):
    """Dummy middleware to reuse Django's CSRF validation logic."""

    def _reject(self, request, reason):
        # Return the reason instead of an HttpResponse so we can raise a DRF exception
        return reason


class JWTCookieAuthentication(JWTAuthentication):
    """Reads the JWT access token from an httpOnly cookie instead of
    the Authorization header. Enforces CSRF on cookie-authenticated
    requests (same pattern as DRF SessionAuthentication).
    Falls through to header-based auth if no cookie is present
    (allows dev-only Bearer fallback).

    If the cookie contains an invalid/expired token, returns None
    instead of raising — this lets AllowAny views (login, refresh,
    OTP) work even when a stale access_token cookie is present."""

    def authenticate(self, request):
        cookie_name = getattr(settings, 'SIMPLE_JWT', {}).get('AUTH_COOKIE', 'access_token')
        raw_token = request.COOKIES.get(cookie_name)

        if raw_token is None:
            # No cookie — let the next authenticator in the chain handle it
            return None

        try:
            validated_token = self.get_validated_token(raw_token)
        except (InvalidToken, TokenError):
            # Stale/expired cookie — fall through so AllowAny views still work.
            # Protected views will get request.user = AnonymousUser and be
            # rejected by their permission classes as expected.
            return None

        # Cookie-based auth must enforce CSRF like SessionAuthentication
        self._enforce_csrf(request)

        return self.get_user(validated_token), validated_token

    def _enforce_csrf(self, request):
        """Enforce CSRF validation for cookie-authenticated requests."""
        check = _CSRFCheck(lambda req: None)
        # populates request.META['CSRF_COOKIE'] from the cookie
        check.process_request(request)
        reason = check.process_view(request, None, (), {})
        if reason:
            raise exceptions.PermissionDenied(f'CSRF Failed: {reason}')
