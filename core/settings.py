from pathlib import Path
from datetime import timedelta
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', cast=bool, default=False)
ALLOWED_HOSTS = ['hse-construction-backend.onrender.com', '127.0.0.1', 'localhost']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # third party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    # local
    'users',
    'projects',
    'hira',
    'permits',
    'notifications',
    'audit',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME'),
        'USER': config('DB_USER'),
        'PASSWORD': config('DB_PASSWORD'),
        'HOST': config('DB_HOST'),
        'PORT': config('DB_PORT', default='5432'),
        'DISABLE_SERVER_SIDE_CURSORS': True
    }
}

AUTH_USER_MODEL = 'users.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Jakarta'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Evidence photos (permits). Private Supabase Storage bucket, reached with the service key.
# The service key is a secret: set it on Render only, never in the frontend.
SUPABASE_URL = config('SUPABASE_URL', default='')
SUPABASE_SERVICE_KEY = config('SUPABASE_SERVICE_KEY', default='')
SUPABASE_BUCKET = config('SUPABASE_BUCKET', default='permit-photos')
EVIDENCE_STORAGE = 'core.storage.SupabaseStorage'

# Email (MK approval links and OTP codes). Resend over HTTPS when a key is set, else the console.
RESEND_API_KEY = config('RESEND_API_KEY', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='K3 Permit <noreply@localhost>')
EMAIL_BACKEND = ('core.mail.ResendBackend' if RESEND_API_KEY
                 else 'django.core.mail.backends.console.EmailBackend')
FRONTEND_BASE_URL = config('FRONTEND_BASE_URL', default='').strip().rstrip('/')   # builds the emailed link

# MK approval limits
MK_LINK_TTL = timedelta(days=7)
MK_OTP_TTL = timedelta(minutes=10)
MK_OTP_COOLDOWN_SECONDS = 60      # between two "send code" requests
MK_OTP_MAX_SENDS = 5              # codes per link; then HSE has to issue a new link
MK_OTP_MAX_ATTEMPTS = 5           # wrong tries per code

CORS_ALLOW_ALL_ORIGINS = True  # TODO: restrict to frontend domain in production

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # Coarse per-IP limits on the public MK pages. The real protection is per link (see MK_*).
    'DEFAULT_THROTTLE_RATES': {'mk_read': '60/min', 'mk_write': '20/min'},
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=8),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'ROTATE_REFRESH_TOKENS': True,
}

HIRA_FORM_REVISION = 3