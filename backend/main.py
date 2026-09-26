"""FastAPI app: session middleware, core API routes, static web/ mounted last."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend import db, eventlog
from backend.settings import WEB_DIR, settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.seed_demo_member()
    eventlog.log("startup", features=settings.features.as_dict())
    yield
    eventlog.log("shutdown")


app = FastAPI(title="SpeedMart", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.env.session_secret, same_site="lax", https_only=False)


@app.get("/api/health")
def health():
    # vision_age_ms is -1 until shelf_state (S1.2) receives snapshots; serial is false until S1.4.
    return {"ok": True, "vision_age_ms": -1, "serial": False}


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
