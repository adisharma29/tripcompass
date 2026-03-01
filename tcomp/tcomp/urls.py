from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from shortlinks.views import shortlink_redirect

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include('guides.urls')),
    path('api/v1/auth/', include('users.urls')),
    path('api/v1/', include('concierge.urls')),
    path('s/<str:code>', shortlink_redirect, name='shortlink_redirect'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
