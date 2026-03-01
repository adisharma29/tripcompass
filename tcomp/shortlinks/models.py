import secrets

from django.db import models, IntegrityError
from django.utils import timezone


def generate_code(length=8):
    """Generate a URL-safe short code. 8 chars = ~48 bits of entropy."""
    return secrets.token_urlsafe(length)[:length]


ALLOWED_REDIRECT_ORIGINS = None  # Populated lazily from settings


def _get_origin(url):
    """Extract origin (scheme://host[:port]) from a URL. Port omitted for 80/443."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    # Omit port for default HTTP/HTTPS ports (matches browser origin semantics)
    if parsed.port and parsed.port not in (80, 443):
        return f'{parsed.scheme}://{parsed.hostname}:{parsed.port}'
    return f'{parsed.scheme}://{parsed.hostname}'


def _get_allowed_origins():
    global ALLOWED_REDIRECT_ORIGINS
    if ALLOWED_REDIRECT_ORIGINS is None:
        from django.conf import settings

        ALLOWED_REDIRECT_ORIGINS = {
            _get_origin(settings.API_ORIGIN),
            _get_origin(settings.FRONTEND_ORIGIN),
        }
    return ALLOWED_REDIRECT_ORIGINS


class ShortLinkManager(models.Manager):
    def create_for_url(self, target_url, expires_at=None, max_clicks=None, metadata=None):
        """Create a short link with a unique code. Validates target origin, retries on collision."""
        target_origin = _get_origin(target_url)
        if target_origin not in _get_allowed_origins():
            raise ValueError(
                f'Target URL origin "{target_origin}" not in allowed origins: {_get_allowed_origins()}'
            )
        for _ in range(5):
            code = generate_code()
            try:
                return self.create(
                    code=code,
                    target_url=target_url,
                    expires_at=expires_at,
                    max_clicks=max_clicks,
                    metadata=metadata or {},
                )
            except IntegrityError:
                continue  # Code collision — retry with new code
        raise RuntimeError('Failed to generate unique short code after 5 attempts')


class ShortLink(models.Model):
    """Generic short URL that redirects to a target URL."""

    code = models.CharField(max_length=16, unique=True, db_index=True)
    target_url = models.URLField(max_length=2048)

    # Optional constraints
    expires_at = models.DateTimeField(null=True, blank=True)
    max_clicks = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Tracking
    click_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    objects = ShortLinkManager()

    def is_valid(self):
        """Check if this short link can still redirect."""
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        if self.max_clicks and self.click_count >= self.max_clicks:
            return False
        return True

    def __str__(self):
        return f'/s/{self.code} → {self.target_url[:60]}'
