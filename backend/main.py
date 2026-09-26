"""FastAPI app: session middleware, core API routes, static web/ mounted last."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend import (admin, auth_passkeys, bank, db, disputes, eventlog, evidence, intent, kiosk, members,
                     returns, review, routes_api, serial_bridge, shelf_state, store, voice, ws)
from backend.settings import WEB_DIR, settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.seed_demo_member()
    bank.backfill()  # demo bank (8.15): existing members and the demo member get their opening balance
    ws.manager.bind(asyncio.get_running_loop())
    eventlog.log("startup", features=settings.features.as_dict())
    timeout_task = asyncio.create_task(store.timeout_task())
    cleanup_task = asyncio.create_task(disputes.cleanup_task())  # F20: evidence goes when the visit is over
    # 8.14: does the dispute review model take images? Off the loop, so a slow model never delays startup.
    probe_task = asyncio.create_task(asyncio.to_thread(review.probe)) if review.available() else None
    serial_bridge.start()  # no-op unless hardware_leds is on
    yield
    serial_bridge.stop()
    for task in (timeout_task, cleanup_task, probe_task):
        if task is None:
            continue
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    eventlog.log("shutdown")
    ws.manager.bind(None)


app = FastAPI(title="SpeedMart", lifespan=lifespan)
# Secure cookies only behind the HTTPS tunnel (S3.1). uvicorn runs with --proxy-headers so the tunnel's
# X-Forwarded-Proto makes requests https. An http PUBLIC_ORIGIN (LAN mode) keeps plain cookies working.
def session_https_only(s=settings) -> bool:
    return s.features.https_tunnel and s.env.public_origin.startswith("https://")


SESSION_HTTPS_ONLY = session_https_only()


class PlainHttpCookieFix:
    """Drop "Secure" from the session cookie on requests that arrived over plain http.

    SessionMiddleware's https_only is one global flag, but the demo is reached two ways at once: phones
    come through the https tunnel, while the admin page, the kiosk tablet and scripts/e2e_sim.py talk to
    http://localhost:8000 or http://<laptop-ip>:8000 over the LAN. With https_only on, those plain-http
    clients are handed a Secure cookie that no client will ever send back, so they log in and are
    immediately logged out again.

    Deciding per request keeps the tunnel's cookie Secure (uvicorn --proxy-headers turns the tunnel's
    X-Forwarded-Proto into scheme "https") and only relaxes the LAN clients, which were never protected
    by the flag in the first place.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("scheme") == "https":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = []
                for key, value in message["headers"]:
                    if key.lower() == b"set-cookie" and value.lower().startswith(b"session="):
                        value = b"; ".join(p for p in value.split(b"; ") if p.lower() != b"secure")
                    headers.append((key, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SessionMiddleware, secret_key=settings.env.session_secret, same_site="lax",
                   https_only=SESSION_HTTPS_ONLY)
if SESSION_HTTPS_ONLY:  # added last, so it wraps SessionMiddleware and sees the cookie on the way out
    app.add_middleware(PlainHttpCookieFix)
app.include_router(shelf_state.router)
app.include_router(routes_api.router)
app.include_router(returns.router)
app.include_router(disputes.router)
app.include_router(evidence.router)  # POST /internal/clips (8.2)
app.include_router(members.router)
app.include_router(auth_passkeys.router)
app.include_router(admin.public)
app.include_router(admin.router)
app.include_router(disputes.admin_router)
app.include_router(review.router)  # /admin/disputes/{id}/approve | keep (8.3)
app.include_router(intent.router)
app.include_router(kiosk.router)
app.include_router(voice.router)
app.include_router(ws.router)


@app.exception_handler(store.StoreError)
async def store_error_handler(request: Request, e: store.StoreError):
    return routes_api.store_error_response(e)


@app.exception_handler(routes_api.ApiError)
async def api_error_handler(request: Request, e: routes_api.ApiError):
    return routes_api.error_response(e.status, e.code, e.message)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, e: RequestValidationError):
    fields = ", ".join(".".join(str(x) for x in err["loc"][1:]) or "body" for err in e.errors())
    return routes_api.error_response(422, "bad_request", f"Invalid or missing: {fields}.")


@app.get("/api/health")
def health():
    # vision_age_ms is -1 until the first snapshot arrives; serial is the real port state.
    return {"ok": True, "vision_age_ms": shelf_state.last_snapshot_age_ms(), "serial": serial_bridge.is_connected()}


@app.get("/api/config/public")
def config_public():
    return {
        "features": settings.features.as_dict(),
        "store_name": settings.store["name"],
        "currency": settings.store["currency"],
    }


@app.get("/api/catalog")
def catalog():
    """SKUs with prices, plus the shelf map's bays: the printed card number (bay id 0 is card 1), the product
    and how many of it are on the shelf now (null before the first vision snapshot)."""
    counts = shelf_state.shelf_counts() if shelf_state.has_snapshot() else None
    return {
        "skus": [{"sku": s.sku, "name": s.name, "price_usd": s.price_usd} for s in settings.skus.values()],
        "bays": [{"bay": b.id, "card": b.id + 1, "sku": b.sku, "name": settings.skus[b.sku].name,
                  "on_shelf": counts.get(b.sku, 0) if counts is not None else None}
                 for b in sorted(settings.bays, key=lambda b: b.id)],
    }


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
