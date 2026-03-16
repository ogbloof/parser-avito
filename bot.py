# bot.py
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import ErrorEvent
from aiogram.filters import ExceptionTypeFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
    WebAppInfo,
)
from sqlalchemy import or_
from database import (
    SessionLocal, User, UserFilter, Ad, Photo, init_db, run_in_thread,
    PIPELINE_STATUSES, STATUS_NEW, STATUS_CALLED, STATUS_NO_ANSWER,
    STATUS_MEETING_SET, STATUS_DEAL, STATUS_LOST, STATUS_CLOSED,
    get_or_create_user, grant_subscription, check_subscription,
)
from avito_parser import run_parser, parse_single_ad, test_one_avito_url, get_last_fetch_error, fetch_zenrows_diagnostic
from cian_parser import run_cian_parser, parse_single_cian_ad, test_one_cian_url, get_last_cian_fetch_error
from selenium_fetcher import check_proxy
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone
from logging_config import get_logger
from config import BOT_TOKEN, PARSER_INTERVAL_MINUTES, ADMIN_USER_IDS, API_PORT, WEBAPP_URL
import asyncio
import os
import re
from datetime import datetime, timedelta

logger = get_logger('bot')
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Пример: export BOT_TOKEN='123:abc' && python3 bot.py")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=timezone("Europe/Moscow"))


@dp.error(ExceptionTypeFilter(TelegramNetworkError))
async def on_telegram_network_error(event: ErrorEvent):
    """При обрыве связи с Telegram логируем и не роняем бота."""
    logger.warning(
        "Telegram API недоступен (Connection reset / таймаут). Проверь интернет и доступ к api.telegram.org: %s",
        event.exception,
    )

class SetupState(StatesGroup):
    url_input = State()
    city = State()
    district = State()
    min_price = State()
    max_price = State()

class PhoneEditState(StatesGroup):
    waiting_for_phone = State()

class AddAdState(StatesGroup):
    waiting_for_url = State()

class CianUrlState(StatesGroup):
    waiting_for_cian_url = State()

class NoteState(StatesGroup):
    waiting_for_note = State()


class NewAdsState(StatesGroup):
    browsing = State()

# Подписи статусов воронки для CRM
PIPELINE_LABELS = {
    STATUS_NEW: "🆕 Новое",
    "in_work": "🔄 В работе",
    STATUS_CALLED: "📞 Позвонил",
    STATUS_NO_ANSWER: "📵 Не дозвонился",
    STATUS_MEETING_SET: "📅 Показ назначен",
    STATUS_DEAL: "✅ Сделка",
    STATUS_LOST: "❌ Отказ",
    STATUS_CLOSED: "🏁 Закрыто",
}
PAGE_SIZE = 5

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


async def _require_subscription(message: types.Message) -> bool:
    return bool(message.from_user)


async def _require_subscription_cb(callback: types.CallbackQuery) -> bool:
    return bool(callback.from_user)


def _ad_card_text(ad, show_source=True):
    fav = "⭐" if getattr(ad, "is_favorite", False) else "☆"
    src = f"[{ad.source.upper()}] " if show_source and getattr(ad, "source", None) else ""
    status = getattr(ad, "status_pipeline", None) or "new"
    status_label = PIPELINE_LABELS.get(status, status)
    removed_label = "  |  ❌ Удалено на Авито" if getattr(ad, "status", None) == "removed" else ""
    notes_preview = ""
    if getattr(ad, "notes", None) and ad.notes:
        notes_preview = "\n📝 " + (ad.notes[:80] + "…" if len(ad.notes) > 80 else ad.notes)
    return (
        f"{fav} {src}<b>{ad.title}</b>\n"
        f"💰 {ad.price}  📍 {ad.address or '—'}\n"
        f"📞 {ad.custom_phone or 'Не задан'}  |  {status_label}{removed_label}{notes_preview}\n"
        f"[Ссылка]({ad.url})"
    )


def _ad_full_caption(ad, prefix: str | None = None) -> str:
    """Формирует «карточку» объявления для фото/сообщения."""
    fav = "⭐" if getattr(ad, "is_favorite", False) else "☆"
    src = getattr(ad, "source", "avito").upper()
    head_prefix = f"{prefix}\n" if prefix else ""
    status = getattr(ad, "status_pipeline", None) or "new"
    status_label = PIPELINE_LABELS.get(status, status)
    removed_label = " | ❌ Удалено на Авито" if getattr(ad, "status", None) == "removed" else ""
    title = ad.title or "Объявление"
    price = ad.price or "Цена не указана"
    address = ad.address or "—"
    phone = ad.custom_phone or "Не задан"
    desc = (ad.description or "").strip() if hasattr(ad, "description") else ""
    if desc:
        desc_short = desc[:600] + "…" if len(desc) > 600 else desc
        desc_block = f"\n\n{desc_short}"
    else:
        desc_block = ""
    notes_block = ""
    if getattr(ad, "notes", None):
        notes_block = "\n\n📝 " + (ad.notes[:400] + "…" if len(ad.notes) > 400 else ad.notes)
    lines = [
        f"{head_prefix}{fav} [{src}] <b>{title}</b>",
        f"💰 {price}",
        f"📍 {address}",
        f"📞 {phone}  |  {status_label}{removed_label}",
    ]
    body = "\n".join(lines) + desc_block + notes_block
    return body + f"\n\n[Открыть на сайте]({ad.url})"


def _single_ad_keyboard(ad) -> InlineKeyboardMarkup:
    """Клавиатура под одной карточкой объявления."""
    return _ads_list_keyboard([ad], page=0, total_pages=1, prefix="ad")


def _new_ad_keyboard(ad_id: int) -> InlineKeyboardMarkup:
    """Клавиатура под карточкой в режиме «Новые объявления» (минимум действий)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ В работу", callback_data=f"new:add:{ad_id}"),
                InlineKeyboardButton(text="⭐ В избранное", callback_data=f"new:fav:{ad_id}"),
                InlineKeyboardButton(text="⏭ Дальше", callback_data=f"new:skip:{ad_id}"),
            ],
        ]
    )


async def _send_new_ad_card(target, ad: Ad):
    """Отправляет одну карточку объявления в режиме «Новые объявления»."""
    caption = _ad_full_caption(ad, prefix="🆕 Новое объявление")
    kb = _new_ad_keyboard(ad.id)
    if ad.photos:
        for p in ad.photos:
            if os.path.exists(p.file_path):
                await target.answer_photo(
                    FSInputFile(p.file_path),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                return
    await target.answer(caption, parse_mode="HTML", reply_markup=kb)

async def _answer_with_retry(message: types.Message, text: str, max_retries: int = 3, **kwargs):
    """Отправка ответа в Telegram с повтором при обрыве сети (Connection reset by peer)."""
    for attempt in range(max_retries):
        try:
            await message.answer(text, **kwargs)
            return
        except TelegramNetworkError as e:
            logger.warning(f"Telegram API недоступен (попытка {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 + attempt * 2)
            else:
                logger.error("Не удалось отправить сообщение в Telegram. Проверь интернет/VPN и доступ к api.telegram.org")
                raise

def transliterate(text: str) -> str:
    """Простая транслитерация кириллицы в латиницу"""
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch', 'ъ': '',
        'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya', ' ': '-'
    }
    result = []
    for char in text.lower():
        result.append(translit_map.get(char, char))
    return ''.join(result)

async def notify_new(user_id, ad):
    """Push-уведомление о новом объявлении."""
    logger.info("Новое объявление %s сохранено для пользователя %s", getattr(ad, "avito_id", "?"), user_id)
    title = (ad.title or "Объявление")[:60]
    price = ad.price or "—"
    text = f"🆕 <b>Новое объявление — посмотрите</b>\n\n{title}\n💰 {price}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Смотреть", callback_data="new:open")],
    ])
    try:
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.warning("notify_new send failed: %s", e)

async def notify_removed(user_id, ad):
    try:
        await bot.send_message(user_id, f"❌ <b>Снято</b>\n{ad.title}\n{ad.price}", parse_mode="HTML")
    except: pass

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not message.from_user:
        return
    keyboard_rows = [
        [KeyboardButton(text="🔍 Запустить поиск"), KeyboardButton(text="🆕 Новые объявления")],
        [KeyboardButton(text="📂 Мои объекты"), KeyboardButton(text="📁 Избранные")],
        [KeyboardButton(text="⚙️ Настройки")],
    ]
    if WEBAPP_URL:
        url = WEBAPP_URL.rstrip("/")
        if url and not url.endswith("/"):
            url = url + "/"
        keyboard_rows.insert(0, [KeyboardButton(text="📱 Открыть приложение", web_app=WebAppInfo(url=url))])
    keyboard = ReplyKeyboardMarkup(keyboard=keyboard_rows, resize_keyboard=True)
    await message.answer(
        "👋 <b>Парсер для риелторов</b>\n\n"
        "— <b>Запустить поиск</b> — обновить базу по твоим фильтрам\n"
        "— <b>Новые объявления</b> — лента новых, по одному\n"
        "— <b>Мои объекты</b> — то, что ты взял в работу\n"
        "— <b>Избранные</b> — важные объекты (даже если сняты с Авито)\n\n"
        "Доп. настройки доступны командами: /set_url, /debug, /check_proxy и т.д.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    """Показывает свой Telegram user_id (нужен для оформления подписки)."""
    if message.from_user:
        await message.answer(f"Ваш ID: <code>{message.from_user.id}</code>\n\nОтправь его администратору при оплате подписки.", parse_mode="HTML")


# --- Admin commands ---

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    text = (
        "🔧 <b>Админ-панель</b>\n\n"
        "• /grant &lt;user_id&gt; [дней] — выдать подписку (по умолчанию 30 дней)\n"
        "  Пример: <code>/grant 123456789 30</code>\n\n"
        "• /users — список пользователей и подписок\n\n"
        "• /stats — статистика"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("grant"))
async def cmd_grant(message: types.Message):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer("Использование: /grant &lt;user_id&gt; [дней]", parse_mode="HTML")
        return
    try:
        uid = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else 30
    except ValueError:
        await message.answer("❌ user_id и days должны быть числами.")
        return
    if days < 1 or days > 365:
        await message.answer("❌ Дни: от 1 до 365.")
        return
    ok = await run_in_thread(grant_subscription, uid, days)
    if ok:
        await message.answer(f"✅ Подписка на {days} дней выдана пользователю {uid}.")
    else:
        await message.answer("❌ Ошибка при выдаче подписки.")


@dp.message(Command("users"))
async def cmd_users(message: types.Message):
    if not message.from_user or not _is_admin(message.from_user.id):
        return

    def list_users():
        db = SessionLocal()
        try:
            users = db.query(User).order_by(User.updated_at.desc()).limit(50).all()
            return [
                {
                    "user_id": u.user_id,
                    "sub_end": u.subscription_end.strftime("%d.%m.%Y") if u.subscription_end else "—",
                    "active": check_subscription(u.user_id),
                }
                for u in users
            ]
        finally:
            db.close()

    rows = await run_in_thread(list_users)
    if not rows:
        await message.answer("Пользователей пока нет.")
        return
    lines = [f"• {r['user_id']} — подписка до {r['sub_end']} {'✅' if r['active'] else '❌'}" for r in rows]
    text = "👥 <b>Пользователи</b> (последние 50):\n\n" + "\n".join(lines)
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not message.from_user or not _is_admin(message.from_user.id):
        return

    def get_stats():
        db = SessionLocal()
        try:
            from datetime import datetime
            now = datetime.utcnow()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            active_subs = sum(1 for u in db.query(User).all() if u.subscription_end and u.subscription_end >= now)
            new_ads_today = db.query(Ad).filter(Ad.created_at >= today_start).count()
            return active_subs, new_ads_today
        finally:
            db.close()

    active_subs, new_ads = await run_in_thread(get_stats)
    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"• Активных подписок: {active_subs}\n"
        f"• Новых объявлений сегодня: {new_ads}",
        parse_mode="HTML",
    )


@dp.message(Command("set_url"))
async def cmd_set_url(message: types.Message, state: FSMContext):
    if not await _require_subscription(message):
        return
    await message.answer(
        "🔗 <b>Фильтр Авито</b>\n\n"
        "1. Открой avito.ru в браузере\n"
        "2. Выбери город, цену, район\n"
        "3. Скопируй ссылку из адресной строки\n"
        "4. Вставь сюда\n\n"
        "Пример: https://www.avito.ru/moskva/kvartiry/prodam-ASgBAgICAUSSA8YQ?priceMin=5000000&priceMax=15000000",
        parse_mode="HTML",
    )
    await state.set_state(SetupState.url_input)

@dp.message(SetupState.url_input)
async def process_url_input(message: types.Message, state: FSMContext):
    url = message.text.strip()
    if "avito.ru" not in url or not url.startswith("http"):
        await message.answer("❌ Это не ссылка на Авито. Пришли ссылку вида https://www.avito.ru/...")
        return
    url = url.replace("m.avito.ru", "www.avito.ru")

    def save_filter(uid, search_url):
        db = SessionLocal()
        try:
            uf = db.query(UserFilter).filter(UserFilter.user_id == uid, UserFilter.source == "avito").first()
            if not uf:
                uf = UserFilter(user_id=uid, source="avito")
                db.add(uf)
            uf.search_url = search_url
            uf.is_active = True
            db.commit()
            logger.info(f"💾 БД: User {uid} | URL: {search_url[:60]}...")
        finally:
            db.close()
    
    await run_in_thread(save_filter, message.from_user.id, url)
    
    await message.answer(f"✅ <b>Ссылка сохранена!</b>\nТеперь отправь /start_search", parse_mode="HTML")
    await state.clear()

@dp.message(Command("manual"))
async def cmd_manual(message: types.Message, state: FSMContext):
    if not await _require_subscription(message):
        return
    await message.answer("🏙 Введите город (на русском, например Новосибирск):")
    await state.set_state(SetupState.city)


@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: types.Message):
    """Простое меню настроек с подсказками по фильтрам и ЦИАН."""
    if not await _require_subscription(message):
        return
    db = SessionLocal()
    try:
        uid = message.from_user.id if message.from_user else 0
        uf = db.query(UserFilter).filter(UserFilter.user_id == uid, UserFilter.source == "avito").first()
        uf_cian = db.query(UserFilter).filter(UserFilter.user_id == uid, UserFilter.source == "cian").first()
        if uf:
            current = (
                f"Текущий фильтр Авито:\n"
                f"— Город: {uf.city}\n"
                f"— Район: {uf.district or 'любой'}\n"
                f"— Цена: {uf.min_price or 0} – {uf.max_price or '∞'}\n\n"
            )
        else:
            current = "Фильтр Авито пока не настроен.\n\n"
    finally:
        db.close()

    cian_status = "настроен ✅" if (uf_cian and uf_cian.search_url) else "не настроен"
    cian_text = f"— ЦИАН: {cian_status}\n\n"

    await message.answer(
        "⚙️ <b>Настройки фильтров</b>\n\n"
        f"{current}{cian_text}"
        "<b>Авито</b> — /set_url (пришли ссылку) или /manual (город, район, цена)\n\n"
        "<b>ЦИАН</b> — /set_cian или напиши «ЦИАН», затем ссылку\n\n"
            "Если поиск не находит объявления — /check_parse (диагностика)",
        parse_mode="HTML",
    )


@dp.message(SetupState.city)
async def set_city(message: types.Message, state: FSMContext):
    city = message.text.strip()
    city_slug = transliterate(city)
    await state.update_data(city=city, city_slug=city_slug)
    logger.info(f"Город: {city} -> {city_slug}")
    await message.answer("🏘 Введите район (как на Авито, например Первомайский). Можно оставить пустым — тогда по всему городу:")
    await state.set_state(SetupState.district)


@dp.message(SetupState.district)
async def set_district(message: types.Message, state: FSMContext):
    district = (message.text or "").strip()
    await state.update_data(district=district)
    await message.answer("💰 Минимальная цена (цифрами, без пробелов):")
    await state.set_state(SetupState.min_price)

@dp.message(SetupState.min_price)
async def set_min(message: types.Message, state: FSMContext):
    try:
        val = int(message.text.replace(' ', ''))
        await state.update_data(min_price=val)
        await message.answer("💰 Максимальная цена (цифрами):")
        await state.set_state(SetupState.max_price)
    except:
        await message.answer("❌ Только цифры!")

@dp.message(SetupState.max_price)
async def set_max(message: types.Message, state: FSMContext):
    try:
        max_p = int(message.text.replace(' ', ''))
        data = await state.get_data()
        city_slug = data.get('city_slug', 'moskva')
        min_p = data.get('min_price', 0)
        district = data.get('district', '')
        
        url = f"https://www.avito.ru/{city_slug}/kvartiry/prodam?priceMin={min_p}&priceMax={max_p}"
        
        def save(uid, url, minv, maxv, district_value):
            db = SessionLocal()
            try:
                uf = db.query(UserFilter).filter(UserFilter.user_id == uid, UserFilter.source == "avito").first()
                if not uf:
                    uf = UserFilter(user_id=uid, source="avito")
                    db.add(uf)
                uf.min_price = minv
                uf.max_price = maxv
                uf.district = district_value
                uf.search_url = url
                uf.is_active = True
                db.commit()
                logger.info(f"💾 БД: User {uid} | {minv}-{maxv} | район='{district_value}' | {url[:50]}...")
            finally:
                db.close()
        
        await run_in_thread(save, message.from_user.id, url, data.get('min_price'), max_p, district)
        await message.answer(f"✅ <b>Сохранено!</b>\nСсылка: {url[:60]}...\nЗапускай /start_search", parse_mode="HTML")
        await state.clear()
    except:
        await message.answer("❌ Только цифры!")

def _count_user_filters(uid: int) -> int:
    """Количество активных фильтров (Авито + ЦИАН) у пользователя. По БД текущего инстанса."""
    db = SessionLocal()
    try:
        return db.query(UserFilter).filter(
            UserFilter.user_id == uid,
            UserFilter.is_active == True,
            UserFilter.search_url.isnot(None),
            UserFilter.search_url != "",
            UserFilter.source.in_(["avito", "cian"]),
        ).count()
    finally:
        db.close()


@dp.message(Command("start_search"))
@dp.message(F.text == "🔍 Запустить поиск")
async def cmd_search(message: types.Message):
    if not await _require_subscription(message):
        return
    # Проверяем фильтры по текущему пользователю (на Render у каждого инстанса своя БД)
    user_has_filters = (await run_in_thread(_count_user_filters, message.from_user.id)) > 0
    if not user_has_filters:
        await _answer_with_retry(
            message,
            "⚠️ Фильтры не настроены.\n\n"
            "• Авито: /set_url — пришли ссылку с avito.ru\n"
            "• ЦИАН: /set_cian или напиши «ЦИАН», затем ссылку с cian.ru\n\n"
            "После настройки снова нажми «🔍 Запустить поиск».",
        )
        return
    await _answer_with_retry(message, "🔄 Ищу по Авито и ЦИАН...")
    avito_res = await run_parser(notify_new, notify_removed) or (0, 0, 0)
    cian_res = await run_cian_parser(notify_new, notify_removed) or (0, 0, 0)
    if len(avito_res) == 2:
        avito_res = (avito_res[0], avito_res[1], avito_res[1])
    if len(cian_res) == 2:
        cian_res = (cian_res[0], cian_res[1], cian_res[1])
    avito_new, avito_ok, avito_total = avito_res[0], avito_res[1], avito_res[2]
    cian_new, cian_ok, cian_total = cian_res[0], cian_res[1], cian_res[2]
    total_new = avito_new + cian_new
    no_success = avito_ok == 0 and cian_ok == 0
    if no_success:
        err_avito = get_last_fetch_error()
        err_cian = get_last_cian_fetch_error()
        detail = " / ".join(filter(None, [err_avito, err_cian]))
        if not detail:
            await _answer_with_retry(message, "🔄 Проверяю ZenRows...")
            detail = await fetch_zenrows_diagnostic() or "добавь SCRAPERAPI_API_KEY (1000 бесплатно/мес) или ZENROWS_API_KEY в Render → Environment"
        hint = ""
        if "AUTH004" in detail or "usage exceeded" in detail.lower() or "quota" in detail.lower():
            hint = "\n\n💡 Исчерпан лимит запросов ZenRows. Обнови тариф на zenrows.com или дождись сброса квоты (обычно раз в месяц)."
        await _answer_with_retry(
            message,
            "⚠️ Не удалось загрузить страницы (Авито/ЦИАН).\n\n"
            f"Ошибка: {detail}{hint}\n\nПопробуй позже.",
        )
        return
    if total_new > 0:
        await _answer_with_retry(message, f"✅ Найдено новых: {total_new}. Смотри «🆕 Новые объявления».")
    else:
        await _answer_with_retry(
            message,
            "✅ Готово. Новых объявлений пока нет. Загляни позже.",
        )


@dp.message(Command("new_ads"))
@dp.message(F.text == "🆕 Новые объявления")
async def cmd_new_ads(message: types.Message, state: FSMContext):
    """Показывает новые объявления по одному, как ленту."""
    if not await _require_subscription(message):
        return
    def get_new(uid):
        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(hours=24)
            return (
                db.query(Ad)
                .filter(
                    Ad.user_id == uid,
                    Ad.status == "active",
                    or_(Ad.status_pipeline.is_(None), Ad.status_pipeline == STATUS_NEW),
                    Ad.created_at >= cutoff,
                )
                .order_by(Ad.created_at.desc())
                .all()
            )
        finally:
            db.close()

    ads = await run_in_thread(get_new, message.from_user.id)
    if not ads:
        await message.answer("Сейчас нет новых объявлений. Нажми «🔍 Запустить поиск», а потом вернись сюда.")
        return

    ids = [ad.id for ad in ads]
    await state.update_data(new_ads_ids=ids, new_ads_pos=0)
    await state.set_state(NewAdsState.browsing)
    await message.answer(f"Новых объявлений: {len(ids)}. Показываю по одному.")
    await _send_new_ad_card(message, ads[0])

def _ads_list_keyboard(ads, page=0, total_pages=1, prefix="my"):
    rows = []
    for ad in ads:
        rows.append([
            InlineKeyboardButton(text="⭐" if ad.is_favorite else "☆", callback_data=f"fav:{ad.id}"),
            InlineKeyboardButton(text="📞 Тел.", callback_data=f"phone:{ad.id}"),
            InlineKeyboardButton(text="📝", callback_data=f"add_note:{ad.id}"),
        ])
        rows.append([
            InlineKeyboardButton(text="📞 Позв.", callback_data=f"set_status:{ad.id}:{STATUS_CALLED}"),
            InlineKeyboardButton(text="📵 Нет", callback_data=f"set_status:{ad.id}:{STATUS_NO_ANSWER}"),
            InlineKeyboardButton(text="📅 Показ", callback_data=f"set_status:{ad.id}:{STATUS_MEETING_SET}"),
            InlineKeyboardButton(text="✅ Сделка", callback_data=f"set_status:{ad.id}:{STATUS_DEAL}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"{prefix}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"{prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(Command("my_ads"))
@dp.message(F.text == "📂 Мои объявления")
@dp.message(F.text == "📂 Мои объекты")
async def cmd_ads(message: types.Message):
    if not await _require_subscription(message):
        return
    def get(uid):
        db = SessionLocal()
        try:
            # Показываем и активные, и удалённые на Авито — чтобы сохранялась история объектов
            return db.query(Ad).filter(Ad.user_id == uid).order_by(Ad.updated_at.desc()).all()
        finally:
            db.close()

    ads = await run_in_thread(get, message.from_user.id)
    if not ads:
        await message.answer("📭 Пусто. Запусти поиск или добавь объявление по ссылке.")
        return

    # Для наглядности показываем объекты как отдельные карточки (до PAGE_SIZE штук)
    page = 0
    total_pages = (len(ads) + PAGE_SIZE - 1) // PAGE_SIZE or 1
    chunk = ads[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    await message.answer(f"📂 <b>Мои объекты</b> (показано {len(chunk)} из {len(ads)})", parse_mode="HTML")
    for ad in chunk:
        caption = _ad_full_caption(ad)
        kb = _single_ad_keyboard(ad)
        if ad.photos:
            sent = False
            for p in ad.photos:
                if os.path.exists(p.file_path):
                    await message.answer_photo(
                        FSInputFile(p.file_path),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    sent = True
                    break
            if sent:
                continue
        await message.answer(caption, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("my:"))
async def cb_my_ads_page(callback: types.CallbackQuery):
    if not await _require_subscription_cb(callback):
        return
    page = int(callback.data.split(":", 1)[1])

    def get(uid):
        db = SessionLocal()
        try:
            return db.query(Ad).filter(Ad.user_id == uid).order_by(Ad.updated_at.desc()).all()
        finally:
            db.close()

    ads = await run_in_thread(get, callback.from_user.id)
    if not ads:
        await callback.answer("Список пуст")
        return
    total_pages = (len(ads) + PAGE_SIZE - 1) // PAGE_SIZE or 1
    page = min(max(0, page), total_pages - 1)
    chunk = ads[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    await callback.message.edit_text(
        f"📂 <b>Мои объекты</b> (страница {page + 1}/{total_pages})",
        parse_mode="HTML",
    )
    for ad in chunk:
        caption = _ad_full_caption(ad)
        kb = _single_ad_keyboard(ad)
        if ad.photos:
            sent = False
            for p in ad.photos:
                if os.path.exists(p.file_path):
                    await callback.message.answer_photo(
                        FSInputFile(p.file_path),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    sent = True
                    break
            if sent:
                continue
        await callback.message.answer(caption, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@dp.message(Command("set_cian"))
@dp.message(F.text == "🔗 ЦИАН")
@dp.message(F.text.lower() == "циан")
async def cmd_cian_setup(message: types.Message, state: FSMContext):
    if not await _require_subscription(message):
        return
    await message.answer(
        "🔗 <b>Фильтр ЦИАН</b>\n\n"
        "1. Открой cian.ru в браузере\n"
        "2. Выбери город, цену, тип недвижимости\n"
        "3. Скопируй ссылку из адресной строки\n"
        "4. Вставь сюда\n\n"
        "Пример: https://www.cian.ru/cat.php?deal_type=sale&offer_type=flat&region=4593",
        parse_mode="HTML",
    )
    await state.set_state(CianUrlState.waiting_for_cian_url)

@dp.message(CianUrlState.waiting_for_cian_url)
async def process_cian_url(message: types.Message, state: FSMContext):
    url = message.text.strip()
    if "cian.ru" not in url:
        await message.answer("❌ Нужна ссылка с cian.ru. Попробуй ещё раз.")
        return

    def save_cian_filter(uid, search_url):
        db = SessionLocal()
        try:
            uf = db.query(UserFilter).filter(UserFilter.user_id == uid, UserFilter.source == "cian").first()
            if not uf:
                uf = UserFilter(user_id=uid, source="cian")
                db.add(uf)
            uf.search_url = search_url
            uf.is_active = True
            db.commit()
        finally:
            db.close()

    await run_in_thread(save_cian_filter, message.from_user.id, url)
    await message.answer("✅ Поиск ЦИАН сохранён. Запусти «Запустить поиск» для обновления.")
    await state.clear()

@dp.message(F.text == "➕ Добавить объявление")
async def cmd_add_ad_start(message: types.Message, state: FSMContext):
    if not await _require_subscription(message):
        return
    await message.answer(
        "Отправь ссылку на объявление Авито или ЦИАН.\n\n"
        "Авито: https://www.avito.ru/...\n"
        "ЦИАН: https://www.cian.ru/sale/flat/123456789/"
    )
    await state.set_state(AddAdState.waiting_for_url)

@dp.message(AddAdState.waiting_for_url)
async def cmd_add_ad_process_url(message: types.Message, state: FSMContext):
    url = message.text.strip()
    if not url.startswith("http"):
        await message.answer("❌ Пришли полную ссылку на объявление (Авито или ЦИАН).")
        return

    await message.answer("🔄 Сохраняю объявление...")
    try:
        if "cian.ru" in url:
            ad = await parse_single_cian_ad(message.from_user.id, url)
        elif "avito.ru" in url:
            ad = await parse_single_ad(message.from_user.id, url)
        else:
            await message.answer("❌ Поддерживаются только ссылки Авито и ЦИАН.")
            await state.clear()
            return
    except RuntimeError as e:
        await message.answer(
            "🚫 Страница с блокировкой по IP или ошибка загрузки. Попробуй позже или с другого интернета."
        )
        await state.clear()
        return
    except Exception as e:
        logger.error(f"Ошибка при добавлении объявления: {e}", exc_info=True)
        await message.answer("⚠️ Не получилось сохранить объявление. Попробуй ещё раз позже.")
        await state.clear()
        return

    src = getattr(ad, "source", "avito")
    text = (
        f"✅ <b>Объявление сохранено!</b> [{src.upper()}]\n"
        f"{ad.title}\n"
        f"{ad.price}\n"
        f"{ad.address or '—'}\n"
        f"[Ссылка]({ad.url})"
    )
    await message.answer(text, parse_mode="HTML")
    await state.clear()

@dp.message(F.text == "📁 Избранные")
async def cmd_favorites(message: types.Message):
    if not await _require_subscription(message):
        return
    def get_fav(uid):
        db = SessionLocal()
        try:
            return (
                db.query(Ad)
                # Избранное показываем даже если объявление уже снято с Авито
                .filter(Ad.user_id == uid, Ad.is_favorite == True)
                .order_by(Ad.updated_at.desc())
                .all()
            )
        finally:
            db.close()

    ads = await run_in_thread(get_fav, message.from_user.id)
    if not ads:
        await message.answer("📭 В избранном пусто. В «Мои объекты» нажми ⭐ у объявления.")
        return

    # Показываем избранные как отдельные карточки (с фото, описанием и действиями)
    for ad in ads[:15]:
        caption = _ad_full_caption(ad)
        kb = _single_ad_keyboard(ad)
        if ad.photos:
            sent = False
            for p in ad.photos:
                if os.path.exists(p.file_path):
                    await message.answer_photo(
                        FSInputFile(p.file_path),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    sent = True
                    break
            if sent:
                continue
        await message.answer(caption, parse_mode="HTML", reply_markup=kb)

@dp.message(F.text == "🔄 Объекты в работе")
async def cmd_work(message: types.Message):
    """Объявления с активными статусами (не закрыты/сделка/отказ)."""
    if not await _require_subscription(message):
        return
    def get_work(uid):
        db = SessionLocal()
        try:
            return (
                db.query(Ad)
                .filter(
                    Ad.user_id == uid,
                    Ad.status == "active",
                    or_(
                        Ad.status_pipeline.is_(None),
                        Ad.status_pipeline.notin_([STATUS_CLOSED, STATUS_DEAL, STATUS_LOST]),
                    ),
                )
                .order_by(Ad.updated_at.desc())
                .all()
            )
        finally:
            db.close()

    ads = await run_in_thread(get_work, message.from_user.id)
    if not ads:
        await message.answer("📭 Нет объектов в работе. Открой «Мои объекты» и смени статус у объявлений.")
        return

    total_pages = (len(ads) + PAGE_SIZE - 1) // PAGE_SIZE or 1
    page = 0
    chunk = ads[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    text = f"🔄 <b>Объекты в работе</b> (стр. {page + 1}/{total_pages})\n\n"
    for ad in chunk:
        text += _ad_card_text(ad) + "\n\n"
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=_ads_list_keyboard(chunk, page, total_pages, "work_p"),
    )

@dp.callback_query(F.data.startswith("work_p:"))
async def cb_work_page(callback: types.CallbackQuery):
    if not await _require_subscription_cb(callback):
        return
    page = int(callback.data.split(":", 1)[1])

    def get_work(uid):
        db = SessionLocal()
        try:
            return (
                db.query(Ad)
                .filter(
                    Ad.user_id == uid,
                    Ad.status == "active",
                    or_(
                        Ad.status_pipeline.is_(None),
                        Ad.status_pipeline.notin_([STATUS_CLOSED, STATUS_DEAL, STATUS_LOST]),
                    ),
                )
                .order_by(Ad.updated_at.desc())
                .all()
            )
        finally:
            db.close()

    ads = await run_in_thread(get_work, callback.from_user.id)
    if not ads:
        await callback.answer("Список пуст")
        return
    total_pages = (len(ads) + PAGE_SIZE - 1) // PAGE_SIZE or 1
    page = min(max(0, page), total_pages - 1)
    chunk = ads[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    text = f"🔄 <b>Объекты в работе</b> (стр. {page + 1}/{total_pages})\n\n"
    for ad in chunk:
        text += _ad_card_text(ad) + "\n\n"
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_ads_list_keyboard(chunk, page, total_pages, "work_p"),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_status:"))
async def cb_set_status(callback: types.CallbackQuery):
    if not await _require_subscription_cb(callback):
        return
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка")
        return
    ad_id, status = int(parts[1]), parts[2]
    from datetime import datetime

    def set_status(ad_pk, new_status):
        db = SessionLocal()
        try:
            ad = db.query(Ad).filter(Ad.id == ad_pk, Ad.user_id == callback.from_user.id).first()
            if not ad:
                return None
            ad.status_pipeline = new_status
            if new_status == STATUS_CALLED:
                ad.last_contact_at = datetime.utcnow()
            db.commit()
            return PIPELINE_LABELS.get(new_status, new_status)
        finally:
            db.close()

    label = await run_in_thread(set_status, ad_id, status)
    if label is None:
        await callback.answer("Объявление не найдено", show_alert=True)
        return
    await callback.answer(f"Статус: {label}")

@dp.callback_query(F.data.startswith("add_note:"))
async def cb_add_note_start(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_subscription_cb(callback):
        return
    ad_id = int(callback.data.split(":", 1)[1])
    await state.set_state(NoteState.waiting_for_note)
    await state.update_data(ad_id=ad_id)
    await callback.message.answer("📝 Введите заметку к объявлению (одним сообщением):")
    await callback.answer()

@dp.message(NoteState.waiting_for_note)
async def set_note_value(message: types.Message, state: FSMContext):
    note = message.text.strip()
    data = await state.get_data()
    ad_id = data.get("ad_id")
    if not ad_id:
        await message.answer("Ошибка. Выберите объявление снова.")
        await state.clear()
        return
    from datetime import datetime

    def append_note(ad_pk, text):
        db = SessionLocal()
        try:
            ad = db.query(Ad).filter(Ad.id == ad_pk, Ad.user_id == message.from_user.id).first()
            if not ad:
                return False
            prefix = f"[{datetime.utcnow().strftime('%d.%m %H:%M')}] "
            ad.notes = (ad.notes + "\n" + prefix + text) if ad.notes else (prefix + text)
            db.commit()
            return True
        finally:
            db.close()

    ok = await run_in_thread(append_note, ad_id, note)
    if ok:
        await message.answer("✅ Заметка добавлена.")
    else:
        await message.answer("⚠️ Объявление не найдено.")
    await state.clear()

@dp.callback_query(F.data.startswith("fav:"))
async def cb_toggle_favorite(callback: types.CallbackQuery):
    if not await _require_subscription_cb(callback):
        return
    ad_id = int(callback.data.split(":", 1)[1])

    def toggle(ad_pk):
        db = SessionLocal()
        try:
            ad = db.query(Ad).filter(Ad.id == ad_pk).first()
            if not ad:
                return None
            ad.is_favorite = not (ad.is_favorite or False)
            db.commit()
            return ad.is_favorite
        finally:
            db.close()

    is_fav = await run_in_thread(toggle, ad_id)
    if is_fav is None:
        await callback.answer("Объявление не найдено", show_alert=True)
        return

    await callback.answer("Добавлено в избранное" if is_fav else "Убрано из избранного")

@dp.callback_query(F.data.startswith("phone:"))
async def cb_edit_phone(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_subscription_cb(callback):
        return
    ad_id = int(callback.data.split(":", 1)[1])
    await state.set_state(PhoneEditState.waiting_for_phone)
    await state.update_data(ad_id=ad_id)
    await callback.message.answer("Отправь номер телефона для этого объявления в одном сообщении.")
    await callback.answer()


@dp.callback_query(F.data.startswith("new:"))
async def cb_new_ads(callback: types.CallbackQuery, state: FSMContext):
    """Обработка кнопок под карточкой в режиме «Новые объявления» и «Смотреть» из push."""
    if not await _require_subscription_cb(callback):
        return
    if callback.data == "new:open":
        # Кнопка «Смотреть» из push-уведомления — открываем ленту новых объявлений
        def get_new(uid):
            db = SessionLocal()
            try:
                cutoff = datetime.utcnow() - timedelta(hours=24)
                return (
                    db.query(Ad)
                    .filter(
                        Ad.user_id == uid,
                        Ad.status == "active",
                        or_(Ad.status_pipeline.is_(None), Ad.status_pipeline == STATUS_NEW),
                        Ad.created_at >= cutoff,
                    )
                    .order_by(Ad.created_at.desc())
                    .all()
                )
            finally:
                db.close()
        ads = await run_in_thread(get_new, callback.from_user.id)
        if not ads:
            await callback.answer("Сейчас нет новых объявлений.", show_alert=True)
            return
        ids = [ad.id for ad in ads]
        await state.update_data(new_ads_ids=ids, new_ads_pos=0)
        await state.set_state(NewAdsState.browsing)
        await callback.message.answer(f"Новых объявлений: {len(ids)}. Показываю по одному.")
        await _send_new_ad_card(callback.message, ads[0])
        await callback.answer()
        return
    parts = callback.data.split(":")
    action = parts[1]

    if len(parts) < 3:
        await callback.answer()
        return

    ad_id = int(parts[2])

    # Обновляем БД в зависимости от действия
    def apply_action(uid, pk, act):
        db = SessionLocal()
        try:
            ad = db.query(Ad).filter(Ad.id == pk, Ad.user_id == uid).first()
            if not ad:
                return False
            if act == "add":
                ad.status_pipeline = "in_work"
            elif act == "fav":
                ad.is_favorite = True
            elif act == "skip":
                ad.status_pipeline = STATUS_CLOSED
            db.commit()
            return True
        finally:
            db.close()

    if action in {"add", "fav", "skip"}:
        await run_in_thread(apply_action, callback.from_user.id, ad_id, action)

    data = await state.get_data()
    ids = data.get("new_ads_ids", [])
    pos = data.get("new_ads_pos", 0)

    # Переходим к следующему объявлению
    try:
        idx = ids.index(ad_id)
    except ValueError:
        idx = pos
    next_pos = idx + 1

    if next_pos >= len(ids):
        await callback.message.answer("Новых объявлений больше нет.")
        await state.clear()
        await callback.answer()
        return

    await state.update_data(new_ads_ids=ids, new_ads_pos=next_pos)

    def get_by_pk(pk):
        db = SessionLocal()
        try:
            return db.query(Ad).filter(Ad.id == pk).first()
        finally:
            db.close()

    next_ad = await run_in_thread(get_by_pk, ids[next_pos])
    if not next_ad:
        await callback.answer()
        return

    await _send_new_ad_card(callback.message, next_ad)
    await callback.answer()

@dp.message(PhoneEditState.waiting_for_phone)
async def set_phone_value(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    data = await state.get_data()
    ad_id = data.get("ad_id")
    if not ad_id:
        await message.answer("Что-то пошло не так, попробуй ещё раз открыть объявление.")
        await state.clear()
        return

    def update_phone(ad_pk, phone_value):
        db = SessionLocal()
        try:
            ad = db.query(Ad).filter(Ad.id == ad_pk).first()
            if not ad:
                return False
            ad.custom_phone = phone_value
            db.commit()
            return True
        finally:
            db.close()

    ok = await run_in_thread(update_phone, ad_id, phone)
    if ok:
        await message.answer("✅ Номер телефона сохранён.")
    else:
        await message.answer("⚠️ Объявление не найдено, попробуй ещё раз через «Мои объявления».")
    await state.clear()

@dp.message(F.text == "🔍 Проверить прокси")
async def cmd_check_proxy(message: types.Message):
    """Проверяет, с какого IP идёт трафик и открывает ли Авито (через текущий прокси из .env)."""
    await _answer_with_retry(message, "🔄 Проверяю прокси и доступ к Авито (30–60 сек)...")
    try:
        ip_result, avito_result = await run_in_thread(check_proxy)
        # Экранируем для HTML, чтобы < > из ответа не ломали parse_mode
        from html import escape
        ip_safe = escape(str(ip_result))
        avito_safe = escape(str(avito_result))
        text = (
            f"<b>Проверка прокси</b>\n\n"
            f"🌐 <b>IP (через прокси):</b> <code>{ip_safe}</code>\n\n"
            f"📄 <b>Авито:</b> {avito_safe}\n\n"
        )
        if "блок" in avito_result.lower():
            text += (
                "Если видишь «блок» — этот IP Авито режет. Варианты:\n"
                "• Другой прокси (резидентский/мобильный)\n"
                "• Другой формат в .env: AVITO_PROXY=host:port:user:pass или host:port@user:pass\n"
                "• Мобильный интернет с телефона без прокси"
            )
        if "ошибка загрузки" in avito_result.lower():
            text += (
                "\n\n💡 Если в консоли видишь <i>Invalid proxy server credentials supplied</i> — прокси отклонил логин/пароль "
                "(проверь логин и пароль в .env) или разрешает только одно подключение (не запускай поиск параллельно)."
            )
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.exception("Проверка прокси: %s", e)
        await _answer_with_retry(
            message,
            f"⚠️ Ошибка проверки: {e}. Посмотри логи в консоли.",
        )


@dp.message(Command("check_proxies"))
async def cmd_check_proxies(message: types.Message):
    """
    Прогоняет список прокси по очереди и показывает, какой IP и режет ли Авито.
    Использование:
      1) Отправь сообщение со списком (каждый прокси с новой строки), затем ответь на него командой /check_proxies
      2) Или: /check_proxies <список прокси через пробелы/переводы строк>
    """
    raw_list = ""
    if message.reply_to_message and getattr(message.reply_to_message, "text", None):
        raw_list = message.reply_to_message.text or ""
    else:
        # после команды может быть payload
        raw_list = (message.text or "").replace("/check_proxies", "", 1).strip()

    proxies = [p.strip() for p in re.split(r"[\s\r\n]+", raw_list) if p.strip()]
    if not proxies:
        await message.answer(
            "Пришли список прокси (каждый с новой строки) и ответь на него командой /check_proxies.",
        )
        return

    await _answer_with_retry(message, f"🔄 Проверяю {len(proxies)} прокси (может занять несколько минут)...")
    from html import escape

    results = []
    for idx, proxy in enumerate(proxies, start=1):
        # Чтобы прокси не ругался на параллельные соединения — строго по одному
        ip_result, avito_result = await run_in_thread(check_proxy, proxy)
        ip_safe = escape(str(ip_result))
        avito_safe = escape(str(avito_result))
        proxy_safe = escape(proxy)
        results.append(
            f"{idx}) <code>{proxy_safe}</code>\n"
            f"   🌐 <code>{ip_safe}</code>\n"
            f"   📄 {avito_safe}\n"
        )

        # Мягкая задержка, чтобы прокси/Avito меньше нервничали
        await asyncio.sleep(0.5)

    await message.answer(
        "<b>Проверка списка прокси</b>\n\n" + "\n".join(results),
        parse_mode="HTML",
    )

@dp.message(Command("check_parse"))
async def cmd_check_parse(message: types.Message):
    """Диагностика: почему не находит объявления. Показывает фильтры и тестовый запрос."""
    if not message.from_user:
        return
    uid = message.from_user.id
    db = SessionLocal()
    try:
        avito = db.query(UserFilter).filter(UserFilter.user_id == uid, UserFilter.source == "avito", UserFilter.search_url.isnot(None)).all()
        cian = db.query(UserFilter).filter(UserFilter.user_id == uid, UserFilter.source == "cian", UserFilter.search_url.isnot(None)).all()
    finally:
        db.close()

    lines = ["🔬 <b>Проверка парсинга</b>\n"]
    lines.append(f"• Фильтров Авито: {len(avito)}")
    lines.append(f"• Фильтров ЦИАН: {len(cian)}")
    if not avito and not cian:
        await message.answer(
            "⚠️ Нет ни одного фильтра.\n\n"
            "Авито: /set_url и пришли ссылку с avito.ru\n"
            "ЦИАН: /set_cian или напиши «ЦИАН», затем ссылку с cian.ru",
            parse_mode="HTML",
        )
        return

    if avito:
        url = (avito[0].search_url or "").strip()
        if url:
            await message.answer("⏳ Загружаю страницу Авито (15–30 сек)...")
            try:
                res = await test_one_avito_url(url)
                lines.append(f"\n<b>Тест Авито</b> (первый фильтр):")
                lines.append(f"URL: {res['url_short']}")
                if res.get("error"):
                    lines.append(f"❌ {res['error']}")
                else:
                    lines.append(f"HTML: {res['html_len']} байт")
                    if res["blocked"]:
                        lines.append(f"🚫 Блокировка: {res.get('blocked_reason') or 'да'}")
                    else:
                        lines.append(f"Объявлений извлечено: {res['items_count']}")
                        if res["items_count"] == 0:
                            lines.append("\nЕсли 0 — Авито сменил вёрстку или ссылка ведёт на пустой поиск. Проверь ссылку в браузере.")
            except Exception as e:
                lines.append(f"\n❌ Ошибка: {e}")

    if cian:
        url_cian = (cian[0].search_url or "").strip()
        if url_cian:
            lines.append("\n⏳ Загружаю страницу ЦИАН...")
            try:
                res = await test_one_cian_url(url_cian)
                lines.append(f"\n<b>Тест ЦИАН</b> (первый фильтр):")
                lines.append(f"URL: {res['url_short']}")
                if res.get("error"):
                    lines.append(f"❌ {res['error']}")
                else:
                    lines.append(f"HTML: {res['html_len']} байт")
                    if res["blocked"]:
                        lines.append("🚫 Блокировка (капча/ограничение)")
                    else:
                        lines.append(f"Объявлений извлечено: {res['items_count']}")
                        if res["items_count"] == 0:
                            lines.append("\nЕсли 0 — ЦИАН сменил вёрстку или ссылка не на поиск. Используй ссылку вида cian.ru/cat.php?... или страницу с результатами поиска.")
            except Exception as e:
                lines.append(f"\n❌ ЦИАН ошибка: {e}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("debug"))
@dp.message(F.text == "🛠 Отладка / настройки")
@dp.message(F.text == "🛠 Отладка")
async def cmd_debug(message: types.Message):
    """Показывает, что реально в базе"""
    db = SessionLocal()
    try:
        uf = db.query(UserFilter).filter(UserFilter.user_id == message.from_user.id, UserFilter.source == "avito").first()
        if uf:
            text = (
                f"🔍 <b>Настройки в БД:</b>\n"
                f"ID: {uf.user_id}\n"
                f"Город: {uf.city}\n"
                f"Район: {uf.district or 'любой'}\n"
                f"Цена: {uf.min_price} - {uf.max_price}\n"
                f"URL: {uf.search_url}\n"
                f"Активно: {uf.is_active}"
            )
            await message.answer(text, parse_mode="HTML")
        else:
            await message.answer("❌ В базе ничего нет. Настрой через /set_url")
    finally:
        db.close()

async def scheduled():
    await run_parser(notify_new, notify_removed)
    await run_cian_parser(notify_new, notify_removed)

async def main():
    init_db()
    scheduler.add_job(scheduled, 'interval', minutes=PARSER_INTERVAL_MINUTES)
    scheduler.start()

    from pathlib import Path
    from aiohttp import web
    from api import create_app
    webapp_dir = Path(__file__).resolve().parent / "webapp"
    api_app = create_app(webapp_dir)
    runner = web.AppRunner(api_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    logger.info("🌐 API на порту %s", API_PORT)

    logger.info("🤖 Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logger.info("PORT=%s API_PORT=%s WEBAPP_URL=%s", os.environ.get("PORT"), API_PORT, "OK" if WEBAPP_URL else "не задан")
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception("Критическая ошибка при запуске: %s", e)
        raise