import os

# Загружаем .env из текущей директории (для локального запуска без export)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


BOT_TOKEN = _env("BOT_TOKEN")

ZENROWS_API_KEY = _env("ZENROWS_API_KEY", "YOUR_API_KEY_HERE")
SCRAPINGBEE_API_KEY = _env("SCRAPINGBEE_API_KEY", "YOUR_SCRAPINGBEE_KEY_HERE")

# Прокси для Selenium. Форматы:
#   http://host:port   или   http://user:pass@host:port
#   host:port:user:pass   (преобразуется в http://user:pass@host:port)
AVITO_PROXY = _env("AVITO_PROXY")


def _normalize_proxy(raw: str | None) -> str | None:
    if not raw or not (raw := raw.strip()):
        return None
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("socks"):
        return raw
    # Формат host:port@user:pass
    if "@" in raw and ":" in raw:
        left, _, right = raw.partition("@")
        if ":" in left and ":" in right:
            host, port = left.split(":", 1)
            user, password = right.split(":", 1)
            return f"http://{user}:{password}@{host}:{port}"
    # Формат host:port:user:pass
    parts = raw.split(":", 3)
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return raw


def normalize_proxy(raw: str | None) -> str | None:
    """Public wrapper for proxy normalization (supports multiple formats)."""
    return _normalize_proxy(raw)

# Интервал планировщика (минуты)
PARSER_INTERVAL_MINUTES = int(_env("PARSER_INTERVAL_MINUTES", "5") or "5")

# Админы (Telegram user_id через запятую)
_admin_raw = _env("ADMIN_USER_IDS", "") or ""
ADMIN_USER_IDS = [int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()]

# API и Mini App
API_PORT = int(_env("API_PORT", "8080") or "8080")
WEBAPP_URL = _env("WEBAPP_URL", "")  # https://your-domain.com

# Нормализованный прокси (с подстановкой из host:port:user:pass)
AVITO_PROXY_NORMALIZED = _normalize_proxy(AVITO_PROXY) if AVITO_PROXY else None

