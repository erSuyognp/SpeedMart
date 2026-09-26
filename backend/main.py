"""FastAPI app: session middleware, core API routes, static web/ mounted last."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend import admin, db, eventlog, routes_api, shelf_state, store, ws
from backend.settings import WEB_DIR, settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.seed_demo_member()
    ws.manager.bind(asyncio.get_running_loop())
    eventlog.log("startup", features=settings.features.as_dict())
    timeout_task = asyncio.create_task(store.timeout_task())
    yield
    timeout_task.cancel()
    with suppress(asyncio.CancelledError):
        await timeout_task
    eventlog.log("shutdown")
    ws.manager.bind(None)


app = FastAPI(title="SpeedMart", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.env.session_secret, same_site="lax", https_only=False)
app.include_router(shelf_state.router)
app.include_router(routes_api.router)
app.include_router(admin.public)
app.include_router(admin.router)
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
    # vision_age_ms is -1 until the first snapshot arrives; serial is false until S1.4.
    return {"ok": True, "vision_age_ms": shelf_state.last_snapshot_age_ms(), "serial": False}


@app.get("/api/config/public")
def config_public():
    return {
        "features": settings.features.as_dict(),
        "store_name": settings.store["name"],
        "currency": settings.store["currency"],
    }


@app.get("/api/catalog")
def catalog():
    return {"skus": [{"sku": s.sku, "name": s.name, "price_usd": s.price_usd} for s in settings.skus.values()]}


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
