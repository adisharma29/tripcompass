from .base import *
import os

DEBUG = True

DATABASES = {
    'default': {
        'ENGINE': 'django.contrib.gis.db.backends.postgis',
        'NAME': os.environ.get('DB_NAME'),
        'HOST': os.environ.get('DB_HOST'),
        'PORT': os.environ.get('DB_PORT'),
        'USER': os.environ.get('DB_USER'),
        'PASSWORD': os.environ.get('DB_PASSWORD'),
        'OPTIONS': {
            'sslmode': os.environ.get('DB_SSLMODE', 'prefer'),
            **(
                {'sslrootcert': os.environ.get('DB_SSLROOTCERT')}
                if os.environ.get('DB_SSLROOTCERT') else {}
            ),
        },
    }
}

# Local file storage in dev
STORAGE_BACKEND = 'local'

# Cookie not secure over HTTP in dev
SIMPLE_JWT['AUTH_COOKIE_SECURE'] = False

# Allow Bearer auth in dev for Postman/curl testing (never in prod)
REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES'] = [
    'concierge.authentication.JWTCookieAuthentication',
    'rest_framework_simplejwt.authentication.JWTAuthentication',
]

# CSRF trusted origins for local dev
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:6001',
    'http://localhost:3000',
]

CORS_ALLOWED_ORIGINS = [
    'http://localhost:6001',
    'http://127.0.0.1:6001',
    'http://localhost:3000',
    'http://127.0.0.1:3000',
]

FRONTEND_ORIGIN = 'http://localhost:6001'
