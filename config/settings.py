import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
# Deliberately small .env reader; inherited environment variables take priority.
env_file = BASE_DIR / ".env"
if env_file.exists():
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == "replace-with-a-long-random-secret":
    raise RuntimeError("Run python3 setup.py first, or supply DJANGO_SECRET_KEY.")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
CSRF_TRUSTED_ORIGINS = [x for x in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if x]
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "leads", "discovery", "automation", "classification",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "whitenoise.middleware.WhiteNoiseMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates"],
              "APP_DIRS": True, "OPTIONS": {"context_processors": [
                  "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
                  "django.contrib.messages.context_processors.messages", "leads.views.console_context",
              ]}}]
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
database_url = os.environ.get("DATABASE_URL", "")
if database_url:
    parsed = urlparse(database_url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise RuntimeError("DATABASE_URL must be a PostgreSQL URL.")
    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""), "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname, "PORT": parsed.port or 5432, "CONN_MAX_AGE": 60}}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": DATA_DIR / "leads.sqlite3",
                             "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"}}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("APP_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 43200
SESSION_COOKIE_SECURE = os.environ.get("DJANGO_SECURE_COOKIES", "0") == "1"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
BOT_CONTACT = os.environ.get("BOT_CONTACT", "")
BOT_USER_AGENT = "ClearPayLeadBot/0.1" + (f" (+{BOT_CONTACT})" if BOT_CONTACT else "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")
BRAVE_SEARCH_ENABLED = os.environ.get("BRAVE_SEARCH_ENABLED", "0") == "1"
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
BRAVE_DAILY_SEARCH_LIMIT = max(0, int(os.environ.get("BRAVE_DAILY_SEARCH_LIMIT", "20")))

# Merely installing this app or supplying a key never enables paid work.
JEV_MODE = os.environ.get("JEV_MODE", "off")
if JEV_MODE not in ("off", "mock", "live"):
    raise RuntimeError("JEV_MODE must be off, mock or live.")
JEV_CAPTURE_ENABLED = os.environ.get("JEV_CAPTURE_ENABLED", "0") == "1"
JEV_LAYERED_BLOCKS_ENABLED = os.environ.get("JEV_LAYERED_BLOCKS_ENABLED", "0") == "1"
EXTRACTION_PACKS_ENABLED = os.environ.get("EXTRACTION_PACKS_ENABLED", "0") == "1"
JEV_ROUTING_ENABLED = os.environ.get("JEV_ROUTING_ENABLED", "0") == "1"
JEV_MODEL = "jev-1.13.0"
TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
JEV_PRICE_CONFIRMED = os.environ.get("JEV_PRICE_CONFIRMED", "0") == "1"
JEV_TOKEN_COUNTER = os.environ.get("JEV_TOKEN_COUNTER", "")
JEV_ALLOW_ESTIMATED_TOKENS = os.environ.get("JEV_ALLOW_ESTIMATED_TOKENS", "0") == "1"
JEV_MAX_INPUT_TOKENS = 5000
try:
    _jev_daily_usd = Decimal(os.environ.get("JEV_DAILY_ALLOWANCE_USD", "2"))
    _jev_daily_nusd = _jev_daily_usd * 1_000_000_000
    if (not _jev_daily_usd.is_finite() or _jev_daily_usd < 0 or _jev_daily_usd > 1000 or
            _jev_daily_nusd != _jev_daily_nusd.to_integral_value()):
        raise ValueError
    JEV_DAILY_ALLOWANCE_NUSD = int(_jev_daily_nusd)
except (InvalidOperation, ValueError):
    raise RuntimeError("JEV_DAILY_ALLOWANCE_USD must be between 0 and 1000 with at most nine decimal places.")
