# api.py — HTTP API для Telegram Mini App
import hmac
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote

from aiohttp import web
from aiohttp.web import Request, Response
from datetime import datetime, timedelta

from database import (
    SessionLocal, Ad, Photo, run_in_thread,
    STATUS_NEW, STATUS_CLOSED, STATUS_DEAL, STATUS_LOST,
    check_subscription, get_or_create_user,
)
from logging_config import get_logger

logger = get_logger("api")


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """
    Валидация initData от Telegram Web App.
    Возвращает распарсенные данные (с user) или None при ошибке.
    """
    if not init_data or not bot_token:
        return None
    try:
        parsed = parse_qs(init_data, keep_blank_values=True)
        hash_val = parsed.get("hash", [None])[0]
        if not hash_val:
            return None
        data_check_str = "\n".join(
            f"{k}={v[0]}" for k, v in sorted(parsed.items()) if k != "hash"
        )
        secret_key = hmac.new(
            b"WebAppData", bot_token.encode(), hashlib.sha256
        ).digest()
        computed = hmac.new(
            secret_key, data_check_str.encode(), hashlib.sha256
        ).hexdigest()
        if computed != hash_val:
            return None
        auth_date = int(parsed.get("auth_date", [0])[0])
        if datetime.utcnow().timestamp() - auth_date > 86400:
            return None
        user_str = parsed.get("user", [None])[0]
        if not user_str:
            return None
        user = json.loads(unquote(user_str))
        return {"user": user, "auth_date": auth_date}
    except Exception as e:
        logger.warning("initData validation error: %s", e)
        return None


def _ad_to_dict(ad) -> dict:
    photo_url = None
    if ad.photos:
        for p in ad.photos:
            if p.is_main and os.path.exists(p.file_path):
                photo_url = f"/api/photos/{ad.id}"
                break
        if not photo_url and ad.photos and os.path.exists(ad.photos[0].file_path):
            photo_url = f"/api/photos/{ad.id}"
    return {
        "id": ad.id,
        "title": ad.title or "",
        "price": ad.price or "",
        "address": ad.address or "",
        "url": ad.url or "",
        "source": getattr(ad, "source", "avito"),
        "status_pipeline": getattr(ad, "status_pipeline", STATUS_NEW) or STATUS_NEW,
        "is_favorite": bool(getattr(ad, "is_favorite", False)),
        "photo_url": photo_url,
        "removed": getattr(ad, "status", None) == "removed",
    }


async def _get_user_from_request(request: Request) -> tuple[int | None, bool]:
    init_data = request.headers.get("X-Telegram-Init-Data") or request.query.get("initData", "")
    from config import BOT_TOKEN
    validated = validate_init_data(init_data, BOT_TOKEN or "")
    if not validated:
        return None, False
    user = validated.get("user", {})
    user_id = user.get("id")
    if not user_id:
        return None, False
    await run_in_thread(get_or_create_user, user_id)
    active = await run_in_thread(check_subscription, user_id)
    return user_id, active


async def api_user(request: Request) -> Response:
    user_id, active = await _get_user_from_request(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"user_id": user_id, "subscription_active": active})


async def api_ads_new(request: Request) -> Response:
    user_id, active = await _get_user_from_request(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not active:
        return web.json_response({"error": "subscription_required"}, status=403)

    def fetch():
        from sqlalchemy import or_
        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(hours=24)
            ads = (
                db.query(Ad)
                .filter(
                    Ad.user_id == user_id,
                    Ad.status == "active",
                    or_(Ad.status_pipeline.is_(None), Ad.status_pipeline == STATUS_NEW),
                    Ad.created_at >= cutoff,
                )
                .order_by(Ad.created_at.desc())
                .limit(100)
                .all()
            )
            return [_ad_to_dict(a) for a in ads]
        finally:
            db.close()

    ads = await run_in_thread(fetch)
    return web.json_response({"ads": ads})


async def api_ads_mine(request: Request) -> Response:
    user_id, active = await _get_user_from_request(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not active:
        return web.json_response({"error": "subscription_required"}, status=403)

    def fetch():
        from sqlalchemy import or_
        db = SessionLocal()
        try:
            ads = (
                db.query(Ad)
                .filter(
                    Ad.user_id == user_id,
                    Ad.status == "active",
                    or_(
                        Ad.status_pipeline.is_(None),
                        Ad.status_pipeline.notin_([STATUS_CLOSED, STATUS_DEAL, STATUS_LOST]),
                    ),
                )
                .order_by(Ad.updated_at.desc())
                .limit(100)
                .all()
            )
            return [_ad_to_dict(a) for a in ads]
        finally:
            db.close()

    ads = await run_in_thread(fetch)
    return web.json_response({"ads": ads})


async def api_ads_favorite(request: Request) -> Response:
    user_id, active = await _get_user_from_request(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not active:
        return web.json_response({"error": "subscription_required"}, status=403)

    def fetch():
        db = SessionLocal()
        try:
            ads = (
                db.query(Ad)
                .filter(Ad.user_id == user_id, Ad.is_favorite == True)
                .order_by(Ad.updated_at.desc())
                .limit(100)
                .all()
            )
            return [_ad_to_dict(a) for a in ads]
        finally:
            db.close()

    ads = await run_in_thread(fetch)
    return web.json_response({"ads": ads})


async def api_ads_status(request: Request) -> Response:
    user_id, active = await _get_user_from_request(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not active:
        return web.json_response({"error": "subscription_required"}, status=403)

    ad_id = int(request.match_info.get("id", 0))
    if not ad_id:
        return web.json_response({"error": "bad_request"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    action = body.get("action")
    if action not in ("in_work", "favorite", "unfavorite", "skip"):
        return web.json_response({"error": "invalid_action"}, status=400)

    def apply():
        db = SessionLocal()
        try:
            ad = db.query(Ad).filter(Ad.id == ad_id, Ad.user_id == user_id).first()
            if not ad:
                return False
            if action == "in_work":
                ad.status_pipeline = "in_work"
            elif action == "skip":
                ad.status_pipeline = STATUS_CLOSED
            elif action == "favorite":
                ad.is_favorite = True
            elif action == "unfavorite":
                ad.is_favorite = False
            db.commit()
            return True
        finally:
            db.close()

    ok = await run_in_thread(apply)
    if not ok:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"ok": True})


async def api_photo(request: Request) -> Response:
    ad_id = int(request.match_info.get("id", 0))
    if not ad_id:
        raise web.HTTPNotFound()

    def get_photo_path():
        db = SessionLocal()
        try:
            ad = db.query(Ad).filter(Ad.id == ad_id).first()
            if not ad or not ad.photos:
                return None
            for p in ad.photos:
                if p.is_main and os.path.exists(p.file_path):
                    return p.file_path
            if os.path.exists(ad.photos[0].file_path):
                return ad.photos[0].file_path
            return None
        finally:
            db.close()

    path = await run_in_thread(get_photo_path)
    if not path:
        raise web.HTTPNotFound()
    return web.FileResponse(path)


@web.middleware
async def cors_middleware(request: Request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
            }
        )
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    return resp


def create_app(webapp_dir: Path | None = None) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/user", api_user)
    app.router.add_get("/api/ads/new", api_ads_new)
    app.router.add_get("/api/ads/mine", api_ads_mine)
    app.router.add_get("/api/ads/favorite", api_ads_favorite)
    app.router.add_post("/api/ads/{id}/status", api_ads_status)
    app.router.add_get("/api/photos/{id}", api_photo)
    if webapp_dir and webapp_dir.exists():
        async def serve_webapp_index(request):
            index_path = webapp_dir / "index.html"
            if index_path.exists():
                return web.FileResponse(index_path)
            raise web.HTTPNotFound()
        app.router.add_get("/webapp/", serve_webapp_index)
        app.router.add_get("/webapp", serve_webapp_index)
        app.router.add_static("/webapp", webapp_dir, name="webapp")
    return app
