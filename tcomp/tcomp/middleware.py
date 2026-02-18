from django.conf import settings


class NoCacheAuthMiddleware:
    """Sets Cache-Control: no-store on auth endpoints to prevent
    CDN/proxy caching of session-sensitive responses."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.no_store_paths = getattr(settings, 'NO_STORE_PATHS', [])

    def __call__(self, request):
        response = self.get_response(request)
        if any(request.path.startswith(p) for p in self.no_store_paths):
            response['Cache-Control'] = 'no-store'
        return response
