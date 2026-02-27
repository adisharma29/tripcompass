from django.http import HttpResponseRedirect, HttpResponseGone, Http404
from django.db.models import F
from django.utils import timezone
from django.views.decorators.http import require_GET

from .models import ShortLink


@require_GET
def shortlink_redirect(request, code):
    """Resolve short code → redirect to target URL. Atomic click-count increment."""
    try:
        link = ShortLink.objects.get(code=code)
    except ShortLink.DoesNotExist:
        raise Http404

    # Atomic conditional update — prevents max_clicks race.
    # Builds WHERE clause: is_active=True, not expired, and (no max_clicks OR under limit).
    qs = ShortLink.objects.filter(pk=link.pk, is_active=True)
    if link.expires_at:
        qs = qs.filter(expires_at__gte=timezone.now())
    if link.max_clicks:
        qs = qs.filter(click_count__lt=link.max_clicks)

    updated = qs.update(click_count=F('click_count') + 1)
    if not updated:
        return HttpResponseGone('This link has expired or is no longer available.')

    return HttpResponseRedirect(link.target_url)
