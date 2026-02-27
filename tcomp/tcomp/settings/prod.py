from .base import *
import os

DEBUG = False

DATABASES = {
    'default': {
        'ENGINE': 'django.contrib.gis.db.backends.postgis',
        'NAME': os.environ.get('DB_NAME'),
        'HOST': os.environ.get('DB_HOST'),
        'PORT': os.environ.get('DB_PORT'),
        'USER': os.environ.get('DB_USER'),
        'PASSWORD': os.environ.get('DB_PASSWORD'),
        'DISABLE_SERVER_SIDE_CURSORS': True,  # Required for pgbouncer transaction mode
        'OPTIONS': {
            'sslmode': os.environ.get('DB_SSLMODE', 'require'),
            **(
                {'sslrootcert': os.environ.get('DB_SSLROOTCERT')}
                if os.environ.get('DB_SSLROOTCERT') else {}
            ),
        },
    }
}

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Cookie-only auth in production — no Bearer fallback
# (inherits from base.py which only has JWTCookieAuthentication)

_csv = lambda v: [s.strip() for s in v.split(',') if s.strip()]

# Override base.py defaults with production-safe defaults
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS', default='https://refuje.com', cast=_csv
)
FRONTEND_ORIGIN = config('FRONTEND_ORIGIN', default='https://refuje.com')
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS', default='https://refuje.com,https://api.refuje.com', cast=_csv
)
# Cookie domain (e.g. .refuje.com) — shared across app/api subdomains
_cookie_domain = config('COOKIE_DOMAIN', default='.refuje.com')
CSRF_COOKIE_DOMAIN = _cookie_domain
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_DOMAIN = _cookie_domain
SIMPLE_JWT['AUTH_COOKIE_DOMAIN'] = _cookie_domain

STORAGE_BACKEND = config('STORAGE_BACKEND', default='local')

if STORAGE_BACKEND == 'r2':
    AWS_ACCESS_KEY_ID = config('R2_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = config('R2_SECRET_ACCESS_KEY')
    AWS_STORAGE_BUCKET_NAME = config('R2_BUCKET_NAME')
    AWS_S3_ENDPOINT_URL = config('R2_ENDPOINT_URL')
    AWS_S3_REGION_NAME = 'auto'
    AWS_S3_CUSTOM_DOMAIN = config('AWS_S3_CUSTOM_DOMAIN', default=None)
    AWS_S3_OBJECT_PARAMETERS = {'CacheControl': 'max-age=86400'}
    AWS_DEFAULT_ACL = None
    AWS_S3_SIGNATURE_VERSION = 's3v4'

    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"},
        "staticfiles": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"},
    }

    if AWS_S3_CUSTOM_DOMAIN:
        STATIC_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/static/'
        MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/media/'
    else:
        STATIC_URL = f'{AWS_S3_ENDPOINT_URL}/{AWS_STORAGE_BUCKET_NAME}/static/'
        MEDIA_URL = f'{AWS_S3_ENDPOINT_URL}/{AWS_STORAGE_BUCKET_NAME}/media/'
