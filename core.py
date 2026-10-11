"""Settings, limits, encryption aur chhote helpers. Secrets sirf environment se."""
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time

from cryptography.fernet import Fernet
from telegram import Bot

import db

MAIN_TOKEN = os.environ["MAIN_BOT_TOKEN"]
MAIN_ID = int(MAIN_TOKEN.split(":")[0])
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = (os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL_ID") or 0)
STORAGE_CHANNEL = int(os.environ.get("STORAGE_CHANNEL_ID") or 0)  # optional backup channel (sirf main bot)
FORCE_JOIN = os.environ.get("FORCE_JOIN_CHANNEL", "").strip()  # jaise @mychannel
TZ_OFFSET = int(os.environ.get("TZ_OFFSET_MINUTES") or 330)  # India = +5:30

MAIN_USERNAME = ""  # startup par set hota hai

# ===== Limits & Prices (yahin se badlo) =====
GB = 1024 ** 3
FREE_STORAGE = 10 * GB
PREMIUM_STORAGE = 90 * GB
FREE_CLONES = 1
PREMIUM_CLONES = 3
FREE_COLLECTIONS = 2
TRIAL_DAYS = 3
TRASH_DAYS = 7
PLANS = {30: 50, 90: 120, 365: 350}  # din -> Stars
# ============================================


def _make_fernet(k):
    k = k.strip().strip('"').strip("'")
    try:
        return Fernet(k.encode())
    except Exception:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(k.encode()).digest()))


_fernet = _make_fernet(os.environ["ENCRYPT_KEY"])
_HK = hashlib.sha256(("hash:" + os.environ["ENCRYPT_KEY"]).encode()).digest()

_tasks = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


def enc(t):
    return _fernet.encrypt(t.encode()).decode()


def dec(t):
    return _fernet.decrypt(t.encode()).decode()


def secret_for(bid):
    return hmac.new(WEBHOOK_SECRET.encode(), str(bid).encode(), hashlib.sha256).hexdigest()[:32]


def secret_ok(bid, given):
    return hmac.compare_digest(secret_for(bid), given or "")


def hash_secret(scope, value):
    """Password / PIN kabhi plain text me store nahi hota."""
    return hmac.new(_HK, f"{scope}|{value}".encode(), hashlib.sha256).hexdigest()


def same(a, b):
    return hmac.compare_digest(a or "", b or "")


def new_code():
    return secrets.token_urlsafe(8)


def fdate(ts):
    return time.strftime("%d %b %Y", time.gmtime(ts + TZ_OFFSET * 60))


def ftime(ts):
    return time.strftime("%d %b, %I:%M %p", time.gmtime(ts + TZ_OFFSET * 60))


# ---------- bot cache ----------
_bots = {}


async def get_bot(token):
    b = _bots.get(token)
    if b is None:
        b = Bot(token)
        await b.initialize()
        _bots[token] = b
    return b


async def drop_bot(token):
    b = _bots.pop(token, None)
    if b:
        try:
            await b.shutdown()
        except Exception:
            pass


async def main_bot():
    return await get_bot(MAIN_TOKEN)


async def bot_for(bot_id):
    if bot_id == MAIN_ID:
        return await main_bot()
    c = await db.one("SELECT token FROM clones WHERE id=?", bot_id)
    return await get_bot(dec(c["token"])) if c else None


# ---------- clone cache ----------
_cc = {}


async def get_clone(cid):
    c = _cc.get(cid)
    if c and time.time() - c[0] < 20:
        return c[1]
    r = await db.one("SELECT * FROM clones WHERE id=?", cid)
    _cc[cid] = (time.time(), r)
    return r


def inval(cid):
    _cc.pop(cid, None)


# ---------- premium ----------
_pc = {}


async def premium_until(owner_id):
    c = _pc.get(owner_id)
    if c and time.time() - c[0] < 30:
        return c[1]
    r = await db.one("SELECT expiry FROM owners WHERE id=?", owner_id)
    e = r["expiry"] if r else 0
    _pc[owner_id] = (time.time(), e)
    return e


async def is_premium(owner_id):
    if owner_id == OWNER_ID:
        return True
    return await premium_until(owner_id) > time.time()


async def add_premium(owner_id, days):
    now = int(time.time())
    r = await db.one("SELECT expiry FROM owners WHERE id=?", owner_id)
    base = max(now, r["expiry"] if r else 0)
    new = base + days * 86400
    await db.ex("UPDATE owners SET expiry=?, remind3=0, remind1=0 WHERE id=?", new, owner_id)
    _pc.pop(owner_id, None)
    return new


async def ensure_owner(u):
    await db.ex(
        "INSERT INTO owners(id,name,username,joined) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
        u.id, u.full_name, u.username, int(time.time()),
    )


# ---------- messaging helpers ----------
async def notify(uid, text, **kw):
    try:
        await (await main_bot()).send_message(uid, text, **kw)
        return True
    except Exception:
        return False


async def log(text):
    if LOG_CHANNEL:
        await notify(LOG_CHANNEL, text)
