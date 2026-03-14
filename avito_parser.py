# avito_parser.py
import asyncio
import random
import re
import os
import ssl
import json
from pathlib import Path
from datetime import datetime
import aiohttp
from logging_config import get_logger
from database import SessionLocal, Ad, Photo, UserFilter, run_in_thread
from urllib.parse import quote, urlparse, parse_qs
from selenium_fetcher import fetch_page_selenium
from config import ZENROWS_API_KEY, SCRAPINGBEE_API_KEY

logger = get_logger('parser')
PHOTOS_DIR = Path("photos")
PHOTOS_DIR.mkdir(exist_ok=True)

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

def get_zenrows_params(target_url, wait_ms=20000):
    """Параметры для ZenRows. wait в миллисекундах (документация: 2–30 сек). Геолокация ru для Авито."""
    return {
        "apikey": ZENROWS_API_KEY,
        "url": target_url,
        "js_render": "true",
        "wait": str(wait_ms),
        "premium_proxy": "true",
        "antibot": "true",
        "block_resources": "image,font,media",
        "proxy_country": "ru",
    }

def get_scrapingbee_url(target_url):
    """ScrapingBee API"""
    encoded_url = quote(target_url, safe='')
    return f"https://app.scrapingbee.com/api/v1/?api_key={SCRAPINGBEE_API_KEY}&url={encoded_url}&render_js=true&wait=15000&premium_proxy=true"

def convert_to_mobile(url):
    """Конвертирует десктопный URL в мобильный (https://m.avito.ru/..., не www.m.avito.ru)."""
    if 'm.avito.ru' in url:
        return url
    # www.avito.ru -> m.avito.ru (не avito.ru -> m.avito.ru, иначе получится www.m.avito.ru = ERR_NAME_NOT_RESOLVED)
    return url.replace('www.avito.ru', 'm.avito.ru')

def convert_to_api(url):
    """Пробуем получить JSON напрямую через API Авито"""
    # Иногда Авито отдаёт данные через API эндпоинты
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    
    # Формируем API URL (экспериментально)
    base = f"https://www.avito.ru/api/9/items"
    return base

async def download_image(session, url, path):
    try:
        async with session.get(url, timeout=10, ssl=ssl_context) as response:
            if response.status == 200:
                with open(path, 'wb') as f:
                    f.write(await response.read())
                return True
    except Exception as e:
        logger.error(f"Ошибка скачивания: {e}")
        return False


def _db_update_ad_details(ad_id, title, price_text, price_value, address, description):
    """Обновляет подробные данные объявления в БД и возвращает объект."""
    db = SessionLocal()
    try:
        ad = db.query(Ad).filter(Ad.id == ad_id).first()
        if not ad:
            return None
        if title:
            ad.title = title
        if price_text:
            ad.price = price_text
        if price_value is not None:
            ad.price_value = price_value
        if address:
            ad.address = address
        if description:
            ad.description = description
        ad.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(ad)
        return ad
    finally:
        db.close()

async def extract_price_value(price_str):
    if not price_str:
        return None
    try:
        numbers = re.sub(r'[^\d]', '', price_str)
        return float(numbers) if numbers else None
    except:
        return None

# --- DB Функции ---
def _db_get_active_filters():
    db = SessionLocal()
    try:
        filters = db.query(UserFilter).filter(UserFilter.is_active == True).all()
        logger.info(f"📋 Активных фильтров: {len(filters)}")
        return filters
    finally:
        db.close()

SOURCE_AVITO = "avito"

def _db_get_ad_by_id(external_id, source=SOURCE_AVITO):
    db = SessionLocal()
    try:
        return db.query(Ad).filter(Ad.avito_id == external_id, Ad.source == source).first()
    finally:
        db.close()

def _db_add_new_ad(user_id, avito_id, title, price, price_value, address, url, source=SOURCE_AVITO):
    db = SessionLocal()
    try:
        new_ad = Ad(
            avito_id=avito_id, user_id=user_id, source=source, title=title, price=price,
            price_value=price_value, address=address, url=url, status="active"
        )
        db.add(new_ad)
        db.commit()
        db.refresh(new_ad)
        return new_ad
    finally:
        db.close()

def _db_update_ad(avito_id, price, price_value, source=SOURCE_AVITO):
    db = SessionLocal()
    try:
        ad = db.query(Ad).filter(Ad.avito_id == avito_id, Ad.source == source).first()
        if ad and ad.price != price:
            ad.price = price
            ad.price_value = price_value
            ad.updated_at = datetime.utcnow()
            db.commit()
        return ad
    finally:
        db.close()

def _db_mark_removed(user_id, current_ids, source=SOURCE_AVITO):
    db = SessionLocal()
    try:
        query = db.query(Ad).filter(Ad.user_id == user_id, Ad.source == source, Ad.status == "active")
        if current_ids:
            query = query.filter(~Ad.avito_id.in_(current_ids))
        old_ads = query.all()
        for ad in old_ads:
            ad.status = "removed"
            ad.removed_at = datetime.utcnow()
        db.commit()
        return old_ads
    finally:
        db.close()

def _db_add_photo(ad_id, file_path):
    db = SessionLocal()
    try:
        photo = Photo(ad_id=ad_id, file_path=file_path, is_main=True)
        db.add(photo)
        db.commit()
    finally:
        db.close()


async def enrich_avito_ad_details(session, ad):
    """
    Подгружает с карточки объявления подробности: точную цену, адрес, описание и главное фото.
    Работает только для Авито.
    """
    try:
        if not ad.url:
            return ad
        clean_url = ad.url.strip()
        mobile_url = convert_to_mobile(clean_url)

        html = None
        # 1) Пробуем через ZenRows (если задан ключ)
        if ZENROWS_API_KEY and ZENROWS_API_KEY != "YOUR_API_KEY_HERE":
            html = await fetch_with_service(session, mobile_url, "zenrows")
            if not html:
                html = await fetch_with_service(session, clean_url, "zenrows")
        # 2) Fallback — Selenium
        if not html:
            html = await run_in_thread(fetch_page_selenium, mobile_url)
        if not html:
            html = await run_in_thread(fetch_page_selenium, clean_url)
        if not html:
            return ad

        content_info = check_content(html)
        if content_info.get("blocked"):
            return ad

        # Заголовок
        title_match = re.search(r"<h1[^>]*>([^<]+)", html)
        title = title_match.group(1).strip() if title_match else ad.title

        # Цена — сначала по новому маркеру, потом meta price, потом общий шаблон
        price_text = None
        m_price = re.search(r'data-marker="item-price-value"[^>]*>([^<]+₽)', html)
        if m_price:
            price_text = m_price.group(1).strip()
        else:
            m_price = re.search(r'<meta[^>]+itemprop="price"[^>]+content="(\d+)"', html)
            if m_price:
                # Преобразуем число в человекочитаемый вид
                num = m_price.group(1)
                try:
                    num_i = int(num)
                    price_text = f"{num_i:,.0f} ₽".replace(",", " ")
                except Exception:
                    price_text = num
            else:
                m_price = re.search(r"(\d[\d\s]*)\s*₽", html)
                if m_price:
                    price_text = m_price.group(0).strip()
        price_val = await extract_price_value(price_text) if price_text else None

        # Адрес — блок data-marker="item-address"
        address = ad.address
        addr_block = re.search(r'data-marker="item-address".{0,800}</div>', html, re.S)
        if addr_block:
            raw = re.sub(r"<[^>]+>", " ", addr_block.group(0))
            raw = re.sub(r"\s+", " ", raw).strip()
            address = raw[:200]

        # Описание — блок data-marker="item-description" или itemprop="description"
        description = ad.description or ""
        desc_block = re.search(r'data-marker="item-description".{0,3000}</div>', html, re.S)
        if not desc_block:
            desc_block = re.search(r'itemprop="description".{0,3000}</div>', html, re.S)
        if desc_block:
            raw = re.sub(r"<[^>]+>", " ", desc_block.group(0))
            raw = re.sub(r"\s+", " ", raw).strip()
            # Обрезаем по первому "window." / "document." / "trackElementsVisibility" если такие куски просочились
            for stop in ("window.", "document.", "trackElementsVisibility"):
                idx = raw.find(stop)
                if idx != -1:
                    raw = raw[:idx].strip()
                    break
            # Ограничиваем длину описания
            if len(raw) > 800:
                raw = raw[:800].rsplit(" ", 1)[0] + "…"
            description = raw

        # Обновляем запись в БД
        updated = await run_in_thread(
            _db_update_ad_details,
            ad.id,
            title,
            price_text,
            price_val,
            address,
            description,
        )
        if updated:
            ad = updated

        # Фото — берём первое itemprop="image"
        # Скачиваем только если у объявления ещё нет фото
        if not ad.photos:
            img_match = re.search(r'itemprop="image"[^>]+src="(https://[^"]+)"', html)
            if img_match:
                img_url = img_match.group(1)
                ext = "jpg"
                file_path = PHOTOS_DIR / f"avito_{ad.id}_1.{ext}"
                ok = await download_image(session, img_url, file_path)
                if ok:
                    await run_in_thread(_db_add_photo, ad.id, str(file_path))

        return ad
    except Exception as e:
        logger.error(f"⚠️ Ошибка обогащения объявления {getattr(ad, 'id', '?')}: {e}")
        return ad

async def fetch_with_service(session, url, service="zenrows"):
    """Универсальная функция запроса. При ZenRows RESP001 — один повтор с увеличенным wait."""
    if service == "zenrows" and ZENROWS_API_KEY != "YOUR_API_KEY_HERE":
        api_base = "https://api.zenrows.com/v1/"
        params = get_zenrows_params(url)
    elif service == "scrapingbee" and SCRAPINGBEE_API_KEY != "YOUR_SCRAPINGBEE_KEY_HERE":
        api_base = "https://app.scrapingbee.com/api/v1/"
        params = {"api_key": SCRAPINGBEE_API_KEY, "url": url, "render_js": "true", "wait": "15000", "premium_proxy": "true"}
    else:
        return None

    async def _do_request(req_params):
        async with session.get(api_base, params=req_params, timeout=120, ssl=ssl_context) as response:
            text = await response.text()
            logger.info(f"📦 {service}: {len(text)} байт, статус {response.status}")
            if response.status != 200:
                try:
                    err = json.loads(text)
                    return None, err.get("code"), err.get("title", str(err))
                except Exception:
                    return None, None, text[:500]
            if text.strip().startswith("{") and ("code" in text and ("REQS" in text or "RESP" in text) or "error" in text.lower()):
                try:
                    err = json.loads(text)
                    return None, err.get("code"), err.get("title", str(err))
                except Exception:
                    return None, None, None
            return text, None, None

    try:
        logger.info(f"🌐 {service}: {url[:60]}...")
        text, err_code, err_msg = await _do_request(params)
        if err_msg:
            logger.error(f"❌ {service} API ошибка: {err_msg}")
        if text is not None:
            return text
        if service == "zenrows" and err_code == "RESP001":
            logger.info("[ZenRows] RESP001 — повтор с wait=30s без block_resources...")
            await asyncio.sleep(2)
            retry_params = {**params, "wait": "30000"}
            retry_params.pop("block_resources", None)
            text, _, err_msg2 = await _do_request(retry_params)
            if err_msg2:
                logger.error(f"❌ {service} повтор: {err_msg2}")
            if text is not None:
                return text
        return None
    except Exception as e:
        logger.error(f"Ошибка {service}: {e}")
        return None

def check_content(html):
    """Проверяет, есть ли в HTML объявления. Блокировка — только явная страница Авито «Доступ ограничен»."""
    low = html.lower()
    has_items = (
        'data-marker="item"' in html
        or 'data-marker="catalog-serp"' in html
        or 'data-marker="item-title"' in html
        or 'data-marker="item-photo-sliderLink"' in html
        or 'data-marker="item-price"' in html
    )
    has_json = '"items"' in html or '"catalogItems"' in html

    # Явная страница блокировки Авито: заголовок «Доступ ограничен: проблема с IP» или текст про капчу/ограничение
    blocked_phrases = [
        ("доступ ограничен", "доступ ограничен"),
        ("проблема с ip", "проблема с IP"),
        ("подозрительная активность", "подозрительная активность"),
    ]
    blocked_reason = None
    for phrase, label in blocked_phrases:
        if phrase in low:
            blocked_reason = label
            break
    # Если в HTML есть и блокировка, и контент объявлений — считаем, что страница нормальная
    if has_items or has_json:
        blocked_reason = None
    has_block = blocked_reason is not None

    return {
        "has_items": has_items,
        "blocked": has_block,
        "blocked_reason": blocked_reason,
        "has_json": has_json,
        "length": len(html),
    }

def extract_items_from_html(html):
    """Извлекает объявления: сначала JSON, потом HTML. Поддержка любых городов Авито."""
    items = []

    # Способ 1: JSON (приоритет)
    for pattern_name, pattern in [
        ("__initialData__", re.compile(r'window\.__initialData__\s*=\s*({.+?});\s*</script>', re.DOTALL)),
        ("catalog", re.compile(r'"catalog":\s*\{[^}]*"items":\s*(\[[^\]]+\])', re.DOTALL)),
        ("items array", re.compile(r'"items":\s*(\[\s*\{[^\]]+\])', re.DOTALL)),
    ]:
        try:
            match = pattern.search(html)
            if match:
                raw = match.group(1)
                data = json.loads(raw) if not raw.startswith("[") else raw
                if isinstance(data, list):
                    lst = data
                elif isinstance(data, dict) and "items" in data:
                    lst = data["items"]
                else:
                    continue
                for item in lst:
                    if isinstance(item, dict):
                        o_id = str(item.get("id", ""))
                        if not o_id:
                            continue
                        title = item.get("title", "Объявление")[:200]
                        price = item.get("price") or item.get("priceFormatted") or "Цена не указана"
                        if isinstance(price, (int, float)):
                            price = f"{price:,.0f} ₽".replace(",", " ")
                        url = item.get("url", "")
                        if url and not url.startswith("http"):
                            url = "https://www.avito.ru" + (url if url.startswith("/") else "/" + url)
                        addr = ""
                        if isinstance(item.get("location"), dict):
                            addr = item["location"].get("name", "")
                        items.append({
                            "id": o_id,
                            "title": title,
                            "price": str(price)[:50],
                            "url": url or f"https://www.avito.ru{item.get('path', '')}",
                            "address": addr or "",
                        })
                if items:
                    logger.debug(f"Извлечено из JSON ({pattern_name}): {len(items)}")
                    return items
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"JSON parse {pattern_name}: {e}")
            continue

    # Способ 2: HTML — ссылки на объявления (любой город/категория)
    # В путях теперь бывают точки и проценты, расширяем допустимые символы.
    link_pattern = re.compile(r'href="(/(?:[a-z0-9._%-]+/)+(?:[a-z0-9._%-]+)_(\d+)(?:\?[^"]*)?)"', re.I)
    seen = set()
    for m in link_pattern.finditer(html):
        path, item_id = m.group(1), m.group(2)
        if item_id in seen:
            continue
        if "/item/" in path or path.count("/") < 2:
            continue
        seen.add(item_id)
        full_url = f"https://www.avito.ru{path}"
        idx = m.start()
        # Берём более широкий контекст вокруг ссылки, чтобы зацепить цену и адрес
        context = html[max(0, idx - 2000) : idx + 2000]
        title = "Объявление"
        t = re.search(r'>([^<]{10,120})<', context)
        if t:
            title = t.group(1).strip()[:100]
        price = "Цена не указана"
        # Сначала пытаемся вытащить по новому маркеру item-price-value
        p = re.search(r'data-marker="item-price-value"[^>]*>([^<]+₽)', context)
        if not p:
            p = re.search(r'(\d[\d\s]*)\s*₽', context)
        if p:
            price = p.group(1)
        address = ""
        # Адрес в блоке data-marker="item-address" — очищаем от тегов
        addr_block = re.search(r'data-marker="item-address".{0,600}</div>', context, re.S)
        if addr_block:
            raw = re.sub(r"<[^>]+>", " ", addr_block.group(0))
            raw = re.sub(r"\s+", " ", raw).strip()
            address = raw[:120]
        items.append({
            "id": item_id,
            "title": title,
            "price": price,
            "url": full_url,
            "address": address,
        })
    if items:
        logger.debug(f"Извлечено из HTML: {len(items)}")
    return items

def _random_delay(min_sec=2, max_sec=6):
    """Случайная пауза для снижения риска блокировки по IP."""
    delay = random.uniform(min_sec, max_sec)
    return delay


async def run_parser(send_new_ad_callback=None, send_removed_ad_callback=None):
    """Основной парсер: приоритет Selenium при отсутствии API ключей, rate limiting."""
    logger.info("🚀 Запуск парсера Авито")

    use_zenrows = ZENROWS_API_KEY and ZENROWS_API_KEY != "YOUR_API_KEY_HERE"
    use_scrapingbee = SCRAPINGBEE_API_KEY and SCRAPINGBEE_API_KEY != "YOUR_SCRAPINGBEE_KEY_HERE"
    if not use_zenrows and not use_scrapingbee:
        logger.info("ℹ️ Нет ZENROWS/SCRAPINGBEE — только Selenium. Рекомендуется задать ZENROWS_API_KEY в .env (свой прокси тогда не нужен).")

    filters = await run_in_thread(_db_get_active_filters)
    if not filters:
        logger.warning("⚠️ Нет фильтров!")
        return

    new_count = 0
    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=10)

    async with aiohttp.ClientSession(connector=connector) as session:
        for i, user_filter in enumerate(filters):
            if not user_filter.search_url:
                continue

            # Rate limiting: пауза перед каждым фильтром (кроме первого — небольшая)
            delay = _random_delay(3, 8) if i > 0 else _random_delay(1, 3)
            logger.info(f"⏳ Пауза {delay:.1f} с перед запросом")
            await asyncio.sleep(delay)

            search_url = user_filter.search_url.strip()
            mobile_url = convert_to_mobile(search_url)
            logger.info(f"🔍 Парсинг: {mobile_url}")
            district_filter = (getattr(user_filter, "district", "") or "").strip().lower()

            html = None
            # Приоритет: платные API (обход блокировок без своего прокси) → затем Selenium/прокси
            logger.info(f"[Парсер] Загрузка: ZenRows={use_zenrows}, ScrapingBee={use_scrapingbee}, иначе Selenium")
            if use_zenrows:
                html = await fetch_with_service(session, mobile_url, "zenrows")
                if not html and mobile_url != search_url:
                    html = await fetch_with_service(session, search_url, "zenrows")
            if not html and use_scrapingbee:
                html = await fetch_with_service(session, mobile_url, "scrapingbee")
            if not html:
                html = await run_in_thread(fetch_page_selenium, mobile_url)
                if not html and mobile_url != search_url:
                    html = await run_in_thread(fetch_page_selenium, search_url)
            if not html:
                logger.error("[Парсер] ❌ Все попытки загрузки страницы не удались")
                continue

            logger.info(f"[Парсер] Получено HTML: {len(html)} байт, первые 150 символов: {html[:150]!r}")
            content_info = check_content(html)
            logger.info(
                f"[Парсер] check_content: has_items={content_info['has_items']}, blocked={content_info['blocked']}"
                f"{', reason=' + content_info.get('blocked_reason', '') if content_info.get('blocked_reason') else ''}"
                f", has_json={content_info['has_json']}, length={content_info['length']}"
            )
            
            # Сохраняем для отладки
            debug_file = PHOTOS_DIR / f"debug_{user_filter.user_id}.html"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(html[:200000])
            
            if content_info["blocked"]:
                # Всё равно пробуем вытащить объявления — иногда на странице есть и блок, и данные
                items_try = extract_items_from_html(html)
                if items_try:
                    logger.info(f"[Парсер] Страница с признаком блокировки, но извлечено объявлений: {len(items_try)} — продолжаем")
                    items = items_try
                else:
                    reason = content_info.get("blocked_reason") or "капча/ограничение"
                    logger.error(f"[Парсер] 🚫 Блокировка по IP (причина: {reason}). В боте нажми «Проверить прокси» или смени прокси/VPN.")
                    logger.info(f"[Парсер] Фрагмент ответа: {html[:800]!r}")
                    continue
            else:
                items = extract_items_from_html(html)

            if not content_info["blocked"] and (not content_info["has_items"] and not content_info["has_json"]):
                logger.warning("[Парсер] ⚠️ В HTML нет маркеров объявлений (data-marker, items). Возможно другая верстка или пустая выдача.")
                logger.info(f"[Парсер] Фрагмент: {html[5000:5500]!r}" if len(html) > 5500 else f"[Парсер] HTML: {html[:1000]!r}")
                # Может быть страница редиректит или ждёт загрузки
                # Пробуем подождать и повторить
                await asyncio.sleep(5)
                continue

            logger.info(f"[Парсер] Извлечено объявлений: {len(items)}")
            if items:
                logger.info(f"[Парсер] Первое объявление: id={items[0].get('id')}, title={items[0].get('title', '')[:40]!r}")

            if not items:
                logger.warning("[Парсер] ⚠️ Список объявлений пуст после парсинга. Проверь структуру страницы.")
                continue
            
            src = getattr(user_filter, "source", None) or SOURCE_AVITO
            current_ids = []

            for item in items[:15]:
                try:
                    avito_id = item["id"]

                    # Фильтр по району: если задан district_filter, оставляем только те,
                    # где он содержится в адресе (из item["address"])
                    if district_filter:
                        addr = (item.get("address") or "").lower()
                        if district_filter not in addr:
                            continue

                    current_ids.append(avito_id)

                    ad = await run_in_thread(_db_get_ad_by_id, avito_id, src)

                    if not ad:
                        price_val = await extract_price_value(item["price"])
                        new_ad = await run_in_thread(
                            _db_add_new_ad,
                            user_filter.user_id,
                            avito_id,
                            item["title"],
                            item["price"],
                            price_val,
                            item["address"],
                            item.get("url", ""),
                            src,
                        )
                        # Обогащаем карточку подробностями (цена/адрес/описание/фото)
                        try:
                            new_ad = await enrich_avito_ad_details(session, new_ad) or new_ad
                        except Exception as e:
                            logger.error(f"⚠️ Ошибка enrich_avito_ad_details: {e}")
                        new_count += 1
                        logger.info(f"✨ Новое: {item['title'][:50]}")
                        if send_new_ad_callback:
                            await send_new_ad_callback(user_filter.user_id, new_ad)
                    else:
                        await run_in_thread(
                            _db_update_ad,
                            avito_id,
                            item["price"],
                            await extract_price_value(item["price"]),
                            src,
                        )
                except Exception as e:
                    logger.error(f"⚠️ Ошибка обработки: {e}")
                    continue

            removed = await run_in_thread(_db_mark_removed, user_filter.user_id, current_ids, src)
            if removed:
                logger.info(f"❌ Удалено: {len(removed)}")
                if send_removed_ad_callback:
                    for ad in removed:
                        await send_removed_ad_callback(user_filter.user_id, ad)
            
            # Небольшая пауза между фильтрами
            await asyncio.sleep(2)

    logger.info(f"✅ Готово. Новых: {new_count}")

async def parse_single_ad(user_id: int, ad_url: str, max_retries=3):
    """Парсит одно объявление"""
    logger.info(f"🔍 Парсим: {ad_url}")

    clean_url = ad_url.strip()
    mobile_url = convert_to_mobile(clean_url)
    avito_id = clean_url.rstrip("/").split("/")[-1].split("?")[0]

    existing = await run_in_thread(_db_get_ad_by_id, avito_id)
    if existing:
        logger.info(f"ℹ️ Уже в базе: {avito_id}")
        return existing

    html = None
    if ZENROWS_API_KEY != "YOUR_API_KEY_HERE":
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            html = await fetch_with_service(session, mobile_url, "zenrows")
            if not html:
                html = await fetch_with_service(session, clean_url, "zenrows")
    if not html:
        logger.info("🔄 Пробуем Selenium...")
        html = await run_in_thread(fetch_page_selenium, mobile_url)
    if not html:
        html = await run_in_thread(fetch_page_selenium, clean_url)

    if not html:
        raise RuntimeError("Не удалось получить страницу")

    content_info = check_content(html)
    if content_info['blocked']:
        raise RuntimeError("Блокировка")

    title_match = re.search(r'<h1[^>]*>([^<]+)', html)
    title = title_match.group(1).strip() if title_match else "Без названия"

    price_match = re.search(r'(\d[\d\s]*)\s*₽', html)
    price = price_match.group(0) if price_match else "0 ₽"

    addr_match = re.search(r'[Аа]дрес[^>]*>([^<]+)', html)
    address = addr_match.group(1).strip() if addr_match else ""

    new_ad = await run_in_thread(
        _db_add_new_ad,
        user_id,
        avito_id,
        title,
        price,
        await extract_price_value(price),
        address,
        clean_url,
    )

    logger.info(f"✨ Добавлено: {title[:40]}")
    return new_ad