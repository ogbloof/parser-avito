# cian_parser.py — парсер ЦИАН для объявлений о недвижимости
import asyncio
import re
import json
import random
import ssl
from pathlib import Path
from datetime import datetime
from urllib.parse import quote

import aiohttp
from logging_config import get_logger
from database import SessionLocal, UserFilter, Ad, run_in_thread
from selenium_fetcher import fetch_page_selenium
from config import ZENROWS_API_KEY, SCRAPINGBEE_API_KEY

logger = get_logger('parser')
_ssl = ssl.create_default_context()
_ssl.check_hostname = False
_ssl.verify_mode = ssl.CERT_NONE
SOURCE_CIAN = "cian"


def _db_get_active_cian_filters():
    db = SessionLocal()
    try:
        return db.query(UserFilter).filter(
            UserFilter.is_active == True,
            UserFilter.source == SOURCE_CIAN,
            UserFilter.search_url.isnot(None),
        ).all()
    finally:
        db.close()


def _db_get_ad_by_id(external_id, source=SOURCE_CIAN):
    db = SessionLocal()
    try:
        return db.query(Ad).filter(Ad.avito_id == external_id, Ad.source == source).first()
    finally:
        db.close()


def _db_add_new_ad(user_id, external_id, title, price, price_value, address, url):
    db = SessionLocal()
    try:
        ad = Ad(
            avito_id=external_id,
            user_id=user_id,
            source=SOURCE_CIAN,
            title=title,
            price=price,
            price_value=price_value,
            address=address or "",
            url=url,
            status="active",
        )
        db.add(ad)
        db.commit()
        db.refresh(ad)
        return ad
    finally:
        db.close()


def _db_update_ad(external_id, price, price_value):
    db = SessionLocal()
    try:
        ad = db.query(Ad).filter(Ad.avito_id == external_id, Ad.source == SOURCE_CIAN).first()
        if ad and ad.price != price:
            ad.price = price
            ad.price_value = price_value
            ad.updated_at = datetime.utcnow()
            db.commit()
        return ad
    finally:
        db.close()


def _db_mark_removed(user_id, current_ids):
    db = SessionLocal()
    try:
        q = db.query(Ad).filter(Ad.user_id == user_id, Ad.source == SOURCE_CIAN, Ad.status == "active")
        if current_ids:
            q = q.filter(~Ad.avito_id.in_(current_ids))
        old_ads = q.all()
        for ad in old_ads:
            ad.status = "removed"
            ad.removed_at = datetime.utcnow()
        db.commit()
        return old_ads
    finally:
        db.close()


def _extract_price_value(price_str):
    if not price_str:
        return None
    try:
        numbers = re.sub(r"[^\d]", "", str(price_str))
        return float(numbers) if numbers else None
    except Exception:
        return None


def _check_cian_blocked(html):
    low = html.lower()
    return any(
        x in low
        for x in [
            "доступ ограничен",
            "captcha",
            "капча",
            "blocked",
            "подозрительная активность",
        ]
    )


def extract_items_from_cian_html(html):
    """Извлекает объявления из страницы поиска ЦИАН (JSON или HTML)."""
    items = []

    # Попытка 1: JSON в script (__initialState__, __NUXT__, или data-offers)
    for pattern_name, pattern in [
        (
            "initialState",
            re.compile(r"__initialState__\s*=\s*({.+?});\s*</script>", re.DOTALL),
        ),
        (
            "offers",
            re.compile(r'"offers"\s*:\s*(\[\s*\{[^\]]+\])', re.DOTALL),
        ),
        (
            "data-offers",
            re.compile(r'data-offers=["\'](\[.*?\]|{.*?})["\']', re.DOTALL),
        ),
    ]:
        try:
            m = pattern.search(html)
            if not m:
                continue
            raw = m.group(1)
            data = json.loads(raw)
            if isinstance(data, list):
                offers = data
            elif isinstance(data, dict) and "offers" in data:
                offers = data["offers"]
            else:
                continue
            for o in offers:
                if not isinstance(o, dict):
                    continue
                oid = str(o.get("id") or o.get("offerId") or o.get("cid") or "")
                if not oid:
                    continue
                title = (o.get("title") or o.get("name") or "Объявление ЦИАН")[:200]
                price = o.get("price") or o.get("priceFormatted") or o.get("priceTotal") or "Цена не указана"
                if isinstance(price, (int, float)):
                    price = f"{price:,.0f} ₽".replace(",", " ")
                url = o.get("url") or o.get("link") or ""
                if url and not url.startswith("http"):
                    url = "https://www.cian.ru" + (url if url.startswith("/") else "/" + url)
                if not url:
                    url = f"https://www.cian.ru/sale/flat/{oid}/"
                address = (
                    o.get("address") or o.get("location", {}).get("address") if isinstance(o.get("location"), dict) else ""
                ) or ""
                items.append(
                    {
                        "id": oid,
                        "title": title,
                        "price": str(price)[:80],
                        "url": url,
                        "address": address[:200] if address else "",
                    }
                )
            if items:
                logger.debug(f"ЦИАН: извлечено из JSON ({pattern_name}): {len(items)}")
                return items
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"ЦИАН JSON {pattern_name}: {e}")
            continue

    # Попытка 2: HTML — ссылки на карточки объявлений
    # ЦИАН: /sale/flat/123456789/ или /rent/flat/... или offer/123
    link_re = re.compile(
        r'href="(https?://(?:www\.)?cian\.ru/(?:sale|rent)/[^"]+/(\d+)/?[^"]*)"',
        re.I,
    )
    seen = set()
    for m in link_re.finditer(html):
        full_url, oid = m.group(1), m.group(2)
        if oid in seen:
            continue
        seen.add(oid)
        ctx = html[max(0, m.start() - 300) : m.end() + 400]
        title = "Объявление ЦИАН"
        t = re.search(r'<[^>]+title[^>]*>([^<]{10,120})</', ctx, re.I)
        if not t:
            t = re.search(r'aria-label="([^"]{10,120})"', ctx)
        if t:
            title = t.group(1).strip()[:100]
        price = "Цена не указана"
        p = re.search(r"(\d[\d\s]*)\s*₽", ctx)
        if p:
            price = p.group(0)
        items.append(
            {
                "id": oid,
                "title": title,
                "price": price,
                "url": full_url,
                "address": "",
            }
        )
    if items:
        logger.debug(f"ЦИАН: извлечено из HTML: {len(items)}")
    return items


async def _fetch_cian_page(url: str) -> str | None:
    """Загрузка страницы ЦИАН: ZenRows → ScrapingBee → Selenium."""
    use_zenrows = ZENROWS_API_KEY and ZENROWS_API_KEY != "YOUR_API_KEY_HERE"
    use_sb = SCRAPINGBEE_API_KEY and SCRAPINGBEE_API_KEY != "YOUR_SCRAPINGBEE_KEY_HERE"

    if use_zenrows:
        params = {
            "apikey": ZENROWS_API_KEY,
            "url": url,
            "js_render": "true",
            "wait": "15000",
            "premium_proxy": "true",
            "antibot": "true",
            "proxy_country": "ru",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.zenrows.com/v1/",
                params=params,
                timeout=aiohttp.ClientTimeout(total=90),
                ssl=_ssl,
            ) as r:
                if r.status == 200:
                    text = await r.text()
                    if "captcha" not in text.lower() and "доступ ограничен" not in text.lower():
                        logger.info("ЦИАН: загружено через ZenRows, %s байт", len(text))
                        return text

    if use_sb:
        api_url = f"https://app.scrapingbee.com/api/v1/?api_key={SCRAPINGBEE_API_KEY}&url={quote(url)}&render_js=true&wait=15000"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=90), ssl=_ssl) as r:
                if r.status == 200:
                    text = await r.text()
                    logger.info("ЦИАН: загружено через ScrapingBee, %s байт", len(text))
                    return text

    return await run_in_thread(fetch_page_selenium, url)


async def run_cian_parser(send_new_callback=None, send_removed_callback=None):
    """Запуск парсера ЦИАН по активным фильтрам с source=cian. Возвращает (new_count, processed)."""
    filters = await run_in_thread(_db_get_active_cian_filters)
    num_filters = len(filters)
    if not filters:
        logger.info("ЦИАН: нет активных фильтров. Пользователь должен отправить ЦИАН и ссылку.")
        return 0, 0, 0

    new_count = 0
    processed = 0
    for i, uf in enumerate(filters):
        url = (uf.search_url or "").strip()
        if not url or "cian.ru" not in url:
            continue
        delay = random.uniform(3, 8) if i > 0 else random.uniform(1, 3)
        await asyncio.sleep(delay)
        logger.info(f"ЦИАН: парсинг {url[:60]}...")
        html = await _fetch_cian_page(url)
        if not html:
            logger.error("ЦИАН: не удалось загрузить страницу")
            continue
        if _check_cian_blocked(html):
            logger.warning("ЦИАН: обнаружена блокировка по IP")
            continue
        items = extract_items_from_cian_html(html)
        if not items:
            logger.warning("ЦИАН: объявления не найдены (проверь структуру страницы или ZenRows)")
            continue
        processed += 1
        logger.info(f"ЦИАН: найдено {len(items)} объявлений")
        current_ids = []
        for item in items[:15]:
            try:
                oid = item["id"]
                current_ids.append(oid)
                ad = await run_in_thread(_db_get_ad_by_id, oid)
                if not ad:
                    price_val = _extract_price_value(item["price"])
                    new_ad = await run_in_thread(
                        _db_add_new_ad,
                        uf.user_id,
                        oid,
                        item["title"],
                        item["price"],
                        price_val,
                        item.get("address", ""),
                        item.get("url", f"https://www.cian.ru/sale/flat/{oid}/"),
                    )
                    new_count += 1
                    if send_new_callback:
                        await send_new_callback(uf.user_id, new_ad)
                else:
                    await run_in_thread(
                        _db_update_ad,
                        oid,
                        item["price"],
                        _extract_price_value(item["price"]),
                    )
            except Exception as e:
                logger.error(f"ЦИАН: ошибка обработки {e}")
        removed = await run_in_thread(_db_mark_removed, uf.user_id, current_ids)
        if removed and send_removed_callback:
            for ad in removed:
                await send_removed_callback(uf.user_id, ad)
    logger.info(f"ЦИАН: готово. Проверено фильтров: {processed}/{num_filters}, новых: {new_count}")
    return new_count, processed, num_filters


async def parse_single_cian_ad(user_id: int, url: str):
    """Парсит одно объявление ЦИАН по ссылке."""
    url = url.strip()
    if "cian.ru" not in url:
        raise RuntimeError("Нужна ссылка на объявление ЦИАН")
    oid = re.search(r"/(\d+)/?", url)
    oid = oid.group(1) if oid else url.rstrip("/").split("/")[-1].split("?")[0]
    existing = await run_in_thread(_db_get_ad_by_id, oid)
    if existing:
        logger.info(f"ЦИАН: уже в базе {oid}")
        return existing
    html = await run_in_thread(fetch_page_selenium, url)
    if not html:
        raise RuntimeError("Не удалось загрузить страницу ЦИАН")
    if _check_cian_blocked(html):
        raise RuntimeError("Блокировка ЦИАН по IP")
    title = "Объявление ЦИАН"
    t = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if t:
        title = t.group(1).strip()[:200]
    price = "Цена не указана"
    p = re.search(r"(\d[\d\s]*)\s*₽", html)
    if p:
        price = p.group(0)
    addr = ""
    a = re.search(r'[Аа]дрес[^>]*>([^<]+)', html)
    if a:
        addr = a.group(1).strip()[:200]
    full_url = url if url.startswith("http") else f"https://www.cian.ru{url}"
    new_ad = await run_in_thread(
        _db_add_new_ad,
        user_id,
        oid,
        title,
        price,
        _extract_price_value(price),
        addr,
        full_url,
    )
    logger.info(f"ЦИАН: добавлено {title[:40]}")
    return new_ad
