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
