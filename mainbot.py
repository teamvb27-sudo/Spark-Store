"""Main (Mother) bot: Spark Store + clone banana/manage + Premium (Stars) + Owner panel."""
import asyncio
import csv
import io
import re
import time

from telegram import Bot, LabeledPrice, Update
from telegram.error import InvalidToken, TelegramError

import core
import db
import store
import ui
from ui import B, Btn, Kb, _del, bd, card, esc, fsize, trunc
from store import scr, ack, alert, set_state, get_state, clear_state

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
ALLOWED = ["message", "callback_query"]
STATUS = {"active": "🟢 Active", "paused": "⏸ Paused", "dead": "💀 Dead", "banned": "🚫 Banned"}

OHELP = """/owner – panel
/clones [page], /search text
/pause ID, /resume ID, /remove ID   (clone/bot ID)
/ban OWNERID, /unban OWNERID
/give OWNERID DIN – free premium
/broadcast (reply) – sab main-bot users ko
/dm OWNERID text
/newcoupon CODE DIN USES, /delcoupon CODE, /coupons
/payments, /stats, /reports
/takedown CODE – link + file hatao + blocklist
/revoke CODE – sirf link band
/maintenance on|off – naye clones band/chalu
/backup – CSV files (tokens nahi)"""


# ================= ENTRY =================
async def handle(data):
    bot = await core.main_bot()
    upd = Update.de_json(data, bot)
    if upd.pre_checkout_query:
        await pre_checkout(upd.pre_checkout_query, bot)
    elif upd.callback_query:
        await on_cb(bot, upd.callback_query)
    elif upd.message and upd.message.chat.type == "private" and upd.message.from_user:
        await on_msg(bot, upd.message)


async def pre_checkout(q, bot):
    p = q.invoice_payload or ""
    ok = p.startswith("plan:") and p[5:].isdigit() and int(p[5:]) in core.PLANS and q.total_amount == core.PLANS[int(p[5:])]
    await bot.answer_pre_checkout_query(q.id, ok=ok, error_message=None if ok else "Invalid plan")


async def on_msg(bot, m):
    u = m.from_user
    await core.ensure_owner(u)
    o = await db.one("SELECT banned FROM owners WHERE id=?", u.id)
    if o["banned"] and u.id != core.OWNER_ID:
        return
    ctx = await store.make_ctx(bot, None, u)
    ctx.chat = m.chat_id
    if m.successful_payment:
        await paid(ctx, m)
        return
    text = m.text or ""
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0][1:].split("@")[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("create", "mybot", "upgrade", "coupon", "cancel"):
            await clear_state(ctx)
            if cmd == "create":
                await start_create(ctx)
            elif cmd == "mybot":
                await show_my(ctx)
            elif cmd == "upgrade":
                await show_plans(ctx)
            elif cmd == "coupon":
                if rest:
                    await redeem(ctx, rest)
                else:
                    await ask_coupon(ctx)
            else:
                await store.show_home(ctx)
            return
        if u.id == core.OWNER_ID and cmd in OWNER_CMDS:
            await clear_state(ctx)
            await owner_cmd(ctx, m, cmd, rest)
            return
        await store.on_message(ctx, m)
        return
    st = await get_state(ctx)
    if st and text and st.split(":")[0] in ("tok", "cpn"):
        kind = st.split(":")[0]
        await _del(bot, m.chat_id, m.message_id)
        await clear_state(ctx)
        if kind == "tok":
            await take_token(ctx, text.strip())
        else:
            await redeem(ctx, text.strip())
        return
    await store.on_message(ctx, m)


# ================= CREATE / MANAGE STORE BOTS =================
async def joined(bot, uid):
    if not core.FORCE_JOIN:
        return True
    try:
        mem = await bot.get_chat_member(core.FORCE_JOIN, uid)
        return mem.status in ("member", "administrator", "creator", "owner")
    except TelegramError:
        return True


async def ask_token(ctx, err=None):
    lines = ["①  @BotFather kholo",
             "②  /newbot bhejo – naam aur username do",
             f"③  Jo {bd('TOKEN')} mile (123456:ABC...) wo yahan paste karo", "",
             "🔐 Token encrypted store hota hai",
             "🗑 Tumhara token wala message turant delete ho jayega"]
    if err:
        lines += ["", err]
    await scr(ctx, card("🛠", "CREATE STORE BOT", *lines),
              Kb([[Btn("🤖 Open BotFather", url="https://t.me/BotFather")], [B("❌ Cancel", "home")]]))
    await set_state(ctx, "tok")


async def ask_coupon(ctx, err=None):
    lines = ["🎟 Apna coupon code bhejo", "", "Example:  FRIEND30"]
    if err:
        lines += ["", err]
    await scr(ctx, card("🎟", "COUPON", *lines), Kb([[B("❌ Cancel", "home")]]))
    await set_state(ctx, "cpn")


async def start_create(ctx):
    uid = ctx.uid
    if uid != core.OWNER_ID and await db.kv_get("maintenance", "0") == "1":
        await scr(ctx, card("🛠", "MAINTENANCE", "Abhi naye bots banana band hai.", "Thodi der baad aao."), Kb([[B("🏠 Menu", "home")]]))
        return
    if not await joined(ctx.bot, uid):
        kb = Kb([[Btn("📢 Join Channel", url=f"https://t.me/{core.FORCE_JOIN.lstrip('@')}")],
                 [B("✅ Join kar liya", "create")], [B("🏠 Menu", "home")]])
        await scr(ctx, card("📢", "JOIN REQUIRED", f"Pehle {esc(core.FORCE_JOIN)} join karo,", "phir dobara try karo."), kb)
        return
    n = (await db.one("SELECT COUNT(*) c FROM clones WHERE owner_id=?", uid))["c"]
    lim = 99 if uid == core.OWNER_ID else (core.PREMIUM_CLONES if ctx.premium else core.FREE_CLONES)
    if n >= lim:
        lines = [f"Tumhara bot limit ({lim}) poora ho gaya."]
        rows = [[B("🏠 Menu", "home")]]
        if not ctx.premium:
            lines += ["", f"💎 Premium me {core.PREMIUM_CLONES} bots bana sakte ho."]
            rows.insert(0, [B("💎 Premium lo", "up")])
        await scr(ctx, card("⚠️", "LIMIT FULL", *lines), Kb(rows))
        return
    await ask_token(ctx)


async def take_token(ctx, token):
    u, bot = ctx.user, ctx.bot
    if not TOKEN_RE.match(token) or token == core.MAIN_TOKEN:
        await ask_token(ctx, "❌ Token galat lag raha hai. Dobara bhejo.")
        return
    err = me = None
    tb = Bot(token)
    try:
        async with tb:
            me = await tb.get_me()
            if me.id == core.MAIN_ID or await db.one("SELECT id FROM clones WHERE id=?", me.id):
                err = "⚠️ Ye bot pehle se registered hai."
            else:
                await tb.set_webhook(url=f"{core.BASE_URL}/wh/{me.id}", secret_token=core.secret_for(me.id),
                                     allowed_updates=ALLOWED, drop_pending_updates=True)
    except InvalidToken:
        err = "❌ Token invalid hai. Check karke dobara bhejo."
    except TelegramError:
        err = "❌ Telegram se connect nahi ho paya. Thodi der baad try karo."
    if err:
        await ask_token(ctx, err)
        return
    await db.ex("INSERT INTO clones(id,token,username,name,owner_id,status,created) VALUES(?,?,?,?,?,'active',?)",
                me.id, core.enc(token), me.username, me.full_name, u.id, int(time.time()))
    lines = [f"🤖 @{esc(me.username)}", "",
             "①  Pehle apna bot kholo aur /start dabao (tum admin ho)",
             "②  File bhejo → link milega",
             "③  🛠 Admin Panel me public uploads, welcome, branding set karo",
             f"④  Link kholne ka test kisi {bd('DUSRE')} account se karo"]
    o = await db.one("SELECT trial_used FROM owners WHERE id=?", u.id)
    if not o["trial_used"] and not ctx.premium:
        await core.add_premium(u.id, core.TRIAL_DAYS)
        await db.ex("UPDATE owners SET trial_used=1 WHERE id=?", u.id)
        lines += ["", f"🎁 {core.TRIAL_DAYS} din ka Premium trial free mila!"]
    await scr(ctx, card("✅", "BOT READY", *lines),
              Kb([[Btn("🤖 Open My Bot", url=f"https://t.me/{me.username}")], [B("🏠 Menu", "home")]]))
    await core.log(f"🆕 Clone: @{me.username} ({me.id}) by {u.full_name} ({u.id})")


async def show_my(ctx):
    rows = await db.q("SELECT c.id,c.username,c.status,(SELECT COUNT(*) FROM ubots x WHERE x.bot_id=c.id) n FROM clones c WHERE owner_id=?", ctx.uid)
    until = await core.premium_until(ctx.uid)
    if ctx.premium:
        plan = "💎 Premium" + (f" • {core.fdate(until)} tak" if until and ctx.uid != core.OWNER_ID else "")
    else:
        plan = f"🆓 Free • {fsize(core.FREE_STORAGE)}"
    if not rows:
        await scr(ctx, card("🤖", "MY BOTS", "Abhi tumhara koi bot nahi hai.", "", plan),
                  Kb([[B("➕ Create Store Bot", "create")], [B("🏠 Menu", "home")]]))
        return
    lines = [f"{STATUS.get(r['status'], '⚪')}  @{esc(r['username'])}  •  👥 {r['n']}" for r in rows]
    kb = Kb([[B(f"⚙️ @{r['username']}", f"bk:{r['id']}")] for r in rows] + [[B("🏠 Menu", "home")]])
    await scr(ctx, card("🤖", "MY BOTS", *lines, "", plan, "", "👇 Manage karne ke liye bot chuno"), kb)


async def own_clone(uid, cid):
    c = await db.one("SELECT * FROM clones WHERE id=?", cid)
    return c if c and (c["owner_id"] == uid or uid == core.OWNER_ID) else None


async def show_bot(ctx, cid):
    c = await own_clone(ctx.uid, cid)
    if not c:
        await show_my(ctx)
        return
    r = await db.batch([
        ("SELECT COUNT(*) c FROM ubots WHERE bot_id=?", (cid,)),
        ("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files WHERE bot_id=? AND trashed=0", (cid,)),
    ])
    text = card("🤖", "BOT DETAILS", f"◈ Bot: @{esc(c['username'])}", f"◈ Status: {STATUS.get(c['status'], c['status'])}",
                f"◈ Users: {r[0][0]['c']}", f"◈ Files: {r[1][0]['c']}  •  {fsize(r[1][0]['s'])}",
                f"◈ Public uploads: {'ON' if c['pub'] else 'OFF'}", f"◈ Bana: {core.fdate(c['created'])}")
    rows = [[Btn("🤖 Open Bot", url=f"https://t.me/{c['username']}")]]
    if c["status"] in ("active", "paused"):
        rows.append([B("⏸ Pause" if c["status"] == "active" else "▶️ Resume", f"pz:{cid}")])
    rows.append([B("🗑 Delete", f"rm:{cid}")])
    rows.append([B("🔙 Back", "my")])
    await scr(ctx, text, Kb(rows))


async def remove_clone(cid):
    c = await db.one("SELECT * FROM clones WHERE id=?", cid)
    if not c:
        return False
    token = core.dec(c["token"])
    try:
        async with Bot(token) as tb:
            await tb.delete_webhook()
    except Exception:
        pass
    await core.drop_bot(token)
    ids = [r["id"] for r in await db.q("SELECT id FROM files WHERE bot_id=?", cid)]
    if ids:
        await store.purge_files(ids)
    await db.batch([(f"DELETE FROM {t} WHERE bot_id=?", (cid,)) for t in ("admins", "ubots", "links", "state", "jobs", "reports")]
                   + [("DELETE FROM col_items WHERE col_id IN (SELECT id FROM cols WHERE bot_id=?)", (cid,)),
                      ("DELETE FROM cols WHERE bot_id=?", (cid,)), ("DELETE FROM opens WHERE bot_id=?", (cid,)),
                      ("DELETE FROM clones WHERE id=?", (cid,))])
    core.inval(cid)
    return True


# ================= PAYMENTS (Stars) =================
async def show_plans(ctx):
    until = await core.premium_until(ctx.uid)
    status = (f"✅ Premium active: {core.fdate(until)} tak\n    (renew karoge to din jud jayenge)"
              if until > time.time() else "🆓 Abhi Free plan par ho")
    text = card("💎", "PREMIUM",
                f"◈ Storage {fsize(core.FREE_STORAGE)} ➜ {fsize(core.PREMIUM_STORAGE)}",
                "◈ Password • Expiry • One-time links", "◈ Protected + Auto-delete files",
                "◈ Private Vault (PIN) • Unlimited collections", "◈ Detailed stats",
                f"◈ {core.PREMIUM_CLONES} store bots • Broadcast • Branding hatao", "", status, "", "👇 Plan chuno")
    rows = [[B(f"⭐ {stars} Stars  •  {days} din", f"buy:{days}")] for days, stars in core.PLANS.items()]
    rows.append([B("🏠 Menu", "home")])
    await scr(ctx, text, Kb(rows))


async def send_invoice(ctx, days):
    chat = ctx.chat
    for i in {ctx.old, ui._last.get((ctx.bot.id, chat))} - {None}:
        await _del(ctx.bot, chat, i)
    ctx.old = None
    s = await ctx.bot.send_invoice(chat_id=chat, title=f"Spark Store Premium – {days} din", description=f"{days} din ka Premium",
                                   payload=f"plan:{days}", currency="XTR", prices=[LabeledPrice(f"{days} din Premium", core.PLANS[days])],
                                   reply_markup=Kb([[Btn("⭐ Pay Now", pay=True)], [B("🔙 Back", "up")]]))
    ui._last[(ctx.bot.id, chat)] = s.message_id


async def paid(ctx, m):
    sp, u = m.successful_payment, m.from_user
    p = sp.invoice_payload or ""
    days = int(p[5:]) if p.startswith("plan:") and p[5:].isdigit() else 0
    if days not in core.PLANS:
        await core.notify(core.OWNER_ID, f"⚠️ Unknown payment payload from {u.id}: {p} (⭐{sp.total_amount}) – manual check karo.")
        return
    if await db.one("SELECT id FROM payments WHERE charge_id=?", sp.telegram_payment_charge_id):
        return
    await db.ex("INSERT INTO payments(owner_id,stars,days,charge_id,ts,source) VALUES(?,?,?,?,?,'stars')",
                u.id, sp.total_amount, days, sp.telegram_payment_charge_id, int(time.time()))
    new = await core.add_premium(u.id, days)
    ctx.premium = True
    await scr(ctx, card("🎉", "PAYMENT SUCCESS", f"💎 Premium {core.fdate(new)} tak active", f"⭐ {sp.total_amount} Stars  •  {days} din", "",
                        "Shukriya! 🙏"), store.home_kb(ctx))
    await core.notify(core.OWNER_ID, f"💰 {u.full_name} ({u.id}) ne ⭐{sp.total_amount} diye ({days} din)")
    await core.log(f"💰 Payment ⭐{sp.total_amount} from {u.id} ({days}d)")


async def redeem(ctx, code):
    uid = ctx.uid
    code = code.upper()
    c = await db.one("SELECT * FROM coupons WHERE code=?", code)
    if not c or c["used"] >= c["max_uses"]:
        await ask_coupon(ctx, "❌ Coupon invalid ya khatam.")
        return
    if await db.one("SELECT 1 x FROM coupon_uses WHERE code=? AND owner_id=?", code, uid):
        await ask_coupon(ctx, "⚠️ Ye coupon tum pehle use kar chuke ho.")
        return
    if not await db.run("UPDATE coupons SET used=used+1 WHERE code=? AND used<max_uses", code):
        await ask_coupon(ctx, "❌ Coupon khatam ho gaya.")
        return
    await db.ex("INSERT INTO coupon_uses(code,owner_id) VALUES(?,?)", code, uid)
    new = await core.add_premium(uid, c["days"])
    ctx.premium = True
    await scr(ctx, card("🎉", "COUPON APPLIED", f"◈ +{c['days']} din Premium", f"◈ Premium {core.fdate(new)} tak"), store.home_kb(ctx))


# ================= CALLBACKS =================
async def on_cb(bot, q):
    u = q.from_user
    await core.ensure_owner(u)
    o = await db.one("SELECT banned FROM owners WHERE id=?", u.id)
    ctx = await store.make_ctx(bot, None, u)
    ctx.q = q
    if o["banned"] and u.id != core.OWNER_ID:
        await alert(ctx, "Banned")
        return
    msg = q.message
    ctx.chat = msg.chat.id if msg else u.id
    ctx.old = msg.message_id if msg else None
    a, *p = (q.data or "").split(":")
    h = MCB.get(a)
    if not h:
        await store.on_callback(ctx, q)
        return
    try:
        await clear_state(ctx)
        await h(ctx, *p)
    except TypeError:
        pass
    finally:
        await ack(ctx, q)


async def cb_create(ctx):
    await start_create(ctx)


async def cb_my(ctx):
    await show_my(ctx)


async def cb_up(ctx):
    await show_plans(ctx)


async def cb_coupon(ctx):
    await ask_coupon(ctx)


async def cb_buy(ctx, days):
    if days.isdigit() and int(days) in core.PLANS:
        await send_invoice(ctx, int(days))


async def cb_bk(ctx, cid):
    if cid.isdigit():
        await show_bot(ctx, int(cid))


async def cb_pz(ctx, cid):
    c = await own_clone(ctx.uid, int(cid)) if cid.isdigit() else None
    if c and c["status"] in ("active", "paused"):
        await db.ex("UPDATE clones SET status=? WHERE id=?", "paused" if c["status"] == "active" else "active", c["id"])
        core.inval(c["id"])
        await show_bot(ctx, c["id"])


async def cb_rm(ctx, cid):
    c = await own_clone(ctx.uid, int(cid)) if cid.isdigit() else None
    if c:
        await scr(ctx, card("⚠️", "DELETE BOT", f"@{esc(c['username'])} delete karna hai?", "",
                            "Is bot ki saari files, links aur users", "hamesha ke liye jayenge."),
                  Kb([[B("✅ Haan, delete", f"rmy:{c['id']}"), B("❌ Nahi", f"bk:{c['id']}")]]))


async def cb_rmy(ctx, cid):
    c = await own_clone(ctx.uid, int(cid)) if cid.isdigit() else None
    if c:
        await remove_clone(c["id"])
        await core.log(f"🗑 Clone removed: @{c['username']} by {ctx.uid}")
        await scr(ctx, card("🗑", "DELETED", f"@{esc(c['username'])} delete ho gaya."), store.home_kb(ctx))


async def mod_result(ctx, text):
    await ack(ctx, ctx.q, text, True)
    msg = ctx.q.message
    if msg:
        try:
            await ctx.bot.edit_message_text(text, chat_id=msg.chat.id, message_id=msg.message_id)
        except TelegramError:
            pass


async def cb_rt(ctx, code):
    if ctx.uid == core.OWNER_ID:
        n, l = await store.takedown(code)
        await mod_result(ctx, f"🗑 Takedown done: {n} file(s) hata di gayi." if l else "Link nahi mila.")


async def cb_rd(ctx, code):
    if ctx.uid == core.OWNER_ID:
        await store.dismiss_report(code)
        await mod_result(ctx, "✅ Report dismiss kiya, link wapas chalu.")


async def cb_rb(ctx, code):
    if ctx.uid == core.OWNER_ID:
        l = await db.one("SELECT * FROM links WHERE code=?", code)
        if l:
            await store.ban_uploader(l)
            await mod_result(ctx, f"🚫 Uploader {l['uploader']} ban ho gaya.")
        else:
            await alert(ctx, "Link nahi mila.")


# ================= OWNER PANEL =================
def panel_kb():
    return Kb([[B("📊 Dashboard", "o:dash"), B("📋 Bots", "o:cl:0")],
               [B("🚩 Reports", "o:rep"), B("💰 Payments", "o:pay")],
               [B("🛠 Maintenance", "o:maint"), B("📥 Backup", "o:backup")],
               [B("📖 Commands", "o:help"), B("🏠 Menu", "home")]])


def oback():
    return Kb([[B("🔙 Panel", "o:menu")]])


async def panel_text():
    m = await db.kv_get("maintenance", "0")
    return card("👑", "OWNER PANEL", "◈ Control center", f"◈ Naye bots banana: {'🔴 BAND' if m == '1' else '🟢 chalu'}")


async def dashboard():
    now = int(time.time())
    r = await db.batch([
        ("SELECT status,COUNT(*) c FROM clones GROUP BY status", ()),
        ("SELECT COUNT(*) c FROM owners", ()),
        ("SELECT COUNT(*) c FROM ubots", ()),
        ("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files WHERE trashed=0", ()),
        ("SELECT COALESCE(SUM(opens),0) s FROM links", ()),
        ("SELECT COALESCE(SUM(stars),0) s FROM payments", ()),
        ("SELECT COALESCE(SUM(stars),0) s FROM payments WHERE ts>?", (now - 30 * 86400,)),
        ("SELECT COUNT(*) c FROM owners WHERE expiry>?", (now,)),
        ("SELECT COUNT(DISTINCT code) c FROM reports WHERE status='open'", ()),
        ("SELECT COUNT(*) c FROM blocked", ()),
    ])
    st = {x["status"]: x["c"] for x in r[0]}
    maint = await db.kv_get("maintenance", "0")
    return card("📊", "DASHBOARD",
                f"🤖 Bots: {sum(st.values())}",
                f"     🟢 {st.get('active', 0)}  ⏸ {st.get('paused', 0)}  💀 {st.get('dead', 0)}  🚫 {st.get('banned', 0)}",
                f"👤 Owners: {r[1][0]['c']}   👥 Store users: {r[2][0]['c']}",
                f"📄 Files: {r[3][0]['c']}  •  💾 {fsize(r[3][0]['s'])}",
                f"👁 Total opens: {r[4][0]['s']}",
                f"💎 Premium owners: {r[7][0]['c']}",
                f"⭐ Kamai: {r[5][0]['s']} total  |  {r[6][0]['s']} (30 din)",
                f"🚩 Open reports: {r[8][0]['c']}   ⛔ Blocklist: {r[9][0]['c']}",
                f"🛠 Maintenance: {'ON' if maint == '1' else 'off'}")


async def clones_page(page=0):
    rows = await db.q(
        "SELECT c.id,c.username,c.owner_id,c.status,(SELECT COUNT(*) FROM ubots u WHERE u.bot_id=c.id) n "
        "FROM clones c ORDER BY c.created DESC LIMIT 16 OFFSET ?", page * 15)
    more = len(rows) > 15
    rows = rows[:15]
    if not rows:
        return card("📋", "BOTS", "Koi clone bot nahi."), False
    lines = [f"{r['id']} • @{esc(r['username'])} • 👤{r['owner_id']} • {r['status']} • 👥{r['n']}" for r in rows]
    return card("📋", f"BOTS P{page + 1}", *lines), more


async def payments_text():
    rows = await db.q("SELECT owner_id,stars,days,ts FROM payments ORDER BY id DESC LIMIT 15")
    if not rows:
        return card("💰", "PAYMENTS", "Abhi koi payment nahi.")
    return card("💰", "PAYMENTS", *[f"{core.fdate(r['ts'])} • 👤{r['owner_id']} • ⭐{r['stars']} • {r['days']}d" for r in rows])


async def reports_screen():
    rows = await db.q("SELECT code, COUNT(*) n FROM reports WHERE status='open' GROUP BY code ORDER BY n DESC LIMIT 8")
    if not rows:
        return card("🚩", "REPORTS", "Koi open report nahi. 🎉"), Kb([[B("🔙 Panel", "o:menu")]])
    kb = []
    lines = []
    for r in rows:
        l = await db.one("SELECT * FROM links WHERE code=?", r["code"])
        name = esc(trunc(await store.link_name(l), 30)) if l else "?"
        lines.append(f"• {name}  —  {r['n']} report(s)\n   <code>{r['code']}</code>")
        kb.append([B(f"🗑 {r['code']}", f"rt:{r['code']}"), B("✅ Dismiss", f"rd:{r['code']}"), B("🚫 Ban", f"rb:{r['code']}")])
    kb.append([B("🔙 Panel", "o:menu")])
    return card("🚩", "REPORTS", *lines), Kb(kb)


async def send_csv(bot, chat, name, rows):
    if not rows:
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(rows[0].keys())
    for r in rows:
        w.writerow(r.values())
    await bot.send_document(chat, io.BytesIO(buf.getvalue().encode()), filename=name)


async def backup(bot, chat):
    await send_csv(bot, chat, "clones.csv", await db.q("SELECT id,username,name,owner_id,status,created,pub FROM clones"))
    await send_csv(bot, chat, "owners.csv", await db.q("SELECT * FROM owners"))
    await send_csv(bot, chat, "payments.csv", await db.q("SELECT * FROM payments"))
    await send_csv(bot, chat, "blocked.csv", await db.q("SELECT * FROM blocked"))


async def cb_o(ctx, a="menu", page="0"):
    if ctx.uid != core.OWNER_ID:
        return
    if a == "menu":
        await scr(ctx, await panel_text(), panel_kb())
    elif a == "dash":
        await scr(ctx, await dashboard(), oback())
    elif a == "cl":
        pg = int(page) if page.isdigit() else 0
        text, more = await clones_page(pg)
        nav = ([B("◀ Prev", f"o:cl:{pg - 1}")] if pg > 0 else []) + ([B("Next ▶", f"o:cl:{pg + 1}")] if more else [])
        await scr(ctx, text, Kb(([nav] if nav else []) + [[B("🔙 Panel", "o:menu")]]))
    elif a == "rep":
        text, kb = await reports_screen()
        await scr(ctx, text, kb)
    elif a == "pay":
        await scr(ctx, await payments_text(), oback())
    elif a == "maint":
        on = await db.kv_get("maintenance", "0") != "1"
        await db.kv_set("maintenance", "1" if on else "0")
        await scr(ctx, await panel_text(), panel_kb())
    elif a == "backup":
        await backup(ctx.bot, ctx.chat)
        await scr(ctx, card("📥", "BACKUP", "Files bhej di gayi hain.", "(Tokens include nahi hain)"), oback())
    elif a == "help":
        await scr(ctx, card("👑", "OWNER COMMANDS", esc(OHELP)), oback())


MCB = {"create": cb_create, "my": cb_my, "up": cb_up, "coupon": cb_coupon, "buy": cb_buy, "bk": cb_bk, "pz": cb_pz,
       "rm": cb_rm, "rmy": cb_rmy, "o": cb_o, "rt": cb_rt, "rd": cb_rd, "rb": cb_rb}

OWNER_CMDS = {"owner", "admin", "panel", "ownerhelp", "stats", "clones", "search", "pause", "resume", "remove", "ban", "unban",
              "give", "broadcast", "dm", "newcoupon", "delcoupon", "coupons", "payments", "maintenance", "backup",
              "takedown", "revoke", "reports"}


async def broadcast_all(bot, chat, from_chat, mid):
    rows = await db.q("SELECT id FROM owners WHERE banned=0")
    ok = fail = 0
    for r in rows:
        try:
            await bot.copy_message(r["id"], from_chat, mid)
            ok += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.06)
    await bot.send_message(chat, f"📢 Broadcast poora: ✅ {ok} ❌ {fail}")


async def owner_cmd(ctx, m, cmd, rest):
    bot, chat, a = ctx.bot, m.chat_id, rest.split()

    async def say(t, **kw):
        return await bot.send_message(chat, t, **kw)

    if cmd in ("owner", "admin", "panel"):
        await scr(ctx, await panel_text(), panel_kb())
    elif cmd == "ownerhelp":
        await say(OHELP)
    elif cmd == "stats":
        await scr(ctx, await dashboard(), oback())
    elif cmd == "reports":
        text, kb = await reports_screen()
        await scr(ctx, text, kb)
    elif cmd == "clones":
        text, _ = await clones_page(int(a[0]) if a and a[0].isdigit() else 0)
        await scr(ctx, text, oback())
    elif cmd == "search":
        k = rest.lstrip("@")
        rows = await db.q("SELECT id,username,owner_id,status FROM clones WHERE username LIKE ? OR CAST(owner_id AS TEXT)=? OR CAST(id AS TEXT)=? LIMIT 15",
                          f"%{k}%", k, k)
        await say("\n".join(f"{r['id']} | @{r['username']} | {r['owner_id']} | {r['status']}" for r in rows) if rows else "Kuch nahi mila.")
    elif cmd in ("pause", "resume"):
        if not a or not a[0].isdigit():
            await say(f"Use: /{cmd} BOT_ID")
            return
        await db.ex("UPDATE clones SET status=? WHERE id=?", "paused" if cmd == "pause" else "active", int(a[0]))
        core.inval(int(a[0]))
        await say("✅ Done.")
    elif cmd == "remove":
        if not a or not a[0].isdigit():
            await say("Use: /remove BOT_ID")
            return
        c = await db.one("SELECT username,owner_id FROM clones WHERE id=?", int(a[0]))
        if c and await remove_clone(int(a[0])):
            await say("🗑 Bot remove ho gaya.")
            await core.notify(c["owner_id"], f"ℹ️ Tumhara bot @{c['username']} admin ne remove kar diya.")
            await core.log(f"🗑 Owner removed clone {a[0]}")
        else:
            await say("Bot nahi mila.")
    elif cmd in ("ban", "unban"):
        if not a or not a[0].isdigit():
            await say(f"Use: /{cmd} OWNERID")
            return
        uid = int(a[0])
        ids = [r["id"] for r in await db.q("SELECT id FROM clones WHERE owner_id=?", uid)]
        if cmd == "ban":
            await db.batch([("INSERT OR IGNORE INTO owners(id,name,username,joined) VALUES(?,?,?,?)", (uid, "?", None, int(time.time()))),
                            ("UPDATE owners SET banned=1 WHERE id=?", (uid,)),
                            ("UPDATE clones SET status='banned' WHERE owner_id=? AND status IN ('active','paused')", (uid,))])
        else:
            await db.batch([("UPDATE owners SET banned=0 WHERE id=?", (uid,)),
                            ("UPDATE clones SET status='active' WHERE owner_id=? AND status='banned'", (uid,))])
        for i in ids:
            core.inval(i)
        await say(f"✅ {cmd} done: {uid}")
    elif cmd == "give":
        if len(a) < 2 or not a[0].isdigit() or not a[1].lstrip("-").isdigit():
            await say("Use: /give OWNERID DIN")
            return
        if not await db.one("SELECT 1 x FROM owners WHERE id=?", int(a[0])):
            await say("Is user ne main bot start nahi kiya.")
            return
        new = await core.add_premium(int(a[0]), int(a[1]))
        await core.notify(int(a[0]), f"🎁 Tumhe {a[1]} din ka Premium mila! {core.fdate(new)} tak.")
        await say(f"✅ Premium {core.fdate(new)} tak.")
    elif cmd == "broadcast":
        if not m.reply_to_message:
            await say("Message par Reply karke /broadcast likho.")
            return
        core.spawn(broadcast_all(bot, chat, chat, m.reply_to_message.message_id))
        await say("📢 Broadcast shuru...")
    elif cmd == "dm":
        p = rest.split(maxsplit=1)
        if len(p) < 2 or not p[0].isdigit():
            await say("Use: /dm OWNERID text")
            return
        await say("✅ Bhej diya." if await core.notify(int(p[0]), p[1]) else "❌ Nahi gaya.")
    elif cmd == "newcoupon":
        if len(a) < 3 or not a[1].isdigit() or not a[2].isdigit():
            await say("Use: /newcoupon CODE DIN USES")
            return
        await db.ex("INSERT OR REPLACE INTO coupons(code,days,max_uses,used) VALUES(?,?,?,0)", a[0].upper(), int(a[1]), int(a[2]))
        await say(f"✅ Coupon {a[0].upper()}: {a[1]} din, {a[2]} uses")
    elif cmd == "delcoupon":
        await db.ex("DELETE FROM coupons WHERE code=?", rest.strip().upper())
        await say("🗑 Done.")
    elif cmd == "coupons":
        rows = await db.q("SELECT * FROM coupons")
        await say("\n".join(f"{r['code']} | {r['days']}d | {r['used']}/{r['max_uses']}" for r in rows) if rows else "Koi coupon nahi.")
    elif cmd == "payments":
        await scr(ctx, await payments_text(), oback())
    elif cmd == "maintenance":
        if a and a[0].lower() in ("on", "off"):
            await db.kv_set("maintenance", "1" if a[0].lower() == "on" else "0")
            await say(f"🛠 Naye bots: {'BAND' if a[0].lower() == 'on' else 'chalu'}")
        else:
            await say("Use: /maintenance on|off")
    elif cmd == "backup":
        await backup(bot, chat)
        await say("📥 Backup bhej diya (tokens include nahi hain).")
    elif cmd in ("takedown", "revoke"):
        if not a or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", a[0]):
            await say(f"Use: /{cmd} CODE")
            return
        if cmd == "takedown":
            n, l = await store.takedown(a[0])
            await say(f"🗑 Takedown done: {n} file(s) hata di gayi." if l else "Link nahi mila.")
        else:
            n = await db.run("UPDATE links SET revoked=1 WHERE code=?", a[0])
            await say("🚫 Link band kar diya." if n else "Link nahi mila.")
