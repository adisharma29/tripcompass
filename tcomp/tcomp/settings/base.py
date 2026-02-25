from pathlib import Path
from datetime import timedelta
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config('SECRET_KEY')

DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='localhost,127.0.0.1',
    cast=lambda v: [s.strip() for s in v.split(',')]
)

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.gis',
    # Third party
    'rest_framework',
    'corsheaders',
    'django_filters',
    'django_celery_beat',
    'django_celery_results',
    'rest_framework_simplejwt.token_blacklist',
    # Local
    'users',
    'location',
    'guides',
    'concierge',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'tcomp.middleware.NoCacheAuthMiddleware',
]

ROOT_URLCONF = 'tcomp.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'tcomp.wsgi.application'
ASGI_APPLICATION = 'tcomp.asgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.contrib.gis.db.backends.postgis',
        'NAME': config('DB_NAME', default='tcomp_db'),
        'USER': config('DB_USER', default='tcomp_user'),
        'PASSWORD': config('DB_PASSWORD', default='tcomp_password'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'users.User'

# Silence auth.E003 — email is not unique=True on the field because guests have email=''.
# Uniqueness is enforced via a partial unique index (WHERE email != '').
SILENCED_SYSTEM_CHECKS = ['auth.E003']

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'concierge.authentication.JWTCookieAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PAGINATION_CLASS': 'tcomp.pagination.StandardPagination',
    'PAGE_SIZE': 50,
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}

# JWT — cookie-based auth
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    # Cookie settings
    'AUTH_COOKIE': 'access_token',
    'AUTH_COOKIE_SECURE': not DEBUG,
    'AUTH_COOKIE_HTTP_ONLY': True,
    'AUTH_COOKIE_SAMESITE': 'Lax',
    'AUTH_COOKIE_PATH': '/',
    'REFRESH_COOKIE': 'refresh_token',
    'REFRESH_COOKIE_PATH': '/api/v1/auth/token/refresh/',
}

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3000,http://127.0.0.1:3000',
    cast=lambda v: [s.strip() for s in v.split(',')]
)

# Auth endpoint paths that must never be cached
NO_STORE_PATHS = [
    '/api/v1/auth/csrf/',
    '/api/v1/auth/token/',
    '/api/v1/auth/token/refresh/',
    '/api/v1/auth/otp/send/',
    '/api/v1/auth/otp/verify/',
    '/api/v1/auth/logout/',
    '/api/v1/auth/profile/',
]

# Web Push VAPID keys
WEBPUSH_VAPID_PRIVATE_KEY = config('VAPID_PRIVATE_KEY', default='')
WEBPUSH_VAPID_PUBLIC_KEY = config('VAPID_PUBLIC_KEY', default='')
WEBPUSH_VAPID_ADMIN_EMAIL = config('VAPID_ADMIN_EMAIL', default='admin@refuje.com')

# --- Gupshup WhatsApp API (primary OTP channel) ---
GUPSHUP_WA_API_KEY = config('GUPSHUP_WA_API_KEY', default='')
GUPSHUP_WA_SOURCE_PHONE = config('GUPSHUP_WA_SOURCE_PHONE', default='')
GUPSHUP_WA_APP_NAME = config('GUPSHUP_WA_APP_NAME', default='Refuje')
GUPSHUP_WA_OTP_TEMPLATE_ID = config('GUPSHUP_WA_OTP_TEMPLATE_ID', default='')
GUPSHUP_WA_WEBHOOK_SECRET = config('GUPSHUP_WA_WEBHOOK_SECRET', default='')
GUPSHUP_WA_FALLBACK_TIMEOUT_SECONDS = 10
GUPSHUP_WA_STAFF_INVITE_TEMPLATE_ID = config('GUPSHUP_WA_STAFF_INVITE_TEMPLATE_ID', default='')

# --- Gupshup WhatsApp Notification Templates (per event type) ---
GUPSHUP_WA_STAFF_REQUEST_TEMPLATE_ID = config('GUPSHUP_WA_STAFF_REQUEST_TEMPLATE_ID', default='')
GUPSHUP_WA_STAFF_ESCALATION_TEMPLATE_ID = config('GUPSHUP_WA_STAFF_ESCALATION_TEMPLATE_ID', default='')
GUPSHUP_WA_STAFF_RESPONSE_DUE_TEMPLATE_ID = config('GUPSHUP_WA_STAFF_RESPONSE_DUE_TEMPLATE_ID', default='')

# --- Resend Email API ---
RESEND_API_KEY = config('RESEND_API_KEY', default='')
RESEND_FROM_EMAIL = config('RESEND_FROM_EMAIL', default='Refuje <notifications@notifications.refuje.com>')

# --- Gupshup Enterprise SMS API (fallback OTP + escalation SMS) ---
GUPSHUP_SMS_USERID = config('GUPSHUP_SMS_USERID', default='')
GUPSHUP_SMS_PASSWORD = config('GUPSHUP_SMS_PASSWORD', default='')
GUPSHUP_SMS_SENDER_MASK = config('GUPSHUP_SMS_SENDER_MASK', default='REFUJE')
GUPSHUP_SMS_DLT_TEMPLATE_ID = config('GUPSHUP_SMS_DLT_TEMPLATE_ID', default='')
GUPSHUP_SMS_PRINCIPAL_ENTITY_ID = config('GUPSHUP_SMS_PRINCIPAL_ENTITY_ID', default='')
GUPSHUP_SMS_OTP_MSG_TEMPLATE = 'Your Refuje verification code is %code%'
GUPSHUP_SMS_ESCALATION_DLT_TEMPLATE_ID = config('GUPSHUP_SMS_ESCALATION_DLT_TEMPLATE_ID', default='')

# --- OTP settings ---
OTP_EXPIRY_SECONDS = 600   # 10 minutes
OTP_CODE_LENGTH = 6
OTP_MAX_ATTEMPTS = 5
OTP_SEND_RATE_PER_PHONE = 3
OTP_SEND_RATE_PER_IP = 5

# SSE (Server-Sent Events via Redis pub/sub)
SSE_REDIS_URL = config('SSE_REDIS_URL', default='redis://localhost:6379/2')
SSE_HEARTBEAT_SECONDS = 15

# Escalation tier thresholds (minutes from request creation)
ESCALATION_TIER_MINUTES = [15, 30, 60]

# Frontend origin (used for QR target_url generation)
FRONTEND_ORIGIN = config('FRONTEND_ORIGIN', default='http://localhost:6001')

# --- Celery ---
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = 'django-db'
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

# DatabaseScheduler syncs these into the DB on first beat startup.
# Can be overridden/tuned via Django admin afterwards.
CELERY_BEAT_SCHEDULE = {
    'check-escalations': {
        'task': 'concierge.tasks.check_escalations_task',
        'schedule': 5 * 60,  # every 5 minutes
    },
    'expire-stale-stays': {
        'task': 'concierge.tasks.expire_stale_stays_task',
        'schedule': 60 * 60,  # every hour
    },
    'expire-stale-requests': {
        'task': 'concierge.tasks.expire_stale_requests_task',
        'schedule': 60 * 60,  # every hour
    },
    'response-due-reminder': {
        'task': 'concierge.tasks.response_due_reminder_task',
        'schedule': 5 * 60,  # every 5 minutes
    },
    'otp-wa-fallback-sweep': {
        'task': 'concierge.tasks.otp_wa_fallback_sweep_task',
        'schedule': 10,  # every 10 seconds
    },
    'cleanup-expired-otps': {
        'task': 'concierge.tasks.cleanup_expired_otps_task',
        'schedule': 24 * 60 * 60,  # daily
    },
    'daily-digest': {
        'task': 'concierge.tasks.daily_digest_task',
        'schedule': 24 * 60 * 60,  # daily
    },
    'expire-events': {
        'task': 'concierge.tasks.expire_events_task',
        'schedule': 60 * 60,  # every hour
    },
    'expire-top-deals': {
        'task': 'concierge.tasks.expire_top_deals_task',
        'schedule': 5 * 60,  # every 5 minutes
    },
    'cleanup-orphaned-content-images': {
        'task': 'concierge.tasks.cleanup_orphaned_content_images_task',
        'schedule': 7 * 24 * 60 * 60,  # weekly
    },
}
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TIME_LIMIT = 10 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 5 * 60
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {'visibility_timeout': 3600}

# Django cache (for rate limiting)
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': config('CACHE_REDIS_URL', default='redis://localhost:6379/1'),
    }
}

# Upload limits
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024   # 5 MB

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
}
