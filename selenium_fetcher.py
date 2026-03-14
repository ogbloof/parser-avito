# selenium_fetcher.py — загрузка страниц через headless Chrome или Playwright (с опциональным прокси)
import re
import time
import traceback
from logging_config import get_logger

logger = get_logger("parser")

try:
    from config import AVITO_PROXY_NORMALIZED
except Exception:
    AVITO_PROXY_NORMALIZED = None


def _proxy_has_auth(proxy_str):
    if not proxy_str:
        return False
    return "@" in proxy_str and "://" in proxy_str


def _parse_proxy_playwright(proxy_url):
    """Из http://user:pass@host:port возвращает dict server, username, password для Playwright."""
    if not proxy_url or "://" not in proxy_url or "@" not in proxy_url:
        return None
    try:
        scheme = "http"
        if proxy_url.startswith("https://"):
            scheme = "https"
            rest = proxy_url[8:]
        elif proxy_url.startswith("http://"):
            rest = proxy_url[7:]
        else:
            return None
        auth, _, hostport = rest.partition("@")
        if not hostport or ":" not in auth:
            return None
        user, password = auth.split(":", 1)
        return {"server": f"{scheme}://{hostport}", "username": user, "password": password}
    except Exception:
        return None


def _fetch_playwright(url, proxy_url, wait_after_load, page_load_timeout):
    """Загрузка через Playwright (встроенная поддержка прокси с логином, без selenium-wire)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.debug("[Playwright] не установлен: pip install playwright && playwright install chromium")
        return None

    proxy_dict = _parse_proxy_playwright(proxy_url)
    if not proxy_dict:
        logger.warning("[Playwright] не удалось разобрать прокси")
        return None

    timeout_ms = max(int(page_load_timeout), 60) * 1000
    html = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = browser.new_context(
                    proxy=proxy_dict,
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    ignore_https_errors=True,
                )
                context.set_default_timeout(timeout_ms)
                page = context.new_page()
                logger.info(f"[Playwright] GET {url[:80]}... (таймаут {timeout_ms // 1000} с)")
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                time.sleep(wait_after_load)
                html = page.content()
                logger.info(f"[Playwright] Ответ: {len(html)} байт")
            finally:
                browser.close()
    except Exception as e:
        logger.error(f"[Playwright] ошибка: {e}\n{traceback.format_exc()}")
        return None
    return html


def fetch_page_selenium(url, wait_after_load=5, page_load_timeout=30, proxy=None):
    """
    Загружает страницу через Playwright (при прокси с логином) или Selenium.
    Возвращает HTML или None при ошибке.
    """
    proxy_str = proxy or AVITO_PROXY_NORMALIZED
    if proxy_str:
        logger.info(f"[Selenium] Прокси: {'***@' + proxy_str.split('@')[-1] if '@' in proxy_str else proxy_str}")

    # Прокси с авторизацией — сначала Playwright (стабильнее), при неудаче selenium-wire
    if proxy_str and _proxy_has_auth(proxy_str):
        html = _fetch_playwright(url, proxy_str, wait_after_load, page_load_timeout)
        if html is not None:
            return html
        try:
            return _fetch_seleniumwire(url, proxy_str, wait_after_load, page_load_timeout)
        except Exception as e:
            logger.error(f"[Selenium] Ошибка selenium-wire: {e}\n{traceback.format_exc()}")
            return None

    # Обычный Selenium (без прокси или прокси без логина)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError as e:
        logger.error(f"[Selenium] Импорт: {e}. Установите: pip install selenium webdriver-manager")
        return None

    driver = None
    try:
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_argument(
            "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        options.page_load_strategy = "normal"

        if proxy_str and not _proxy_has_auth(proxy_str):
            # Только host:port без логина
            server = proxy_str
            if server.startswith("http://"):
                server = server.replace("http://", "", 1)
            if server.startswith("https://"):
                server = server.replace("https://", "", 1)
            options.add_argument(f"--proxy-server={server}")
            logger.info(f"[Selenium] proxy-server={server}")

        logger.info(f"[Selenium] Запуск Chrome, таймаут {page_load_timeout} с...")
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(page_load_timeout)

        logger.info(f"[Selenium] GET {url[:80]}...")
        t0 = time.time()
        driver.get(url)
        elapsed = time.time() - t0
        logger.info(f"[Selenium] Загрузка страницы заняла {elapsed:.1f} с")

        time.sleep(wait_after_load)
        html = driver.page_source
        length = len(html)
        logger.info(f"[Selenium] Ответ: {length} байт")

        # Краткий отладочный срез
        title = driver.title if hasattr(driver, "title") else ""
        logger.info(f"[Selenium] title={title[:60]!r}, html[:200]={html[:200].replace(chr(10), ' ')!r}")

        if length < 500 and "ipify" not in url.lower():
            logger.warning(f"[Selenium] Слишком короткий ответ (возможно ошибка/капча): {html[:500]!r}")
        return html
    except Exception as e:
        logger.error(f"[Selenium] Исключение: {e}\n{traceback.format_exc()}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
                logger.info("[Selenium] Драйвер закрыт")
            except Exception as e:
                logger.debug(f"[Selenium] quit: {e}")


def _fetch_seleniumwire(url, proxy_url, wait_after_load, page_load_timeout):
    try:
        from seleniumwire import webdriver as wire_webdriver
    except ImportError:
        logger.error("[Selenium] Прокси с логином требует: pip install selenium-wire")
        return None

    # Через прокси страницы грузятся дольше — не ждать дольше 60 с и не блокироваться на тяжёлых ресурсах
    timeout = max(int(page_load_timeout), 60)
    https_proxy = proxy_url.replace("http://", "https://", 1) if proxy_url.startswith("http://") else proxy_url
    options = {
        "proxy": {
            "http": proxy_url,
            "https": https_proxy,
        },
        "connection_timeout": 90,
        "verify_ssl": False,
        "suppress_connection_errors": False,
    }
    driver = None
    try:
        chrome_options = wire_webdriver.ChromeOptions()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_argument(
            "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        # eager — не ждать картинки/скрипты, только DOM; меньше шанс таймаута через прокси
        chrome_options.page_load_strategy = "eager"

        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        logger.info(f"[Selenium] Запуск Chrome (selenium-wire, прокси с авторизацией), таймаут {timeout} с...")
        service = Service(ChromeDriverManager().install())
        driver = wire_webdriver.Chrome(
            service=service,
            options=chrome_options,
            seleniumwire_options=options,
        )
        driver.set_page_load_timeout(timeout)

        logger.info(f"[Selenium] GET {url[:80]}...")
        t0 = time.time()
        driver.get(url)
        elapsed = time.time() - t0
        logger.info(f"[Selenium] Загрузка: {elapsed:.1f} с")

        time.sleep(wait_after_load)
        html = driver.page_source
        length = len(html)
        logger.info(f"[Selenium] Ответ: {length} байт, title={getattr(driver, 'title', '')[:50]!r}")
        logger.info(f"[Selenium] html[:300]={html[:300].replace(chr(10), ' ')!r}")

        if length < 500 and "ipify" not in url.lower():
            logger.warning(f"[Selenium] Короткий ответ: {html[:500]!r}")
        return html
    except Exception as e:
        err_str = str(e)
        if "ERR_TUNNEL_CONNECTION_FAILED" in err_str or "TUNNEL_CONNECTION_FAILED" in err_str:
            logger.error(
                "[Selenium] Туннель через прокси не установился. Проверь: доступен ли прокси (хост:порт), "
                "нет ли блокировки фаерволом/VPN, не истёк ли логин прокси."
            )
        logger.error(f"[Selenium] selenium-wire ошибка: {e}\n{traceback.format_exc()}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def check_proxy(proxy_raw: str | None = None):
    """
    Проверяет: с какого IP идёт трафик и открывает ли Авито.
    Возвращает (ip или строка с ошибкой, статус_авито: "ok" / "block" / ошибка).
    Вызывать из потока (run_in_thread).
    """
    try:
        from config import AVITO_PROXY_NORMALIZED, normalize_proxy
    except Exception:
        AVITO_PROXY_NORMALIZED = None
        normalize_proxy = lambda x: x  # noqa: E731

    proxy_str = normalize_proxy(proxy_raw) if proxy_raw else AVITO_PROXY_NORMALIZED
    ip_result = "прокси не задан (AVITO_PROXY в .env)"
    avito_result = "—"
    # 1) Узнать IP (ipify может вернуть HTML или чистый текст)
    html_ip = fetch_page_selenium("https://api.ipify.org", wait_after_load=2, page_load_timeout=15, proxy=proxy_str)
    if html_ip:
        # Вытащить только IPv4, чтобы в ответ не попали теги <html> и т.д.
        match = re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", html_ip)
        ip_result = match.group(0) if match else html_ip.strip()[:50].replace("<", "").replace(">", "")
    else:
        ip_result = "не удалось (нет ответа или ошибка)"
    # 2) Проверить Авито (через прокси даём 60 с — мобильные прокси бывают медленные)
    html_avito = fetch_page_selenium(
        "https://m.avito.ru/novosibirsk/kvartiry/prodam",
        wait_after_load=4,
        page_load_timeout=60 if proxy_str else 25,
        proxy=proxy_str,
    )
    if not html_avito:
        avito_result = "ошибка загрузки"
    elif "доступ ограничен" in html_avito.lower() or "проблема с ip" in html_avito.lower():
        avito_result = "блок (доступ ограничен по IP)"
    elif (
        "data-marker=\"item\"" in html_avito
        or '"items"' in html_avito
        or "data-marker=\"item-title\"" in html_avito
        or "item-title" in html_avito
        or "snippet-title" in html_avito
        or "snippet-link" in html_avito
        or "page-title/count" in html_avito
    ):
        avito_result = "ок, объявления видны"
    else:
        avito_result = "страница загружена, но объявлений не найдено (возможен блок или другая верстка)"
    return ip_result, avito_result
