"""Turso (libSQL) HTTP client. Koi secret yahan nahi, sab environment se aata hai."""
import os

import httpx

_URL = os.environ["TURSO_DATABASE_URL"].replace("libsql://", "https://").rstrip("/")
_TOKEN = os.environ["TURSO_AUTH_TOKEN"]
_client = None


def _http():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=25, headers={"Authorization": f"Bearer {_TOKEN}"})
    return _client


def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def _val(c):
    t = c.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(c["value"])
    if t == "float":
        return float(c["value"])
    return c.get("value")


async def _exec(stmts):
    reqs = [{"type": "execute", "stmt": {"sql": s, "args": [_arg(a) for a in args]}} for s, args in stmts]
    reqs.append({"type": "close"})
    r = await _http().post(f"{_URL}/v2/pipeline", json={"requests": reqs})
    r.raise_for_status()
    rows_out, counts = [], []
    for res in r.json()["results"][:-1]:
        if res["type"] == "error":
            raise RuntimeError(res["error"]["message"])
        rs = res["response"]["result"]
        cols = [c["name"] for c in rs["cols"]]
        rows_out.append([dict(zip(cols, [_val(x) for x in row])) for row in rs["rows"]])
        counts.append(rs.get("affected_row_count", 0) or 0)
    return rows_out, counts


async def batch(stmts):
    """stmts = [(sql, (args...)), ...] -> har statement ke rows (list of dict)"""
    return (await _exec(stmts))[0]


async def q(sql, *args):
    return (await batch([(sql, args)]))[0]


async def one(sql, *args):
    r = await q(sql, *args)
    return r[0] if r else None


async def ex(sql, *args):
    await q(sql, *args)


async def run(sql, *args):
    """Statement chalao aur kitni rows badli wo ginti lautao."""
    return (await _exec([(sql, args)]))[1][0]


async def kv_get(key, default=None):
    r = await one("SELECT value FROM kv WHERE key=?", key)
    return r["value"] if r else default


async def kv_set(key, value):
    await ex("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", key, str(value))


SCHEMA = [
    "CREATE TABLE IF NOT EXISTS owners(id INTEGER PRIMARY KEY, name TEXT, username TEXT, banned INTEGER DEFAULT 0, trial_used INTEGER DEFAULT 0, expiry INTEGER DEFAULT 0, remind3 INTEGER DEFAULT 0, remind1 INTEGER DEFAULT 0, joined INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS clones(id INTEGER PRIMARY KEY, token TEXT, username TEXT, name TEXT, owner_id INTEGER, status TEXT DEFAULT 'active', created INTEGER, pub INTEGER DEFAULT 0, welcome TEXT, nobrand INTEGER DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS i_clones_owner ON clones(owner_id)",
    "CREATE TABLE IF NOT EXISTS admins(bot_id INTEGER, user_id INTEGER, PRIMARY KEY(bot_id,user_id))",
    "CREATE TABLE IF NOT EXISTS ubots(bot_id INTEGER, user_id INTEGER, name TEXT, username TEXT, first_seen INTEGER, last_seen INTEGER, banned INTEGER DEFAULT 0, pin TEXT, PRIMARY KEY(bot_id,user_id))",
    "CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, bot_id INTEGER, uploader INTEGER, owner_id INTEGER, name TEXT, kind TEXT, mime TEXT, size INTEGER DEFAULT 0, file_id TEXT, unique_id TEXT, caption TEXT, backup_msg INTEGER, fav INTEGER DEFAULT 0, tags TEXT DEFAULT '', vault INTEGER DEFAULT 0, trashed INTEGER DEFAULT 0, created INTEGER)",
    "CREATE INDEX IF NOT EXISTS i_files_user ON files(bot_id,uploader,trashed)",
    "CREATE INDEX IF NOT EXISTS i_files_owner ON files(owner_id)",
    "CREATE INDEX IF NOT EXISTS i_files_uid ON files(unique_id)",
    "CREATE TABLE IF NOT EXISTS links(code TEXT PRIMARY KEY, bot_id INTEGER, owner_id INTEGER, uploader INTEGER, kind TEXT, pw TEXT, expires INTEGER DEFAULT 0, max_opens INTEGER DEFAULT 0, opens INTEGER DEFAULT 0, protect INTEGER DEFAULT 0, autodel INTEGER DEFAULT 0, revoked INTEGER DEFAULT 0, created INTEGER)",
    "CREATE TABLE IF NOT EXISTS cols(id TEXT PRIMARY KEY, bot_id INTEGER, uploader INTEGER, owner_id INTEGER, name TEXT, created INTEGER)",
    "CREATE INDEX IF NOT EXISTS i_cols_user ON cols(bot_id,uploader)",
    "CREATE TABLE IF NOT EXISTS col_items(col_id TEXT, file_id TEXT, added INTEGER, PRIMARY KEY(col_id,file_id))",
    "CREATE TABLE IF NOT EXISTS opens(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, bot_id INTEGER, user_id INTEGER, ts INTEGER)",
    "CREATE INDEX IF NOT EXISTS i_opens_code ON opens(code)",
    "CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, code TEXT, reporter INTEGER, ts INTEGER, status TEXT DEFAULT 'open', UNIQUE(code,reporter))",
    "CREATE TABLE IF NOT EXISTS blocked(unique_id TEXT PRIMARY KEY, reason TEXT, ts INTEGER)",
    "CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, chat_id INTEGER, msg_id INTEGER, run_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS state(bot_id INTEGER, user_id INTEGER, state TEXT, PRIMARY KEY(bot_id,user_id))",
    "CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, stars INTEGER, days INTEGER, charge_id TEXT UNIQUE, ts INTEGER, source TEXT)",
    "CREATE TABLE IF NOT EXISTS coupons(code TEXT PRIMARY KEY, days INTEGER, max_uses INTEGER, used INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS coupon_uses(code TEXT, owner_id INTEGER, PRIMARY KEY(code,owner_id))",
    "CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)",
]


async def init():
    # Purane bot (Spark Live) ka database galti se use na ho jaye
    old = await q("SELECT name FROM pragma_table_info('clones')")
    if any(r["name"] == "last_report" for r in old):
        raise RuntimeError(
            "Ye Turso database purane bot ka hai. Spark Store ke liye NAYA Turso database banao "
            "aur uska URL/token Render me daalo."
        )
    await batch([(s, ()) for s in SCHEMA])
