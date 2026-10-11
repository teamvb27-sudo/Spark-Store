"""Stylish cards, buttons aur 'ek hi panel' wala show() helper."""
import html as _html
import re

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb
from telegram import LinkPreviewOptions
from telegram.error import TelegramError

LINE = "━━━━━━━━━━━━━━━━━━"
_NOPREVIEW = LinkPreviewOptions(is_disabled=True)
_last = {}  # (bot_id, chat_id) -> last panel message id


def _mk():
    d = {}
    for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        d[c] = chr(0x1D5D4 + i)
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        d[c] = chr(0x1D5EE + i)
    for i, c in enumerate("0123456789"):
        d[c] = chr(0x1D7EC + i)
    return d


_BOLD = _mk()


def bd(t):
    """Stylish bold letters: Spark -> 𝗦𝗽𝗮𝗿𝗸"""
    return "".join(_BOLD.get(c, c) for c in str(t))


def esc(s):
    return _html.escape("" if s is None else str(s), quote=False)


def clean(s):
    """HTML ko tod sakne wale characters hata do."""
    return re.sub(r"[<>&]", "", "" if s is None else str(s))


def trunc(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def card(icon, title, *lines):
    return f"✦{LINE}✦\n   {icon} {bd(title)} {icon}\n✦{LINE}✦\n\n" + "\n".join(lines)


def B(t, d):
    return Btn(t, callback_data=d)


def fsize(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def bar(used, total, w=10):
    if not total:
        return "▱" * w
    p = min(1.0, used / total)
    f = int(round(w * p))
    return "▰" * f + "▱" * (w - f) + f"  {int(p * 100)}%"


async def _del(bot, chat, mid):
    try:
        await bot.delete_message(chat, mid)
    except TelegramError:
        pass


async def show(bot, chat, text, kb=None, old=None):
    """Purana panel delete karo, naya bhejo (screen par hamesha ek hi panel)."""
    ids = set()
    if old:
        ids.add(old)
    last = _last.get((bot.id, chat))
    if last:
        ids.add(last)
    for i in ids:
        await _del(bot, chat, i)
    s = await bot.send_message(chat, text[:4000], reply_markup=kb, parse_mode="HTML", link_preview_options=_NOPREVIEW)
    _last[(bot.id, chat)] = s.message_id
    return s


async def send(bot, chat, text, kb=None, **kw):
    """Bina panel track kiye normal message."""
    return await bot.send_message(chat, text[:4000], reply_markup=kb, parse_mode="HTML", link_preview_options=_NOPREVIEW, **kw)


async def edit(bot, chat, mid, text, kb=None):
    try:
        await bot.edit_message_text(text[:4000], chat_id=chat, message_id=mid, reply_markup=kb,
                                    parse_mode="HTML", link_preview_options=_NOPREVIEW)
        return True
    except TelegramError:
        return False
