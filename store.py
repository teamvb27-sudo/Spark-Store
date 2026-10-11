"""Spark Store engine: main bot aur saare clones isi code se files store karte hain."""
import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from urllib.parse import quote

from telegram import Update
from telegram.error import Forbidden, RetryAfter, TelegramError

import core
import db
from ui import B, Btn, Kb, _del, bar, bd, card, clean, edit, esc, fsize, send, show, trunc

KIND_ICON = {"photo": "🖼", "video": "🎬", "audio": "🎵", "voice": "🎙",
             "document": "📄", "animation": "🎞", "video_note": "⭕"}
EXT = {"photo": "jpg", "video": "mp4", "audio": "mp3", "voice": "ogg",
       "animation": "mp4", "video_note": "mp4", "document": "bin"}
SENDERS = {"document": ("send_document", "document"), "photo": ("send_photo", "photo"),
           "video": ("send_video", "video"), "audio": ("send_audio", "audio"),
           "voice": ("send_voice", "voice"), "animation": ("send_animation", "animation"),
           "video_note": ("send_video_note", "video_note")}
TYPE_FILTERS = {
    "ph": ("🖼 Photos", "kind='photo'"),
    "vi": ("🎬 Videos", "kind IN ('video','animation','video_note')"),
    "do": ("📄 Docs", "kind='document'"),
    "au": ("🎵 Audio", "kind IN ('audio','voice')"),
}
PER_PAGE = 8
MAX_COL_ITEMS = 100
EXPIRY_PRESETS = [("1 ghanta", 3600), ("1 din", 86400), ("7 din", 604800), ("30 din", 2592000)]
OPEN_PRESETS = [("1 baar (one-time)", 1), ("5 baar", 5), ("10 baar", 10), ("50 baar", 50)]
DEL_PRESETS = [("10 sec", 10), ("30 sec", 30), ("1 min", 60), ("5 min", 300)]
TERMS = "⚠️ Illegal, adult ya copyrighted content mana hai. Report hone par link hata diya jata hai."

log = logging.getLogger("store")
_flood = defaultdict(deque)
_fails = defaultdict(list)
_unlocked = {}
_lastlist = {}
_qtext = {}
_albums = {}
_adm = {}


# ================= CONTEXT =================
class Ctx:
    def __init__(self, bot, clone, user, owner, premium, admin):
        self.bot, self.clone, self.user = bot, clone, user
        self.uid, self.bot_id = user.id, bot.id
        self.owner, self.premium, self.admin = owner, premium, admin
        self.is_main = clone is None
        self.chat = user.id
        self.old = None
        self.q = None
        self.acked = False
        self.pin = None

    @property
    def mod(self):
        return self.admin or self.uid == core.OWNER_ID


async def clone_admins(clone):
    c = _adm.get(clone["id"])
    if c and time.time() - c[0] < 15:
        return c[1]
    rows = await db.q("SELECT user_id FROM admins WHERE bot_id=?", clone["id"])
    s = {clone["owner_id"]} | {r["user_id"] for r in rows}
    _adm[clone["id"]] = (time.time(), s)
    return s


def adm_refresh(bid):
    _adm.pop(bid, None)


async def make_ctx(bot, clone, user):
    if clone:
        admin = user.id in await clone_admins(clone)
        owner = clone["owner_id"]
    else:
        admin = user.id == core.OWNER_ID
        owner = user.id
    return Ctx(bot, clone, user, owner, await core.is_premium(owner), admin)


async def touch(ctx):
    now, u = int(time.time()), ctx.user
    r = await db.batch([
        ("INSERT INTO ubots(bot_id,user_id,name,username,first_seen,last_seen) VALUES(?,?,?,?,?,?) "
         "ON CONFLICT(bot_id,user_id) DO UPDATE SET name=excluded.name, username=excluded.username, last_seen=excluded.last_seen",
         (ctx.bot_id, u.id, u.full_name, u.username, now, now)),
        ("SELECT banned,pin FROM ubots WHERE bot_id=? AND user_id=?", (ctx.bot_id, u.id)),
    ])
    ctx.pin = r[1][0]["pin"]
    return r[1][0]


async def scr(ctx, text, kb=None):
    s = await show(ctx.bot, ctx.chat, text, kb, ctx.old)
    ctx.old = None
    return s


async def ack(ctx, q, text=None, alert=False):
    if ctx.acked or q is None:
        return
    ctx.acked = True
    try:
        await ctx.bot.answer_callback_query(q.id, text=text, show_alert=alert)
    except TelegramError:
        pass


async def alert(ctx, text):
    await ack(ctx, ctx.q, text, True)


# ---------- state ----------
async def get_state(ctx):
    r = await db.one("SELECT state FROM state WHERE bot_id=? AND user_id=?", ctx.bot_id, ctx.uid)
    return r["state"] if r else None


async def set_state(ctx, s):
    await db.ex("INSERT OR REPLACE INTO state(bot_id,user_id,state) VALUES(?,?,?)", ctx.bot_id, ctx.uid, s)


async def clear_state(ctx):
    await db.ex("DELETE FROM state WHERE bot_id=? AND user_id=?", ctx.bot_id, ctx.uid)


# ---------- small helpers ----------
def brand_text(ctx):
    if ctx.is_main:
        return ""
    if ctx.premium and ctx.clone["nobrand"]:
        return ""
    return f"⚡ Powered by TEAMVB\nTry it @{core.MAIN_USERNAME}"


def can_upload(ctx):
    return ctx.is_main or ctx.admin or bool(ctx.clone["pub"])


def storage_limit(ctx):
    if ctx.owner == core.OWNER_ID:
        return None
    return core.PREMIUM_STORAGE if ctx.premium else core.FREE_STORAGE


def link_url(ctx, code):
    return f"https://t.me/{ctx.bot.username}?start={code}"


def lock_text(ctx):
    if ctx.is_main:
        return "💎 Ye Premium feature hai. Menu me 💎 Premium se upgrade karo."
    return "💎 Ye feature is bot ke Premium plan me hai."


async def premium_gate(ctx):
    if ctx.premium:
        return True
    await alert(ctx, lock_text(ctx))
    return False


def flooded(ctx, limit=40):
    d = _flood[(ctx.bot_id, ctx.uid)]
    now = time.time()
    while d and now - d[0] > 60:
        d.popleft()
    d.append(now)
    return len(d) > limit


def too_many(key, limit=5, window=600):
    now = time.time()
    _fails[key] = [t for t in _fails[key] if now - t < window]
    return len(_fails[key]) >= limit


def left_text(secs):
    secs = int(secs)
    if secs <= 0:
        return "khatam"
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d} din {h} ghante"
    if h:
        return f"{h} ghante {m} min"
    return f"{max(m, 1)} min"


async def used_bytes(ctx):
    if ctx.is_main or ctx.admin:
        r = await db.one("SELECT COALESCE(SUM(size),0) s FROM files WHERE owner_id=?", ctx.owner)
    else:
        r = await db.one("SELECT COALESCE(SUM(size),0) s FROM files WHERE bot_id=? AND uploader=?", ctx.bot_id, ctx.uid)
    return r["s"]


async def get_file(ctx, code):
    return await db.one("SELECT * FROM files WHERE id=? AND bot_id=? AND uploader=?", code, ctx.bot_id, ctx.uid)


async def get_col(ctx, code):
    return await db.one("SELECT * FROM cols WHERE id=? AND bot_id=? AND uploader=?", code, ctx.bot_id, ctx.uid)


async def get_my_link(ctx, code):
    return await db.one("SELECT * FROM links WHERE code=? AND bot_id=? AND uploader=?", code, ctx.bot_id, ctx.uid)


def back_target(ctx):
    return _lastlist.get((ctx.bot_id, ctx.uid), "mf:all:0")


# ================= HOME / HELP =================
def home_kb(ctx):
    if not can_upload(ctx):
        return Kb([[B("❓ Help", "help")]])
    rows = [[B("📂 My Files", "mf"), B("📁 Collections", "cl:0")],
            [B("🔒 Vault", "vt"), B("📊 Storage", "st")]]
    if ctx.is_main:
        rows += [[B("➕ Create Store Bot", "create"), B("🤖 My Bots", "my")],
                 [B("💎 Premium", "up"), B("🎟 Coupon", "coupon")]]
    elif ctx.admin:
        rows.append([B("🛠 Admin Panel", "ad")])
    rows.append([B("❓ Help", "help")])
    if ctx.is_main and ctx.uid == core.OWNER_ID:
        rows.append([B("👑 Owner Panel", "o:menu")])
    return Kb(rows)


async def show_home(ctx):
    name = esc(ctx.user.first_name)
    title = "SPARK STORE" if ctx.is_main else clean(trunc(ctx.clone["name"] or "SPARK STORE", 24))
    lines = [f"👋 {bd('Welcome')}, {name}!", ""]
    custom = None if ctx.is_main else ctx.clone["welcome"]
    if custom:
        lines += [esc(custom), ""]
    elif can_upload(ctx):
        lines += ["📤 Koi bhi file bhejo → turant link",
                  "🔗 Link kholo → file wapas, kabhi bhi",
                  "📁 Collections • 🔒 Vault • ⏳ Expiry link", ""]
    else:
        lines += ["🔗 Kisi ka link mila hai? Us link par click karo,",
                  "     file yahin aa jayegi.", ""]
    if can_upload(ctx):
        used = await used_bytes(ctx)
        lim = storage_limit(ctx) if (ctx.is_main or ctx.admin) else None
        if lim:
            lines += [f"💾 {bar(used, lim)}", f"     {fsize(used)} / {fsize(lim)}", ""]
        else:
            lines += [f"💾 {bd('Used')}  ➜  {fsize(used)}", ""]
    lines.append(TERMS)
    b = brand_text(ctx)
    if b:
        lines += ["", "─" * 16, b]
    await scr(ctx, card("⚡", title, *lines), home_kb(ctx))


async def show_help(ctx):
    lines = [
        f"📤 {bd('Upload')}  –  file, photo, video, audio bhejo, link milega",
        f"🔗 {bd('Link')}  –  link par click = file wapas",
        f"📂 {bd('My Files')}  –  type, search, favourites, trash",
        f"📁 {bd('Collections')}  –  bahut files ek link me",
        f"🔒 {bd('Vault')}  –  PIN wala private hissa  💎",
        f"⚙️ {bd('Link Settings')}  –  password, expiry, one-time, protected,",
        "     auto-delete  💎", "",
        "⌨️ /files  /cols  /vault  /storage  /done  /cancel", "",
        f"💾 {bd('Free')}: {fsize(core.FREE_STORAGE)}   •   💎 {bd('Premium')}: {fsize(core.PREMIUM_STORAGE)}", "",
        TERMS, "Moderation ke liye reported files review ho sakti hain.",
    ]
    await scr(ctx, card("❓", "HELP", *lines), Kb([[B("🏠 Menu", "home")]]))


# ================= MEDIA / UPLOAD =================
def extract_media(m):
    if m.animation:
        x, kind = m.animation, "animation"
    elif m.document:
        x, kind = m.document, "document"
    elif m.photo:
        x, kind = m.photo[-1], "photo"
    elif m.video:
        x, kind = m.video, "video"
    elif m.audio:
        x, kind = m.audio, "audio"
    elif m.voice:
        x, kind = m.voice, "voice"
    elif m.video_note:
        x, kind = m.video_note, "video_note"
    else:
        return None
    name = getattr(x, "file_name", None)
    if kind == "audio" and not name:
        name = getattr(x, "title", None)
    return {"kind": kind, "file_id": x.file_id, "unique_id": x.file_unique_id,
            "size": int(getattr(x, "file_size", 0) or 0), "mime": getattr(x, "mime_type", None), "name": name}


async def insert_file(ctx, med, name, caption, backup_msg):
    now = int(time.time())
    for _ in range(3):
        code = core.new_code()
        try:
            await db.batch([
                ("INSERT INTO files(id,bot_id,uploader,owner_id,name,kind,mime,size,file_id,unique_id,caption,backup_msg,created) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (code, ctx.bot_id, ctx.uid, ctx.owner, name, med["kind"], med["mime"], med["size"],
                  med["file_id"], med["unique_id"], caption, backup_msg, now)),
                ("INSERT INTO links(code,bot_id,owner_id,uploader,kind,created) VALUES(?,?,?,?,'file',?)",
                 (code, ctx.bot_id, ctx.owner, ctx.uid, now)),
            ])
            return code
        except RuntimeError:
            continue
    raise RuntimeError("code collision")


async def create_col(ctx, name):
    now = int(time.time())
    for _ in range(3):
        code = core.new_code()
        try:
            await db.batch([
                ("INSERT INTO cols(id,bot_id,uploader,owner_id,name,created) VALUES(?,?,?,?,?,?)",
                 (code, ctx.bot_id, ctx.uid, ctx.owner, name, now)),
                ("INSERT INTO links(code,bot_id,owner_id,uploader,kind,created) VALUES(?,?,?,?,'col',?)",
                 (code, ctx.bot_id, ctx.owner, ctx.uid, now)),
            ])
            return code
        except RuntimeError:
            continue
    raise RuntimeError("code collision")


async def col_count(ctx):
    return (await db.one("SELECT COUNT(*) c FROM cols WHERE bot_id=? AND uploader=?", ctx.bot_id, ctx.uid))["c"]


async def add_to_col(col_code, file_code):
    n = (await db.one("SELECT COUNT(*) c FROM col_items WHERE col_id=?", col_code))["c"]
    if n >= MAX_COL_ITEMS:
        return False
    await db.ex("INSERT OR IGNORE INTO col_items(col_id,file_id,added) VALUES(?,?,?)", col_code, file_code, int(time.time()))
    return True


async def on_upload(ctx, m, med, st):
    bot, chat = ctx.bot, m.chat_id
    if not can_upload(ctx):
        await scr(ctx, card("🔒", "UPLOAD BAND", "Is bot me files sirf admin upload kar sakta hai.",
                            "Link kholne ke liye sahi link par click karo."), Kb([[B("❓ Help", "help")]]))
        return
    if await db.one("SELECT 1 x FROM blocked WHERE unique_id=?", med["unique_id"]):
        await scr(ctx, card("🚫", "NOT ALLOWED", "Ye file allowed nahi hai.", "(Pehle report hokar hata di gayi thi)"),
                  Kb([[B("🏠 Menu", "home")]]))
        return
    collect = st.split(":") if st and st.startswith("collect:") else None
    if st and not collect:
        await clear_state(ctx)

    ex = await db.one("SELECT * FROM files WHERE bot_id=? AND uploader=? AND unique_id=? AND trashed=0 LIMIT 1",
                      ctx.bot_id, ctx.uid, med["unique_id"])
    if ex:
        code = ex["id"]
        note = "♻️ Ye file pehle se stored hai (dobara space nahi lagi)."
    else:
        lim = storage_limit(ctx)
        if lim:
            used = (await db.one("SELECT COALESCE(SUM(size),0) s FROM files WHERE owner_id=?", ctx.owner))["s"]
            if used + med["size"] > lim:
                rows = [[B("🏠 Menu", "home")]]
                if ctx.is_main:
                    rows.insert(0, [B("💎 Premium lo", "up")])
                    msg = f"Storage full: {fsize(used)} / {fsize(lim)}"
                else:
                    msg = "Is bot ka storage full hai. Admin ko batao."
                await scr(ctx, card("💾", "STORAGE FULL", msg), Kb(rows))
                return
        name = clean((med["name"] or "").strip())[:80]
        if not name:
            ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime(time.time() + core.TZ_OFFSET * 60))
            name = f"{med['kind']}_{ts}.{EXT[med['kind']]}"
        caption = (m.caption or "")[:300]
        backup = None
        if ctx.is_main and core.STORAGE_CHANNEL:
            try:
                backup = (await bot.copy_message(core.STORAGE_CHANNEL, chat, m.message_id)).message_id
            except TelegramError:
                backup = None
        code = await insert_file(ctx, med, name, caption, backup)
        note = None

    if collect:
        col = await get_col(ctx, collect[1])
        if col:
            ok = await add_to_col(col["id"], code)
            await collect_status(ctx, col, int(collect[2]), None if ok else "⚠️ Limit poori (100 files)")
            return
        await clear_state(ctx)

    f = await get_file(ctx, code)
    mg = m.media_group_id
    if mg:
        key = (ctx.bot_id, ctx.uid, str(mg))
        ent = _albums.get(key)
        if not ent and (ctx.premium or await col_count(ctx) < core.FREE_COLLECTIONS):
            ccode = await create_col(ctx, "Album " + time.strftime("%d %b %H:%M", time.gmtime(time.time() + core.TZ_OFFSET * 60)))
            ent = _albums[key] = (ccode, time.time())
            if len(_albums) > 300:
                for k in [k for k, v in _albums.items() if time.time() - v[1] > 600]:
                    _albums.pop(k, None)
        if ent:
            await add_to_col(ent[0], code)
            col = await get_col(ctx, ent[0])
            await col_card(ctx, col, "📸 Album ek collection me save ho gaya.")
            return
    await file_card(ctx, f, note)


# ================= FILE SCREENS =================
def link_flags(l):
    f = []
    if l["revoked"]:
        f.append("🚫 Revoked")
    if l["pw"]:
        f.append("🔐 Password")
    if l["expires"]:
        f.append("⏳ " + left_text(l["expires"] - time.time()))
    if l["max_opens"]:
        f.append("🔢 " + ("One-time" if l["max_opens"] == 1 else f"{l['max_opens']} opens"))
    if l["protect"]:
        f.append("🛡 Protected")
    if l["autodel"]:
        f.append(f"🧹 {l['autodel']}s")
    return "  •  ".join(f) if f else "🌐 Public link"


async def file_card(ctx, f, note=None):
    l = await db.one("SELECT * FROM links WHERE code=?", f["id"])
    icon = KIND_ICON.get(f["kind"], "📦")
    lines = [f"{icon} <b>{esc(trunc(f['name'], 60))}</b>", "",
             f"📦 {bd('Size')}  ➜  {fsize(f['size'])}",
             f"🗂 {bd('Type')}  ➜  {f['kind'].title()}",
             f"📅 {bd('Date')}  ➜  {core.fdate(f['created'])}",
             f"👁 {bd('Opens')}  ➜  {l['opens'] if l else 0}"]
    if f["tags"]:
        lines.append(f"🏷 {esc(f['tags'])}")
    code = f["id"]
    back = back_target(ctx)
    if f["trashed"]:
        lines += ["", "🗑 Ye file Trash me hai."]
        kb = Kb([[B("♻️ Restore", f"fre:{code}"), B("❌ Delete forever", f"fdl:{code}")], [B("🔙 Back", back)]])
    else:
        url = link_url(ctx, code)
        lines += ["", f"🔗 <code>{esc(url)}</code>", link_flags(l)]
        share = "https://t.me/share/url?url=" + quote(url, safe="") + "&text=" + quote("📦 " + f["name"][:60], safe="")
        kb = Kb([
            [B("📤 Get File", f"fg:{code}"), Btn("🔗 Share", url=share)],
            [B("⚙️ Link Settings", f"lk:{code}"), B("☆ Unfav" if f["fav"] else "⭐ Fav", f"fv:{code}")],
            [B("✏️ Rename", f"rn:{code}"), B("🏷 Tags", f"tg:{code}")],
            [B("📁 Add to Collection", f"fc:{code}"), B("🔓 Remove Vault" if f["vault"] else "🔒 Move to Vault", f"fvt:{code}")],
            [B("🗑 Trash", f"ftr:{code}"), B("🔙 Back", back)],
        ])
    if note:
        lines += ["", note]
    await scr(ctx, card("📄", "FILE", *lines), kb)


async def files_menu(ctx):
    rows = await db.q("SELECT kind, COUNT(*) c FROM files WHERE bot_id=? AND uploader=? AND trashed=0 AND vault=0 GROUP BY kind",
                      ctx.bot_id, ctx.uid)
    by = {r["kind"]: r["c"] for r in rows}
    n = {"ph": by.get("photo", 0),
         "vi": by.get("video", 0) + by.get("animation", 0) + by.get("video_note", 0),
         "do": by.get("document", 0), "au": by.get("audio", 0) + by.get("voice", 0)}
    ex = await db.batch([
        ("SELECT COUNT(*) c FROM files WHERE bot_id=? AND uploader=? AND trashed=0 AND vault=0 AND fav=1", (ctx.bot_id, ctx.uid)),
        ("SELECT COUNT(*) c FROM files WHERE bot_id=? AND uploader=? AND trashed>0", (ctx.bot_id, ctx.uid)),
        ("SELECT COUNT(*) c FROM files WHERE bot_id=? AND uploader=? AND trashed=0 AND vault=1", (ctx.bot_id, ctx.uid)),
    ])
    fav, tr, vt = ex[0][0]["c"], ex[1][0]["c"], ex[2][0]["c"]
    total = sum(n.values())
    lines = [f"📦 {bd('Total')}  ➜  {total} file(s)", "", "👇 Category chuno"]
    kb = Kb([
        [B(f"📄 All ({total})", "mf:all:0")],
        [B(f"🖼 Photos ({n['ph']})", "mf:ph:0"), B(f"🎬 Videos ({n['vi']})", "mf:vi:0")],
        [B(f"📑 Docs ({n['do']})", "mf:do:0"), B(f"🎵 Audio ({n['au']})", "mf:au:0")],
        [B(f"⭐ Favs ({fav})", "mf:fav:0"), B(f"🗑 Trash ({tr})", "mf:tr:0")],
        [B(f"🔒 Vault ({vt})", "vt"), B("🔍 Search", "sr")],
        [B("🏠 Menu", "home")],
    ])
    await scr(ctx, card("📂", "MY FILES", *lines), kb)


def where_for(ctx, flt):
    base = "bot_id=? AND uploader=?"
    params = [ctx.bot_id, ctx.uid]
    title = "ALL FILES"
    if flt in TYPE_FILTERS:
        title = TYPE_FILTERS[flt][0].split(" ", 1)[1].upper()
        base += f" AND trashed=0 AND vault=0 AND {TYPE_FILTERS[flt][1]}"
    elif flt == "fav":
        title, base = "FAVOURITES", base + " AND trashed=0 AND vault=0 AND fav=1"
    elif flt == "tr":
        title, base = "TRASH", base + " AND trashed>0"
    elif flt == "vt":
        title, base = "VAULT", base + " AND trashed=0 AND vault=1"
    elif flt == "q":
        k = f"%{_qtext.get((ctx.bot_id, ctx.uid), '')}%"
        title = "SEARCH"
        base += " AND trashed=0 AND vault=0 AND (name LIKE ? OR tags LIKE ? OR caption LIKE ?)"
        params += [k, k, k]
    else:
        base += " AND trashed=0 AND vault=0"
    return title, base, params


async def files_list(ctx, flt, page):
    title, base, params = where_for(ctx, flt)
    total = (await db.one(f"SELECT COUNT(*) c FROM files WHERE {base}", *params))["c"]
    page = max(0, min(page, max(0, (total - 1) // PER_PAGE)))
    rows = await db.q(f"SELECT id,name,kind,size,fav FROM files WHERE {base} ORDER BY created DESC LIMIT ? OFFSET ?",
                      *params, PER_PAGE, page * PER_PAGE)
    _lastlist[(ctx.bot_id, ctx.uid)] = f"mf:{flt}:{page}"
    kbr = [[B(f"{'⭐' if r['fav'] else KIND_ICON.get(r['kind'], '📦')} {trunc(r['name'], 26)} • {fsize(r['size'])}", f"f:{r['id']}")]
           for r in rows]
    nav = []
    if page > 0:
        nav.append(B("◀ Prev", f"mf:{flt}:{page - 1}"))
    if (page + 1) * PER_PAGE < total:
        nav.append(B("Next ▶", f"mf:{flt}:{page + 1}"))
    if nav:
        kbr.append(nav)
    extra = []
    if flt == "tr" and total:
        extra.append(B("🧹 Empty Trash", "et"))
    if flt == "vt":
        extra.append(B("🔐 Lock Vault", "vl"))
    if extra:
        kbr.append(extra)
    kbr.append([B("🔙 Back", "mf"), B("🏠 Menu", "home")])
    lines = [f"📦 {total} file(s)  •  page {page + 1}", ""]
    lines.append("👇 File chuno" if rows else "Yahan abhi kuch nahi hai.")
    await scr(ctx, card("📂", title, *lines), Kb(kbr))


async def purge_files(ids):
    ids = list(ids)
    main = await core.main_bot() if core.STORAGE_CHANNEL else None
    for i in range(0, len(ids), 40):
        chunk = tuple(ids[i:i + 40])
        ph = ",".join("?" * len(chunk))
        rows = await db.q(f"SELECT backup_msg FROM files WHERE id IN ({ph})", *chunk)
        await db.batch([
            (f"DELETE FROM col_items WHERE file_id IN ({ph})", chunk),
            (f"DELETE FROM links WHERE code IN ({ph})", chunk),
            (f"DELETE FROM files WHERE id IN ({ph})", chunk),
        ])
        if main:
            for r in rows:
                if r["backup_msg"]:
                    await _del(main, core.STORAGE_CHANNEL, r["backup_msg"])


# ================= LINK SETTINGS =================
async def link_name(l):
    if l["kind"] == "file":
        r = await db.one("SELECT name FROM files WHERE id=?", l["code"])
    else:
        r = await db.one("SELECT name FROM cols WHERE id=?", l["code"])
    return r["name"] if r else "?"


async def lk_screen(ctx, l):
    name = await link_name(l)
    icon = "📁" if l["kind"] == "col" else "📄"
    lock = "" if ctx.premium else " 💎"
    exp = left_text(l["expires"] - time.time()) if l["expires"] else "OFF"
    lines = [f"{icon} <b>{esc(trunc(name, 50))}</b>", "",
             f"🔗 <code>{esc(link_url(ctx, l['code']))}</code>", "",
             f"🔐 {bd('Password')}  ➜  {'ON' if l['pw'] else 'OFF'}",
             f"⏳ {bd('Expiry')}  ➜  {exp}",
             f"🔢 {bd('Max opens')}  ➜  {l['max_opens'] or '∞'}   (opened {l['opens']})",
             f"🛡 {bd('Protected')}  ➜  {'ON' if l['protect'] else 'OFF'}",
             f"🧹 {bd('Auto-delete')}  ➜  {str(l['autodel']) + 's' if l['autodel'] else 'OFF'}",
             f"🚦 {bd('Status')}  ➜  {'🚫 Revoked' if l['revoked'] else '✅ Active'}"]
    if not ctx.premium:
        lines += ["", "💎 Password, expiry, one-time, protected aur auto-delete Premium me hain."]
    c = l["code"]
    pw_btn = B("🔐 Password hatao", f"lpx:{c}") if l["pw"] else B(f"🔐 Password{lock}", f"lpw:{c}")
    kb = Kb([
        [pw_btn, B(f"⏳ Expiry{lock}", f"lx:{c}")],
        [B(f"🔢 Max opens{lock}", f"lm:{c}"), B(f"🛡 Protect {'OFF' if l['protect'] else 'ON'}{lock}", f"lpr:{c}")],
        [B(f"🧹 Auto-delete{lock}", f"la:{c}")],
        [B("✅ Restore link" if l["revoked"] else "🚫 Revoke link", f"lrv:{c}")],
        [B("🔙 Back", f"{'c' if l['kind'] == 'col' else 'f'}:{c}")],
    ])
    await scr(ctx, card("⚙️", "LINK SETTINGS", *lines), kb)


# ================= COLLECTIONS =================
async def col_stats(code):
    r = await db.one("SELECT COUNT(*) n, COALESCE(SUM(f.size),0) s FROM col_items ci JOIN files f ON f.id=ci.file_id "
                     "WHERE ci.col_id=? AND f.trashed=0", code)
    return r["n"], r["s"]


async def col_card(ctx, col, note=None):
    l = await db.one("SELECT * FROM links WHERE code=?", col["id"])
    n, size = await col_stats(col["id"])
    url = link_url(ctx, col["id"])
    lines = [f"📁 <b>{esc(trunc(col['name'], 60))}</b>", "",
             f"📦 {bd('Files')}  ➜  {n}   •   {fsize(size)}",
             f"👁 {bd('Opens')}  ➜  {l['opens'] if l else 0}", "",
             f"🔗 <code>{esc(url)}</code>", link_flags(l)]
    if note:
        lines += ["", note]
    share = "https://t.me/share/url?url=" + quote(url, safe="") + "&text=" + quote("📁 " + col["name"][:60], safe="")
    c = col["id"]
    kb = Kb([
        [B("📤 Get All", f"cg:{c}"), Btn("🔗 Share", url=share)],
        [B("📥 Add Files", f"ca:{c}"), B("📋 Items", f"ci:{c}:0")],
        [B("⚙️ Link Settings", f"lk:{c}"), B("✏️ Rename", f"crn:{c}")],
        [B("🗑 Delete", f"cdl:{c}"), B("🔙 Back", "cl:0")],
    ])
    await scr(ctx, card("📁", "COLLECTION", *lines), kb)


async def collect_status(ctx, col, mid, note=None):
    n, size = await col_stats(col["id"])
    c = col["id"]
    lines = [f"📁 <b>{esc(trunc(col['name'], 40))}</b>", "",
             f"📥 {bd('Collecting')} ...   {n} file(s) • {fsize(size)}", "",
             "Aur files bhejte jao. Khatam hone par ✅ Done dabao."]
    if note:
        lines += ["", note]
    await edit(ctx.bot, ctx.chat, mid, card("📥", "ADD FILES", *lines), Kb([[B("✅ Done", f"cdone:{c}")]]))


# ================= STORAGE / VAULT =================
async def storage_screen(ctx):
    used = await used_bytes(ctx)
    lim = storage_limit(ctx) if (ctx.is_main or ctx.admin) else None
    r = await db.batch([
        ("SELECT COUNT(*) c FROM files WHERE bot_id=? AND uploader=? AND trashed=0", (ctx.bot_id, ctx.uid)),
        ("SELECT COUNT(*) c FROM files WHERE bot_id=? AND uploader=? AND trashed>0", (ctx.bot_id, ctx.uid)),
        ("SELECT COUNT(*) c FROM cols WHERE bot_id=? AND uploader=?", (ctx.bot_id, ctx.uid)),
        ("SELECT COALESCE(SUM(opens),0) s FROM links WHERE bot_id=? AND uploader=?", (ctx.bot_id, ctx.uid)),
    ])
    lines = []
    if lim:
        lines += [f"💾 {bar(used, lim)}", f"     {fsize(used)} / {fsize(lim)}"]
    else:
        lines += [f"💾 {bd('Used')}  ➜  {fsize(used)}"]
    lines += ["", f"📄 {bd('Files')}  ➜  {r[0][0]['c']}", f"🗑 {bd('Trash')}  ➜  {r[1][0]['c']}",
              f"📁 {bd('Collections')}  ➜  {r[2][0]['c']}", f"👁 {bd('Total opens')}  ➜  {r[3][0]['s']}",
              f"💎 {bd('Plan')}  ➜  {'Premium' if ctx.premium else 'Free'}"]
    if ctx.premium:
        week = (await db.one("SELECT COUNT(*) c FROM opens o JOIN links l ON l.code=o.code WHERE l.bot_id=? AND l.uploader=? AND o.ts>?",
                             ctx.bot_id, ctx.uid, int(time.time()) - 7 * 86400))["c"]
        top = await db.q("SELECT f.name, l.opens FROM links l JOIN files f ON f.id=l.code WHERE l.bot_id=? AND l.uploader=? AND l.kind='file' "
                         "AND l.opens>0 ORDER BY l.opens DESC LIMIT 3", ctx.bot_id, ctx.uid)
        lines += [f"📈 {bd('Last 7 days')}  ➜  {week} opens"]
        if top:
            lines += ["", f"🏆 {bd('Top files')}"] + [f"   {i + 1}. {esc(trunc(t['name'], 28))} • {t['opens']}" for i, t in enumerate(top)]
    else:
        lines += ["", "💎 Premium me detailed stats milte hain."]
    kb = [[B("🧹 Empty Trash", "et")] if r[1][0]["c"] else [], [B("🏠 Menu", "home")]]
    await scr(ctx, card("📊", "STORAGE", *lines), Kb([x for x in kb if x]))


def vault_open(ctx):
    return bool(ctx.pin) and _unlocked.get((ctx.bot_id, ctx.uid), 0) > time.time()


async def vault_gate(ctx):
    if not ctx.premium:
        await scr(ctx, card("🔒", "VAULT", "PIN wala private hissa. Yahan ki files normal list me nahi dikhti.", "",
                            lock_text(ctx)), Kb([[B("🏠 Menu", "home")]]))
        return
    if not ctx.pin:
        await set_state(ctx, "pinset")
        await scr(ctx, card("🔒", "SET VAULT PIN", "4 se 8 digit ka PIN bhejo.", "", "Message turant delete ho jayega 🔐"),
                  Kb([[B("❌ Cancel", "home")]]))
        return
    if not vault_open(ctx):
        await set_state(ctx, "unlock")
        await scr(ctx, card("🔒", "VAULT LOCKED", "Apna PIN bhejo.", "", "Message turant delete ho jayega 🔐"),
                  Kb([[B("🔁 Reset PIN", "vr")], [B("❌ Cancel", "home")]]))
        return
    await files_list(ctx, "vt", 0)


# ================= OPEN LINK / DELIVER =================
async def link_files(l):
    if l["kind"] == "file":
        return await db.q("SELECT * FROM files WHERE id=? AND trashed=0", l["code"])
    return await db.q("SELECT f.* FROM col_items ci JOIN files f ON f.id=ci.file_id WHERE ci.col_id=? AND f.trashed=0 "
                      "ORDER BY ci.added LIMIT ?", l["code"], MAX_COL_ITEMS)


async def send_file(ctx, chat, f, protect, extra=""):
    meth, arg = SENDERS.get(f["kind"], SENDERS["document"])
    kw = {arg: f["file_id"], "protect_content": protect}
    if f["kind"] != "video_note":
        cap = ((f["caption"] or "")[:700] + ("\n\n" + extra if extra else "")).strip()
        if cap:
            kw["caption"] = cap[:1000]
    for _ in range(2):
        try:
            return (await getattr(ctx.bot, meth)(chat, **kw)).message_id
        except RetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 5) + 1)
        except Forbidden:
            return None
        except TelegramError:
            break
    if ctx.is_main and core.STORAGE_CHANNEL and f["backup_msg"]:
        try:
            return (await ctx.bot.copy_message(chat, core.STORAGE_CHANNEL, f["backup_msg"], protect_content=protect)).message_id
        except TelegramError:
            pass
    return None


async def deliver(ctx, chat, l, own=False, back=None):
    files = await link_files(l)
    if not files:
        await scr(ctx, card("❌", "NOT AVAILABLE", "Ye file ab available nahi hai."), Kb([[B("🏠 Menu", "home")]]))
        return
    protect = bool(l["protect"]) and not own
    brand = "" if own else brand_text(ctx)
    ids = []
    for i, f in enumerate(files):
        mid = await send_file(ctx, chat, f, protect, brand if i == len(files) - 1 else "")
        if mid:
            ids.append(mid)
        if len(files) > 1:
            await asyncio.sleep(0.35)
    total = sum(f["size"] for f in files)
    if l["kind"] == "file":
        f = files[0]
        what = f"{KIND_ICON.get(f['kind'], '📦')} <b>{esc(trunc(f['name'], 50))}</b>  •  {fsize(f['size'])}"
    else:
        what = f"📁 <b>{esc(trunc(await link_name(l), 40))}</b>  •  {len(files)} files • {fsize(total)}"
    if not ids:
        lines = [what, "", "⚠️ File bhejne me dikkat hui. Thodi der baad dobara try karo."]
        title, icon = "FAILED", "⚠️"
    else:
        lines = [what, "", f"✅ {len(ids)}/{len(files)} file(s) bhej di gayi"]
        title, icon = "DELIVERED", "✅"
    if protect:
        lines.append("🛡 Protected: forward/save band hai")
    if l["autodel"] and ids and not own:
        lines.append(f"🧹 Ye files {l['autodel']} sec me delete ho jayengi, abhi save kar lo!")
        await schedule_delete(ctx.bot, ctx.bot_id, chat, ids, l["autodel"])
    if not own and brand:
        lines += ["", "─" * 16, brand]
    kb = Kb([[B("🔙 Back", back or "home")]]) if own else Kb([[B("🚩 Report", f"rp:{l['code']}")]])
    await scr(ctx, card(icon, title, *lines), kb)


async def open_link(ctx, chat, code, pw=None):
    l = await db.one("SELECT * FROM links WHERE code=? AND bot_id=?", code, ctx.bot_id)
    if not l:
        await scr(ctx, card("❌", "LINK INVALID", "Ye link galat hai ya hata diya gaya."), Kb([[B("🏠 Menu", "home")]]))
        return
    own = ctx.uid == l["uploader"] or ctx.mod
    if l["revoked"] and ctx.uid != core.OWNER_ID:
        await scr(ctx, card("🚫", "LINK BAND", "Ye link band kar diya gaya hai."), Kb([[B("🏠 Menu", "home")]]))
        return
    now = int(time.time())
    if not own:
        if l["expires"] and now > l["expires"]:
            await scr(ctx, card("⏳", "LINK EXPIRED", "Is link ki time-limit khatam ho gayi."), Kb([[B("🏠 Menu", "home")]]))
            return
        if l["max_opens"] and l["opens"] >= l["max_opens"]:
            await scr(ctx, card("🔢", "LINK USED", "Is link ki limit poori ho chuki hai."), Kb([[B("🏠 Menu", "home")]]))
            return
        if l["pw"]:
            key = ("pw", ctx.bot_id, ctx.uid, code)
            if pw is None:
                await set_state(ctx, f"pw:{code}")
                await scr(ctx, card("🔐", "PASSWORD", "Ye link password se locked hai.", "Password bhejo 👇"),
                          Kb([[B("❌ Cancel", "home")]]))
                return
            if too_many(key):
                await scr(ctx, card("⛔", "TOO MANY TRIES", "Bahut galat password. 10 min baad try karo."), Kb([[B("🏠 Menu", "home")]]))
                return
            if not core.same(core.hash_secret(f"link:{code}", pw), l["pw"]):
                _fails[key].append(time.time())
                await set_state(ctx, f"pw:{code}")
                await scr(ctx, card("🔐", "PASSWORD", "❌ Galat password. Dobara bhejo 👇"), Kb([[B("❌ Cancel", "home")]]))
                return
        if l["max_opens"]:
            n = await db.run("UPDATE links SET opens=opens+1 WHERE code=? AND (max_opens=0 OR opens<max_opens)", code)
            if not n:
                await scr(ctx, card("🔢", "LINK USED", "Is link ki limit poori ho chuki hai."), Kb([[B("🏠 Menu", "home")]]))
                return
        else:
            await db.ex("UPDATE links SET opens=opens+1 WHERE code=?", code)
        await db.ex("INSERT INTO opens(code,bot_id,user_id,ts) VALUES(?,?,?,?)", code, ctx.bot_id, ctx.uid, now)
    await clear_state(ctx)
    await deliver(ctx, chat, l, own=own and ctx.uid == l["uploader"], back=None)


async def schedule_delete(bot, bot_id, chat, ids, secs):
    run_at = int(time.time()) + secs
    await db.batch([("INSERT INTO jobs(bot_id,chat_id,msg_id,run_at) VALUES(?,?,?,?)", (bot_id, chat, i, run_at)) for i in ids])
    core.spawn(_delete_later(bot, bot_id, chat, ids, secs))


async def _delete_later(bot, bot_id, chat, ids, secs):
    await asyncio.sleep(secs)
    for i in ids:
        await _del(bot, chat, i)
    ph = ",".join("?" * len(ids))
    try:
        await db.ex(f"DELETE FROM jobs WHERE bot_id=? AND chat_id=? AND msg_id IN ({ph})", bot_id, chat, *ids)
    except Exception:
        pass


async def run_jobs():
    rows = await db.q("SELECT id,bot_id,chat_id,msg_id FROM jobs WHERE run_at<=? LIMIT 200", int(time.time()))
    for r in rows:
        try:
            b = await core.bot_for(r["bot_id"])
            if b:
                await _del(b, r["chat_id"], r["msg_id"])
        except Exception:
            pass
        await db.ex("DELETE FROM jobs WHERE id=?", r["id"])


async def purge_trash():
    rows = await db.q("SELECT id FROM files WHERE trashed>0 AND trashed<? LIMIT 200", int(time.time()) - core.TRASH_DAYS * 86400)
    if rows:
        await purge_files([r["id"] for r in rows])


# ================= MODERATION =================
async def report_link(ctx, code):
    l = await db.one("SELECT * FROM links WHERE code=? AND bot_id=?", code, ctx.bot_id)
    if not l:
        await alert(ctx, "Link nahi mila.")
        return
    new = await db.run("INSERT OR IGNORE INTO reports(bot_id,code,reporter,ts) VALUES(?,?,?,?)", ctx.bot_id, code, ctx.uid, int(time.time()))
    if not new:
        await alert(ctx, "Tumne ye pehle hi report kar diya hai.")
        return
    n = (await db.one("SELECT COUNT(*) c FROM reports WHERE code=? AND status='open'", code))["c"]
    held = False
    if n >= 3 and not l["revoked"]:
        await db.ex("UPDATE links SET revoked=1 WHERE code=?", code)
        held = True
    name = esc(trunc(await link_name(l), 50))
    txt = (f"🚩 <b>REPORT</b>\n\n📄 {name}\n🤖 Bot: @{esc(ctx.bot.username)}\n🔗 Code: <code>{code}</code>\n"
           f"👤 Uploader: <code>{l['uploader']}</code>\n📊 Reports: {n}" + ("\n⛔ Auto-hold laga diya gaya." if held else ""))
    kb = Kb([[B("🗑 Takedown", f"rt:{code}"), B("✅ Dismiss", f"rd:{code}")], [B("🚫 Ban uploader", f"rb:{code}")]])
    await core.notify(core.OWNER_ID, txt, reply_markup=kb, parse_mode="HTML")
    await ack(ctx, ctx.q, "🚩 Report bhej diya. Shukriya.", True)


async def takedown(code):
    """Link + uski files hata do, aur dobara upload rokne ke liye blocklist me daalo."""
    l = await db.one("SELECT * FROM links WHERE code=?", code)
    if not l:
        return 0, None
    if l["kind"] == "file":
        ids = [code]
    else:
        ids = [r["file_id"] for r in await db.q("SELECT file_id FROM col_items WHERE col_id=?", code)]
    now = int(time.time())
    if ids:
        ph = ",".join("?" * len(ids))
        for r in await db.q(f"SELECT unique_id FROM files WHERE id IN ({ph})", *ids):
            await db.ex("INSERT OR IGNORE INTO blocked(unique_id,reason,ts) VALUES(?,?,?)", r["unique_id"], "takedown", now)
        await purge_files(ids)
    await db.batch([("INSERT OR REPLACE INTO links(code,bot_id,owner_id,uploader,kind,revoked,created) VALUES(?,?,?,?,?,1,?)",
                     (code, l["bot_id"], l["owner_id"], l["uploader"], l["kind"], l["created"])),
                    ("UPDATE links SET revoked=1 WHERE code=?", (code,)),
                    ("UPDATE reports SET status='actioned' WHERE code=?", (code,))])
    try:
        b = await core.bot_for(l["bot_id"])
        if b:
            await b.send_message(l["uploader"], "🚫 Tumhari ek file/link report ke baad hata di gayi (rules ke khilaf).")
    except Exception:
        pass
    return len(ids), l


async def ban_uploader(l):
    if l["bot_id"] == core.MAIN_ID:
        await db.ex("INSERT OR IGNORE INTO owners(id,name,username,joined) VALUES(?,?,?,?)", l["uploader"], "?", None, int(time.time()))
        await db.ex("UPDATE owners SET banned=1 WHERE id=?", l["uploader"])
    else:
        await db.ex("UPDATE ubots SET banned=1 WHERE bot_id=? AND user_id=?", l["bot_id"], l["uploader"])


async def dismiss_report(code):
    await db.batch([("UPDATE reports SET status='dismissed' WHERE code=?", (code,)),
                    ("UPDATE links SET revoked=0 WHERE code=?", (code,))])


# ================= CALLBACKS =================
async def cb_home(ctx):
    await clear_state(ctx)
    await show_home(ctx)


async def cb_help(ctx):
    await show_help(ctx)


async def cb_files(ctx, flt=None, page="0"):
    await clear_state(ctx)
    if flt is None:
        await files_menu(ctx)
    elif flt == "vt" and not vault_open(ctx):
        await vault_gate(ctx)
    else:
        await files_list(ctx, flt, int(page) if page.isdigit() else 0)


async def cb_file(ctx, code):
    f = await get_file(ctx, code)
    if not f:
        await alert(ctx, "File nahi mili.")
        return
    await clear_state(ctx)
    await file_card(ctx, f)


async def cb_fg(ctx, code):
    l = await get_my_link(ctx, code)
    if not l or l["kind"] != "file":
        await alert(ctx, "File nahi mili.")
        return
    await deliver(ctx, ctx.chat, l, own=True, back=f"f:{code}")


async def cb_fv(ctx, code):
    f = await get_file(ctx, code)
    if f:
        await db.ex("UPDATE files SET fav=? WHERE id=?", 0 if f["fav"] else 1, code)
        await file_card(ctx, await get_file(ctx, code))


async def cb_rn(ctx, code):
    if not await get_file(ctx, code):
        return
    await set_state(ctx, f"rn:{code}")
    await scr(ctx, card("✏️", "RENAME", "Naya naam bhejo 👇"), Kb([[B("❌ Cancel", f"f:{code}")]]))


async def cb_tg(ctx, code):
    if not await get_file(ctx, code):
        return
    await set_state(ctx, f"tg:{code}")
    await scr(ctx, card("🏷", "TAGS", "Tags bhejo (comma se alag).", "Example:  notes, project", "",
                        "Hatane ke liye  -  bhejo"), Kb([[B("❌ Cancel", f"f:{code}")]]))


async def cb_fvt(ctx, code):
    f = await get_file(ctx, code)
    if not f:
        return
    if f["vault"]:
        if not vault_open(ctx):
            await vault_gate(ctx)
            return
        await db.ex("UPDATE files SET vault=0 WHERE id=?", code)
    else:
        if not await premium_gate(ctx):
            return
        if not ctx.pin:
            await vault_gate(ctx)
            return
        await db.ex("UPDATE files SET vault=1 WHERE id=?", code)
    await file_card(ctx, await get_file(ctx, code), "🔒 Vault me daal di gayi." if not f["vault"] else "🔓 Vault se bahar.")


async def cb_ftr(ctx, code):
    if await get_file(ctx, code):
        await db.ex("UPDATE files SET trashed=? WHERE id=?", int(time.time()), code)
        await alert(ctx, f"🗑 Trash me gayi ({core.TRASH_DAYS} din me permanently delete)")
        await files_list(ctx, "all", 0)


async def cb_fre(ctx, code):
    if await get_file(ctx, code):
        await db.ex("UPDATE files SET trashed=0 WHERE id=?", code)
        await file_card(ctx, await get_file(ctx, code), "♻️ Restore ho gayi.")


async def cb_fdl(ctx, code):
    f = await get_file(ctx, code)
    if f:
        await scr(ctx, card("⚠️", "DELETE FOREVER", f"<b>{esc(trunc(f['name'], 50))}</b>", "", "Ye wapas nahi aayegi. Link bhi kaam nahi karega."),
                  Kb([[B("✅ Haan, delete", f"fdy:{code}"), B("❌ Nahi", f"f:{code}")]]))


async def cb_fdy(ctx, code):
    if await get_file(ctx, code):
        await purge_files([code])
        await alert(ctx, "❌ Permanently delete ho gayi.")
        await files_list(ctx, "all", 0)


async def cb_et(ctx):
    rows = await db.q("SELECT id FROM files WHERE bot_id=? AND uploader=? AND trashed>0", ctx.bot_id, ctx.uid)
    if rows:
        await purge_files([r["id"] for r in rows])
    await alert(ctx, f"🧹 {len(rows)} file(s) delete ho gayi.")
    await storage_screen(ctx)


async def cb_fc(ctx, code):
    if not await get_file(ctx, code):
        return
    rows = await db.q("SELECT id,name FROM cols WHERE bot_id=? AND uploader=? ORDER BY created DESC LIMIT 10", ctx.bot_id, ctx.uid)
    kb = [[B(f"📁 {trunc(r['name'], 30)}", f"fa:{code}:{r['id']}")] for r in rows]
    kb.append([B("🔙 Back", f"f:{code}")])
    await scr(ctx, card("📁", "ADD TO COLLECTION", "Collection chuno 👇" if rows else "Abhi koi collection nahi hai.\nPehle Collections me naya banao."),
              Kb(kb))


async def cb_fa(ctx, code, col):
    f, c = await get_file(ctx, code), await get_col(ctx, col)
    if f and c:
        ok = await add_to_col(col, code)
        await file_card(ctx, f, f"📁 '{esc(trunc(c['name'], 30))}' me add ho gayi." if ok else "⚠️ Collection full (100 files).")


async def cb_sr(ctx):
    await set_state(ctx, "sr")
    await scr(ctx, card("🔍", "SEARCH", "File ka naam, tag ya caption likho 👇"), Kb([[B("❌ Cancel", "mf")]]))


async def cb_vt(ctx):
    await clear_state(ctx)
    await vault_gate(ctx)


async def cb_vl(ctx):
    _unlocked.pop((ctx.bot_id, ctx.uid), None)
    await alert(ctx, "🔐 Vault lock ho gaya.")
    await files_menu(ctx)


async def cb_vr(ctx):
    await scr(ctx, card("⚠️", "RESET PIN", "PIN reset karne par Vault ki saari files", "normal files ban jayengi (vault se bahar).", "",
                        "Pakka reset karna hai?"), Kb([[B("✅ Haan, reset", "vry"), B("❌ Nahi", "vt")]]))


async def cb_vry(ctx):
    await db.batch([("UPDATE ubots SET pin=NULL WHERE bot_id=? AND user_id=?", (ctx.bot_id, ctx.uid)),
                    ("UPDATE files SET vault=0 WHERE bot_id=? AND uploader=?", (ctx.bot_id, ctx.uid))])
    ctx.pin = None
    _unlocked.pop((ctx.bot_id, ctx.uid), None)
    await vault_gate(ctx)


# ----- collections
async def cb_cl(ctx, page="0"):
    await clear_state(ctx)
    page = int(page) if page.isdigit() else 0
    total = await col_count(ctx)
    rows = await db.q("SELECT c.id,c.name,(SELECT COUNT(*) FROM col_items x WHERE x.col_id=c.id) n FROM cols c "
                      "WHERE c.bot_id=? AND c.uploader=? ORDER BY c.created DESC LIMIT ? OFFSET ?",
                      ctx.bot_id, ctx.uid, PER_PAGE, page * PER_PAGE)
    kb = [[B(f"📁 {trunc(r['name'], 26)} • {r['n']}", f"c:{r['id']}")] for r in rows]
    nav = []
    if page > 0:
        nav.append(B("◀ Prev", f"cl:{page - 1}"))
    if (page + 1) * PER_PAGE < total:
        nav.append(B("Next ▶", f"cl:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([B("➕ New Collection", "cn")])
    kb.append([B("🏠 Menu", "home")])
    lim = "" if ctx.premium else f"  (free limit {core.FREE_COLLECTIONS})"
    await scr(ctx, card("📁", "COLLECTIONS", f"📦 {total} collection(s){lim}", "",
                        "👇 Collection chuno" if rows else "Ek link me bahut saari files bhejne ke liye banao."), Kb(kb))


async def cb_cn(ctx):
    if not ctx.premium and await col_count(ctx) >= core.FREE_COLLECTIONS:
        await alert(ctx, f"💎 Free me sirf {core.FREE_COLLECTIONS} collections. {lock_text(ctx)}")
        return
    await set_state(ctx, "cname")
    await scr(ctx, card("📁", "NEW COLLECTION", "Collection ka naam bhejo 👇"), Kb([[B("❌ Cancel", "cl:0")]]))


async def cb_c(ctx, code):
    c = await get_col(ctx, code)
    if not c:
        await alert(ctx, "Collection nahi mili.")
        return
    await clear_state(ctx)
    await col_card(ctx, c)


async def cb_cg(ctx, code):
    l = await get_my_link(ctx, code)
    if not l or l["kind"] != "col":
        return
    await deliver(ctx, ctx.chat, l, own=True, back=f"c:{code}")


async def cb_ca(ctx, code):
    c = await get_col(ctx, code)
    if not c:
        return
    s = await scr(ctx, card("📥", "ADD FILES", f"📁 <b>{esc(trunc(c['name'], 40))}</b>", "",
                            "Files bhejte jao, sab is collection me judti jayengi.", "Khatam hone par ✅ Done dabao."),
                  Kb([[B("✅ Done", f"cdone:{code}")]]))
    await set_state(ctx, f"collect:{code}:{s.message_id}")


async def cb_cdone(ctx, code):
    await clear_state(ctx)
    c = await get_col(ctx, code)
    if c:
        await col_card(ctx, c, "✅ Collection taiyaar!")
    else:
        await show_home(ctx)


async def cb_ci(ctx, code, page="0"):
    c = await get_col(ctx, code)
    if not c:
        return
    page = int(page) if page.isdigit() else 0
    n, _ = await col_stats(code)
    rows = await db.q("SELECT f.id,f.name,f.size FROM col_items ci JOIN files f ON f.id=ci.file_id WHERE ci.col_id=? AND f.trashed=0 "
                      "ORDER BY ci.added LIMIT ? OFFSET ?", code, PER_PAGE, page * PER_PAGE)
    kb = [[B(f"❌ {trunc(r['name'], 28)} • {fsize(r['size'])}", f"cr:{code}:{r['id']}")] for r in rows]
    nav = []
    if page > 0:
        nav.append(B("◀ Prev", f"ci:{code}:{page - 1}"))
    if (page + 1) * PER_PAGE < n:
        nav.append(B("Next ▶", f"ci:{code}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([B("🔙 Back", f"c:{code}")])
    await scr(ctx, card("📋", "ITEMS", f"📁 <b>{esc(trunc(c['name'], 40))}</b>  •  {n} file(s)", "",
                        "File par dabao = collection se hatao" if rows else "Collection khali hai."), Kb(kb))


async def cb_cr(ctx, code, fcode):
    if await get_col(ctx, code):
        await db.ex("DELETE FROM col_items WHERE col_id=? AND file_id=?", code, fcode)
        await cb_ci(ctx, code, "0")


async def cb_crn(ctx, code):
    if await get_col(ctx, code):
        await set_state(ctx, f"crn:{code}")
        await scr(ctx, card("✏️", "RENAME", "Collection ka naya naam bhejo 👇"), Kb([[B("❌ Cancel", f"c:{code}")]]))


async def cb_cdl(ctx, code):
    c = await get_col(ctx, code)
    if c:
        await scr(ctx, card("⚠️", "DELETE COLLECTION", f"<b>{esc(trunc(c['name'], 40))}</b>", "",
                            "Sirf collection aur uska link hatega.", "Andar ki files safe rahengi."),
                  Kb([[B("✅ Haan, delete", f"cdy:{code}"), B("❌ Nahi", f"c:{code}")]]))


async def cb_cdy(ctx, code):
    if await get_col(ctx, code):
        await db.batch([("DELETE FROM col_items WHERE col_id=?", (code,)), ("DELETE FROM links WHERE code=?", (code,)),
                        ("DELETE FROM cols WHERE id=?", (code,))])
        await cb_cl(ctx, "0")


# ----- link settings
async def cb_lk(ctx, code):
    l = await get_my_link(ctx, code)
    if not l:
        await alert(ctx, "Link nahi mila.")
        return
    await clear_state(ctx)
    await lk_screen(ctx, l)


async def cb_lpw(ctx, code):
    if not await premium_gate(ctx) or not await get_my_link(ctx, code):
        return
    await set_state(ctx, f"setpw:{code}")
    await scr(ctx, card("🔐", "SET PASSWORD", "Password bhejo (4 se 32 characters).", "", "Message turant delete ho jayega 🔐"),
              Kb([[B("❌ Cancel", f"lk:{code}")]]))


async def cb_lpx(ctx, code):
    if await get_my_link(ctx, code):
        await db.ex("UPDATE links SET pw=NULL WHERE code=?", code)
        await lk_screen(ctx, await get_my_link(ctx, code))


async def cb_lx(ctx, code, secs=None):
    if not await premium_gate(ctx) or not await get_my_link(ctx, code):
        return
    if secs is None:
        kb = [[B(n, f"lx:{code}:{s}")] for n, s in EXPIRY_PRESETS] + [[B("♾ Expiry OFF", f"lx:{code}:0")], [B("🔙 Back", f"lk:{code}")]]
        await scr(ctx, card("⏳", "LINK EXPIRY", "Itne time baad link band ho jayega.", "(Abhi se ginti shuru)"), Kb(kb))
        return
    s = int(secs) if secs.isdigit() else 0
    await db.ex("UPDATE links SET expires=? WHERE code=?", int(time.time()) + s if s else 0, code)
    await lk_screen(ctx, await get_my_link(ctx, code))


async def cb_lm(ctx, code, n=None):
    if not await premium_gate(ctx) or not await get_my_link(ctx, code):
        return
    if n is None:
        kb = [[B(t, f"lm:{code}:{v}")] for t, v in OPEN_PRESETS] + [[B("♾ Unlimited", f"lm:{code}:0")], [B("🔙 Back", f"lk:{code}")]]
        await scr(ctx, card("🔢", "MAX OPENS", "Link kitni baar khul sakta hai?", "(Tum khud khologe to ginti nahi badhti)"), Kb(kb))
        return
    await db.ex("UPDATE links SET max_opens=? WHERE code=?", int(n) if n.isdigit() else 0, code)
    await lk_screen(ctx, await get_my_link(ctx, code))


async def cb_lpr(ctx, code):
    if not await premium_gate(ctx):
        return
    l = await get_my_link(ctx, code)
    if l:
        await db.ex("UPDATE links SET protect=? WHERE code=?", 0 if l["protect"] else 1, code)
        await lk_screen(ctx, await get_my_link(ctx, code))


async def cb_la(ctx, code, secs=None):
    if not await premium_gate(ctx) or not await get_my_link(ctx, code):
        return
    if secs is None:
        kb = [[B(t, f"la:{code}:{v}")] for t, v in DEL_PRESETS] + [[B("♾ Auto-delete OFF", f"la:{code}:0")], [B("🔙 Back", f"lk:{code}")]]
        await scr(ctx, card("🧹", "AUTO-DELETE", "Dost ke chat me file itni der baad delete ho jayegi."), Kb(kb))
        return
    await db.ex("UPDATE links SET autodel=? WHERE code=?", int(secs) if secs.isdigit() else 0, code)
    await lk_screen(ctx, await get_my_link(ctx, code))


async def cb_lrv(ctx, code):
    l = await get_my_link(ctx, code)
    if l:
        await db.ex("UPDATE links SET revoked=? WHERE code=?", 0 if l["revoked"] else 1, code)
        await lk_screen(ctx, await get_my_link(ctx, code))


async def cb_st(ctx):
    await clear_state(ctx)
    await storage_screen(ctx)


async def cb_rp(ctx, code):
    await report_link(ctx, code)


# ----- clone admin panel
async def admin_panel(ctx, note=None):
    c = ctx.clone
    r = await db.batch([
        ("SELECT COUNT(*) c FROM ubots WHERE bot_id=?", (ctx.bot_id,)),
        ("SELECT COUNT(*) c FROM files WHERE bot_id=? AND trashed=0", (ctx.bot_id,)),
        ("SELECT COALESCE(SUM(opens),0) s FROM links WHERE bot_id=?", (ctx.bot_id,)),
    ])
    used = (await db.one("SELECT COALESCE(SUM(size),0) s FROM files WHERE owner_id=?", ctx.owner))["s"]
    lim = storage_limit(ctx)
    brand_on = not (ctx.premium and c["nobrand"])
    lines = [f"👥 {bd('Users')}  ➜  {r[0][0]['c']}", f"📄 {bd('Files')}  ➜  {r[1][0]['c']}",
             f"👁 {bd('Opens')}  ➜  {r[2][0]['s']}",
             f"💾 {bd('Storage')}  ➜  {fsize(used)}" + (f" / {fsize(lim)}" if lim else ""),
             f"💎 {bd('Plan')}  ➜  {'Premium' if ctx.premium else 'Free'}", "",
             f"🌐 {bd('Public uploads')}  ➜  {'ON' if c['pub'] else 'OFF'}",
             f"🏷 {bd('Branding')}  ➜  {'ON' if brand_on else 'OFF'}", "",
             "⌨️ /ban ID  /unban ID  /revoke CODE", "     /addadmin ID  /deladmin ID"]
    if note:
        lines += ["", note]
    kb = Kb([
        [B(f"🌐 Public uploads: {'ON' if c['pub'] else 'OFF'}", "adp"), B(f"🏷 Branding: {'ON' if brand_on else 'OFF'}", "adb")],
        [B("✏️ Welcome text", "adw"), B("📢 Broadcast 💎", "adc")],
        [B("👥 Users", "adu:0"), B("🏠 Menu", "home")],
    ])
    await scr(ctx, card("🛠", "ADMIN PANEL", *lines), kb)


async def cb_ad(ctx):
    if ctx.admin and not ctx.is_main:
        await clear_state(ctx)
        await admin_panel(ctx)


async def cb_adp(ctx):
    if ctx.admin and not ctx.is_main:
        await db.ex("UPDATE clones SET pub=? WHERE id=?", 0 if ctx.clone["pub"] else 1, ctx.bot_id)
        core.inval(ctx.bot_id)
        ctx.clone = await core.get_clone(ctx.bot_id)
        await admin_panel(ctx, "🌐 Public uploads: " + ("ON — ab sab users upload kar sakte hain" if ctx.clone["pub"] else "OFF"))


async def cb_adb(ctx):
    if not (ctx.admin and not ctx.is_main):
        return
    if ctx.clone["nobrand"]:
        await db.ex("UPDATE clones SET nobrand=0 WHERE id=?", ctx.bot_id)
    else:
        if not await premium_gate(ctx):
            return
        await db.ex("UPDATE clones SET nobrand=1 WHERE id=?", ctx.bot_id)
    core.inval(ctx.bot_id)
    ctx.clone = await core.get_clone(ctx.bot_id)
    await admin_panel(ctx)


async def cb_adw(ctx):
    if ctx.admin and not ctx.is_main:
        await set_state(ctx, "wl")
        await scr(ctx, card("✏️", "WELCOME TEXT", "Naya welcome text bhejo (400 characters tak).", "Default wapas laane ke liye  reset  bhejo."),
                  Kb([[B("❌ Cancel", "ad")]]))


async def cb_adc(ctx):
    if ctx.admin and not ctx.is_main and await premium_gate(ctx):
        await set_state(ctx, "bc")
        await scr(ctx, card("📢", "BROADCAST", "Jo message sabko bhejna hai wo ab bhejo", "(text, photo, video, kuch bhi) 👇"),
                  Kb([[B("❌ Cancel", "ad")]]))


async def cb_adu(ctx, page="0"):
    if not (ctx.admin and not ctx.is_main):
        return
    page = int(page) if page.isdigit() else 0
    total = (await db.one("SELECT COUNT(*) c FROM ubots WHERE bot_id=?", ctx.bot_id))["c"]
    rows = await db.q("SELECT user_id,name,banned FROM ubots WHERE bot_id=? ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                      ctx.bot_id, PER_PAGE, page * PER_PAGE)
    kb = [[B(f"{'✅ Unban' if r['banned'] else '🚫 Ban'} • {trunc(r['name'], 24)}", f"adk:{r['user_id']}")] for r in rows]
    nav = []
    if page > 0:
        nav.append(B("◀ Prev", f"adu:{page - 1}"))
    if (page + 1) * PER_PAGE < total:
        nav.append(B("Next ▶", f"adu:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([B("🔙 Back", "ad")])
    await scr(ctx, card("👥", "USERS", f"👥 {total} user(s)", "", "Naam par dabao = ban / unban"), Kb(kb))


async def cb_adk(ctx, uid):
    if not (ctx.admin and not ctx.is_main) or not uid.isdigit() or int(uid) in await clone_admins(ctx.clone):
        return
    r = await db.one("SELECT banned FROM ubots WHERE bot_id=? AND user_id=?", ctx.bot_id, int(uid))
    if r:
        await db.ex("UPDATE ubots SET banned=? WHERE bot_id=? AND user_id=?", 0 if r["banned"] else 1, ctx.bot_id, int(uid))
    await cb_adu(ctx, "0")


CB = {
    "home": cb_home, "help": cb_help, "mf": cb_files, "f": cb_file, "fg": cb_fg, "fv": cb_fv, "rn": cb_rn, "tg": cb_tg,
    "fvt": cb_fvt, "ftr": cb_ftr, "fre": cb_fre, "fdl": cb_fdl, "fdy": cb_fdy, "et": cb_et, "fc": cb_fc, "fa": cb_fa,
    "sr": cb_sr, "vt": cb_vt, "vl": cb_vl, "vr": cb_vr, "vry": cb_vry,
    "cl": cb_cl, "cn": cb_cn, "c": cb_c, "cg": cb_cg, "ca": cb_ca, "cdone": cb_cdone, "ci": cb_ci, "cr": cb_cr,
    "crn": cb_crn, "cdl": cb_cdl, "cdy": cb_cdy,
    "lk": cb_lk, "lpw": cb_lpw, "lpx": cb_lpx, "lx": cb_lx, "lm": cb_lm, "lpr": cb_lpr, "la": cb_la, "lrv": cb_lrv,
    "st": cb_st, "rp": cb_rp,
    "ad": cb_ad, "adp": cb_adp, "adb": cb_adb, "adw": cb_adw, "adc": cb_adc, "adu": cb_adu, "adk": cb_adk,
}


async def on_callback(ctx, q):
    ub = await touch(ctx)
    msg = q.message
    ctx.q = q
    ctx.chat = msg.chat.id if msg else ctx.uid
    ctx.old = msg.message_id if msg else None
    if ub["banned"]:
        await alert(ctx, "🚫 Tum is bot se banned ho.")
        return
    a, *p = (q.data or "").split(":")
    h = CB.get(a)
    try:
        if h:
            await h(ctx, *p)
    except TypeError:
        log.warning("bad callback data: %r", q.data)
    finally:
        await ack(ctx, q)


# ================= MESSAGES =================
async def on_state_text(ctx, m, st, text):
    chat = m.chat_id
    kind, _, arg = st.partition(":")
    text = text.strip()
    if kind in ("pw", "setpw", "pinset", "unlock"):
        await _del(ctx.bot, chat, m.message_id)
    if kind == "pw":
        await open_link(ctx, chat, arg, pw=text)
    elif kind == "setpw":
        l = await get_my_link(ctx, arg)
        if not l or not ctx.premium:
            return
        if not 4 <= len(text) <= 32:
            await scr(ctx, card("🔐", "SET PASSWORD", "❌ 4 se 32 characters chahiye. Dobara bhejo 👇"), Kb([[B("❌ Cancel", f"lk:{arg}")]]))
            return
        await db.ex("UPDATE links SET pw=? WHERE code=?", core.hash_secret(f"link:{arg}", text), arg)
        await clear_state(ctx)
        await lk_screen(ctx, await get_my_link(ctx, arg))
    elif kind == "pinset":
        if not re.fullmatch(r"\d{4,8}", text):
            await scr(ctx, card("🔒", "SET VAULT PIN", "❌ Sirf 4 se 8 digit. Dobara bhejo 👇"), Kb([[B("❌ Cancel", "home")]]))
            return
        await db.ex("UPDATE ubots SET pin=? WHERE bot_id=? AND user_id=?", core.hash_secret(f"pin:{ctx.bot_id}:{ctx.uid}", text), ctx.bot_id, ctx.uid)
        ctx.pin = "set"
        _unlocked[(ctx.bot_id, ctx.uid)] = time.time() + 600
        await clear_state(ctx)
        await files_list(ctx, "vt", 0)
    elif kind == "unlock":
        key = ("pin", ctx.bot_id, ctx.uid)
        if too_many(key):
            await scr(ctx, card("⛔", "TOO MANY TRIES", "10 min baad try karo."), Kb([[B("🏠 Menu", "home")]]))
            return
        if core.same(core.hash_secret(f"pin:{ctx.bot_id}:{ctx.uid}", text), ctx.pin):
            _unlocked[(ctx.bot_id, ctx.uid)] = time.time() + 600
            await clear_state(ctx)
            await files_list(ctx, "vt", 0)
        else:
            _fails[key].append(time.time())
            await scr(ctx, card("🔒", "VAULT LOCKED", "❌ Galat PIN. Dobara bhejo 👇"), Kb([[B("🔁 Reset PIN", "vr")], [B("❌ Cancel", "home")]]))
    elif kind == "rn":
        f = await get_file(ctx, arg)
        if f and text:
            await db.ex("UPDATE files SET name=? WHERE id=?", clean(text)[:80], arg)
            await clear_state(ctx)
            await file_card(ctx, await get_file(ctx, arg), "✏️ Naam badal gaya.")
    elif kind == "tg":
        f = await get_file(ctx, arg)
        if f:
            tags = "" if text == "-" else clean(text)[:60]
            await db.ex("UPDATE files SET tags=? WHERE id=?", tags, arg)
            await clear_state(ctx)
            await file_card(ctx, await get_file(ctx, arg), "🏷 Tags set ho gaye.")
    elif kind == "cname":
        if not ctx.premium and await col_count(ctx) >= core.FREE_COLLECTIONS:
            await clear_state(ctx)
            await show_home(ctx)
            return
        code = await create_col(ctx, clean(text)[:60] or "Collection")
        await clear_state(ctx)
        await col_card(ctx, await get_col(ctx, code), "✅ Collection ban gayi. 📥 Add Files se files daalo.")
    elif kind == "crn":
        c = await get_col(ctx, arg)
        if c and text:
            await db.ex("UPDATE cols SET name=? WHERE id=?", clean(text)[:60], arg)
            await clear_state(ctx)
            await col_card(ctx, await get_col(ctx, arg), "✏️ Naam badal gaya.")
    elif kind == "sr":
        _qtext[(ctx.bot_id, ctx.uid)] = text[:40]
        await clear_state(ctx)
        await files_list(ctx, "q", 0)
    elif kind == "wl" and ctx.admin and not ctx.is_main:
        await db.ex("UPDATE clones SET welcome=? WHERE id=?", None if text.lower() == "reset" else text[:400], ctx.bot_id)
        core.inval(ctx.bot_id)
        ctx.clone = await core.get_clone(ctx.bot_id)
        await clear_state(ctx)
        await admin_panel(ctx, "✅ Welcome text set.")
    elif kind == "collect":
        await send(ctx.bot, chat, "📥 Files bhejo, ya ✅ Done dabao (/done).")
    else:
        await clear_state(ctx)
        await show_home(ctx)


async def do_broadcast(ctx, chat, mid):
    rows = await db.q("SELECT user_id FROM ubots WHERE bot_id=? AND banned=0", ctx.bot_id)
    ok = fail = 0
    for r in rows:
        for _ in range(2):
            try:
                await ctx.bot.copy_message(r["user_id"], chat, mid)
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(min(e.retry_after, 10) + 1)
            except TelegramError:
                fail += 1
                break
        await asyncio.sleep(0.06)
    try:
        await send(ctx.bot, chat, f"📢 Broadcast poora: ✅ {ok}  ❌ {fail}")
    except TelegramError:
        pass


async def on_message(ctx, m):
    ub = await touch(ctx)
    ctx.chat, ctx.old, ctx.q = m.chat_id, None, None
    if ub["banned"]:
        await send(ctx.bot, m.chat_id, "🚫 Tum is bot se banned ho.")
        return
    if flooded(ctx):
        return
    text = m.text or ""
    if text.startswith("/"):
        await command(ctx, m, text)
        return
    st = await get_state(ctx)
    if st == "bc" and ctx.admin and not ctx.is_main:
        await clear_state(ctx)
        core.spawn(do_broadcast(ctx, m.chat_id, m.message_id))
        await send(ctx.bot, m.chat_id, "📢 Broadcast shuru ho gaya...")
        return
    med = extract_media(m)
    if med:
        await on_upload(ctx, m, med, st)
        return
    if text and st:
        await on_state_text(ctx, m, st, text)
        return
    await show_home(ctx)


async def command(ctx, m, text):
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    chat = m.chat_id
    st = await get_state(ctx)
    if cmd == "done" and st and st.startswith("collect:"):
        await cb_cdone(ctx, st.split(":")[1])
        return
    if st:
        await clear_state(ctx)
    if cmd == "start":
        if arg and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", arg):
            await open_link(ctx, chat, arg)
        else:
            await show_home(ctx)
    elif cmd == "help":
        await show_help(ctx)
    elif cmd in ("files", "myfiles") and can_upload(ctx):
        await files_menu(ctx)
    elif cmd in ("cols", "collections") and can_upload(ctx):
        await cb_cl(ctx)
    elif cmd == "vault" and can_upload(ctx):
        await vault_gate(ctx)
    elif cmd in ("storage", "stats") and can_upload(ctx):
        await storage_screen(ctx)
    elif cmd == "admin" and ctx.admin and not ctx.is_main:
        await admin_panel(ctx)
    elif ctx.admin and not ctx.is_main and cmd in ("ban", "unban", "revoke", "addadmin", "deladmin"):
        await admin_command(ctx, chat, cmd, arg)
    else:
        await show_home(ctx)


async def admin_command(ctx, chat, cmd, arg):
    say = lambda t: send(ctx.bot, chat, t)
    if cmd == "revoke":
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", arg):
            await say("Use: /revoke CODE")
            return
        n = await db.run("UPDATE links SET revoked=1 WHERE code=? AND bot_id=?", arg, ctx.bot_id)
        await say("🚫 Link band kar diya." if n else "Link nahi mila.")
        return
    if not arg.isdigit():
        await say(f"Use: /{cmd} USERID")
        return
    uid = int(arg)
    if cmd in ("ban", "unban"):
        if uid in await clone_admins(ctx.clone):
            await say("Admin ko ban nahi kar sakte.")
            return
        await db.ex("UPDATE ubots SET banned=? WHERE bot_id=? AND user_id=?", 1 if cmd == "ban" else 0, ctx.bot_id, uid)
        await say(f"✅ {cmd} done: {uid}")
    elif ctx.uid != ctx.clone["owner_id"]:
        await say("Sirf bot owner admin add/remove kar sakta hai.")
    elif cmd == "addadmin":
        await db.ex("INSERT OR IGNORE INTO admins(bot_id,user_id) VALUES(?,?)", ctx.bot_id, uid)
        adm_refresh(ctx.bot_id)
        await say("✅ Admin add ho gaya.")
    else:
        await db.ex("DELETE FROM admins WHERE bot_id=? AND user_id=?", ctx.bot_id, uid)
        adm_refresh(ctx.bot_id)
        await say("✅ Admin hata diya.")


# ================= CLONE ENTRY =================
async def handle_clone(clone, data):
    bot = await core.get_bot(core.dec(clone["token"]))
    upd = Update.de_json(data, bot)
    if upd.callback_query:
        q = upd.callback_query
        ctx = await make_ctx(bot, clone, q.from_user)
        if clone["status"] != "active":
            await ack(ctx, q, "⏸ Ye bot abhi band hai.", True)
            return
        await on_callback(ctx, q)
    elif upd.message:
        m = upd.message
        if m.chat.type != "private" or not m.from_user:
            return
        ctx = await make_ctx(bot, clone, m.from_user)
        if clone["status"] != "active":
            await send(bot, m.chat_id, "⏸ Ye bot abhi band hai.")
            return
        await on_message(ctx, m)
