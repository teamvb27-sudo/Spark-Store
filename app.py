"""FastAPI webhook server (Render). Docs/openapi band, secrets sirf environment se."""
import asyncio
import logging
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram.error import InvalidToken, TelegramError

import core
import db
import mainbot
import store

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
_locks = defaultdict(asyncio.Lock)
ALLOWED = ["message", "callback_query"]


def sender(d):
    for k in ("message", "callback_query", "pre_checkout_query"):
        if k in d:
            return d[k].get("from", {}).get("id")
    return None


async def process(bid, data):
    try:
        async with _locks[(bid, sender(data))]:
            if bid == "main":
                await mainbot.handle(data)
            else:
                c = await core.get_clone(int(bid))
                if c:
                    await store.handle_clone(c, data)
    except Exception:
        log.exception("update failed")  # users ko error details kabhi nahi bhejte
    if len(_locks) > 20000:
        for k in [k for k, v in _locks.items() if not v.locked()]:
            _locks.pop(k, None)


# ---------- background jobs ----------
async def reminders():
    now = int(time.time())
    for o in await db.q("SELECT id,expiry,remind3,remind1 FROM owners WHERE expiry>0 AND id!=?", core.OWNER_ID):
        left = o["expiry"] - now
        if left <= 0:
            if o["remind1"] < 2:
                await core.notify(o["id"], "⏰ Tumhara Premium khatam ho gaya. Storage limit 10 GB par aa gayi aur premium features band hain. "
                                           "Renew: Main Bot me 💎 Premium")
                await db.ex("UPDATE owners SET remind1=2 WHERE id=?", o["id"])
        elif left <= 86400:
            if o["remind1"] < 1:
                await core.notify(o["id"], "⏳ Premium 1 din me khatam hone wala hai. Renew: 💎 Premium")
                await db.ex("UPDATE owners SET remind1=1, remind3=1 WHERE id=?", o["id"])
        elif left <= 3 * 86400 and not o["remind3"]:
            await core.notify(o["id"], "⏳ Premium 3 din me khatam hone wala hai. Renew: 💎 Premium")
            await db.ex("UPDATE owners SET remind3=1 WHERE id=?", o["id"])


async def health():
    for c in await db.q("SELECT id,token,owner_id,username FROM clones WHERE status='active'"):
        try:
            b = await core.get_bot(core.dec(c["token"]))
            await b.get_me()
        except InvalidToken:
            await db.ex("UPDATE clones SET status='dead' WHERE id=?", c["id"])
            core.inval(c["id"])
            await core.notify(c["owner_id"], f"⚠️ @{c['username']} ka token invalid ho gaya (revoke hua?). Bot band. Naya bot banao.")
            await core.log(f"💀 Clone dead: @{c['username']} ({c['id']})")
        except TelegramError:
            pass
        await asyncio.sleep(0.3)


async def cleanup():
    now = int(time.time())
    await db.ex("DELETE FROM opens WHERE ts<?", now - 90 * 86400)
    await db.ex("DELETE FROM jobs WHERE run_at<?", now - 86400)


async def resync_webhooks():
    """Render URL badle to saare clones ka webhook apne aap naya ho jaye."""
    if await db.kv_get("base_url", "") == core.BASE_URL:
        return
    for c in await db.q("SELECT id,token FROM clones WHERE status IN ('active','paused')"):
        try:
            b = await core.get_bot(core.dec(c["token"]))
            await b.set_webhook(url=f"{core.BASE_URL}/wh/{c['id']}", secret_token=core.secret_for(c["id"]), allowed_updates=ALLOWED)
        except Exception:
            log.warning("webhook resync failed for %s", c["id"])
        await asyncio.sleep(0.2)
    await db.kv_set("base_url", core.BASE_URL)


async def minutely():
    while True:
        await asyncio.sleep(60)
        try:
            await store.run_jobs()
        except Exception:
            log.exception("jobs")


async def hourly():
    await asyncio.sleep(90)
    while True:
        for fn in (reminders, health, store.purge_trash, cleanup):
            try:
                await fn()
            except Exception:
                log.exception(fn.__name__)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app):
    await db.init()
    main = await core.main_bot()
    me = await main.get_me()
    core.MAIN_USERNAME = me.username
    await main.set_webhook(url=f"{core.BASE_URL}/wh/main", secret_token=core.secret_for("main"),
                           allowed_updates=["message", "callback_query", "pre_checkout_query"])
    log.info("Spark Store @%s ready", me.username)
    tasks = [asyncio.create_task(minutely()), asyncio.create_task(hourly()), asyncio.create_task(resync_webhooks())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health_route():
    return {"ok": True}


@app.post("/wh/{bid}")
async def webhook(bid: str, request: Request):
    if not (bid == "main" or re.fullmatch(r"\d{1,15}", bid)):
        return Response(status_code=404)
    if not core.secret_ok(bid, request.headers.get("x-telegram-bot-api-secret-token")):
        return Response(status_code=403)
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)
    core.spawn(process(bid, data))
    return {"ok": True}
