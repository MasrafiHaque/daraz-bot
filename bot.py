"""
Daraz Affiliate Bot v4 — Button-based, Posting Window, Auto-alerts
"""
import os, json, logging, asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# ── Storage ──────────────────────────────────────────────────────
DATA_DIR   = os.environ.get("DATA_DIR", ".")
POSTS_FILE = os.path.join(DATA_DIR, "posts.json")
CFG_FILE   = os.path.join(DATA_DIR, "config.json")
LOG_FILE   = os.path.join(DATA_DIR, "bot.log")
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "YOUR_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@your_channel")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
TIMEZONE   = "Asia/Dhaka"
MAX_RETRY  = 3
RETRY_WAIT = 15

_posting_lock   = asyncio.Lock()
_alerted_empty  = False
_alerted_overdue= False
scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))

# ═══════════════════════ helpers ════════════════════════════════

def tz():       return pytz.timezone(TIMEZONE)
def now_dt():   return datetime.now(tz())
def now_str():  return now_dt().strftime("%Y-%m-%d %H:%M:%S")

def parse_dt(s):
    if not s: return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return tz().localize(dt)
    except Exception:
        return None

def load_posts():
    try:
        with open(POSTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_posts(posts):
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

def load_cfg():
    defaults = {
        "interval_hours":  1,
        "active":          True,
        "post_index":      0,
        "last_posted_at":  None,
        "bot_started_at":  None,
        # Posting window: শুধু এই সময়ের মধ্যে post যাবে
        "window_start":    22,   # রাত ১০টা
        "window_end":      8,    # সকাল ৮টা
        "window_enabled":  True,
    }
    if os.path.exists(CFG_FILE):
        with open(CFG_FILE, "r") as f:
            cfg = json.load(f)
            for k, v in defaults.items():
                cfg.setdefault(k, v)
            return cfg
    return defaults

def save_cfg(cfg):
    with open(CFG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def is_admin(update: Update):
    return update.effective_user.id == ADMIN_ID

# ── Entities ─────────────────────────────────────────────────────

def entities_to_list(ents):
    if not ents: return []
    out = []
    for e in ents:
        d = {"type": e.type.value if hasattr(e.type,"value") else str(e.type),
             "offset": e.offset, "length": e.length}
        if e.url: d["url"] = e.url
        out.append(d)
    return out

def list_to_entities(el):
    if not el: return None
    return [MessageEntity(type=e["type"], offset=e["offset"],
                          length=e["length"], url=e.get("url") or None) for e in el]

def extract_post(msg):
    ts = now_dt().strftime("%Y-%m-%d %H:%M")
    if msg.photo:
        return {"photo_id": msg.photo[-1].file_id,
                "caption":  msg.caption or "",
                "entities": entities_to_list(msg.caption_entities),
                "created_at": ts}
    elif msg.text and not msg.text.startswith("/"):
        return {"photo_id": None, "caption": msg.text,
                "entities": entities_to_list(msg.entities),
                "created_at": ts}
    return None

# ── Index management ──────────────────────────────────────────────

def current_next_id(posts, cfg):
    if not posts: return None
    return posts[cfg["post_index"] % len(posts)]["id"]

def restore_index(posts, cfg, target_id):
    if not posts: cfg["post_index"] = 0; return
    for i, p in enumerate(posts):
        if p["id"] == target_id:
            cfg["post_index"] = i; return
    cfg["post_index"] = cfg.get("post_index", 0) % len(posts)

def add_post(new_post, posts, cfg):
    nid = current_next_id(posts, cfg)
    new_post["id"] = (max(p["id"] for p in posts) + 1) if posts else 1
    posts.append(new_post); save_posts(posts)
    if nid is not None: restore_index(posts, cfg, nid)
    save_cfg(cfg)
    return new_post["id"]

def del_post(del_id, posts, cfg):
    nid   = current_next_id(posts, cfg)
    posts = [p for p in posts if p["id"] != del_id]
    save_posts(posts)
    if not posts:          cfg["post_index"] = 0
    elif nid == del_id:    cfg["post_index"] = cfg.get("post_index",0) % len(posts)
    else:                  restore_index(posts, cfg, nid)
    save_cfg(cfg); return posts

# ── Timing ────────────────────────────────────────────────────────

def secs_since_last(cfg):
    ref = parse_dt(cfg.get("last_posted_at")) or parse_dt(cfg.get("bot_started_at"))
    if ref is None: return 0
    return (now_dt() - ref).total_seconds()

def next_post_in(cfg):
    rem = max(0, cfg["interval_hours"] * 3600 - secs_since_last(cfg))
    h   = int(rem) // 3600
    m   = (int(rem) % 3600) // 60
    if h > 0: return f"{h}h {m}m পর"
    return f"{m}m পর"

def is_within_window(cfg):
    """
    Posting window check।
    উদাহরণ: start=22, end=8 মানে রাত ১০টা থেকে সকাল ৮টা পর্যন্ত।
    """
    if not cfg.get("window_enabled", True):
        return True   # window বন্ধ → সবসময় post করো
    h     = now_dt().hour
    start = cfg.get("window_start", 22)
    end   = cfg.get("window_end",    8)
    if start > end:   # রাত পেরিয়ে যায় (যেমন 22–8)
        return h >= start or h < end
    else:             # একই দিনে (যেমন 9–18)
        return start <= h < end

def window_str(cfg):
    if not cfg.get("window_enabled", True):
        return "সবসময়"
    s = cfg.get("window_start", 22)
    e = cfg.get("window_end",    8)
    def fmt(h):
        suffix = "AM" if h < 12 else "PM"
        hh = h if h <= 12 else h - 12
        if hh == 0: hh = 12
        return f"{hh}:00 {suffix}"
    return f"{fmt(s)} – {fmt(e)}"

# ═══════════════════════ safe edit helper ═══════════════════════
# 400 Bad Request fix: photo message এ text edit হয় না → caption try করি

async def safe_edit(query, text, **kwargs):
    """photo message হলে edit_message_caption, নইলে edit_message_text।"""
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception as e:
        if "There is no text in the message to edit" in str(e) or "400" in str(e):
            try:
                await query.edit_message_caption(caption=text, **kwargs)
            except Exception:
                await query.message.reply_text(text, **kwargs)
        else:
            await query.message.reply_text(text, **kwargs)

# ═══════════════════════ send logic ═════════════════════════════

async def notify_admin(app, text):
    try:
        await app.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Admin notify failed: {e}")

async def send_next_post(app, force=False):
    if _posting_lock.locked(): return False
    async with _posting_lock:
        cfg   = load_cfg()
        posts = load_posts()
        if not posts: return False
        if not cfg.get("active") and not force: return False

        total = len(posts)
        idx   = cfg["post_index"] % total
        post  = posts[idx]
        cap   = post.get("caption", "")
        ents  = list_to_entities(post.get("entities", []))
        sent  = False

        for attempt in range(1, MAX_RETRY + 1):
            try:
                if post.get("photo_id"):
                    await app.bot.send_photo(
                        chat_id=CHANNEL_ID, photo=post["photo_id"],
                        caption=cap, caption_entities=ents)
                else:
                    await app.bot.send_message(
                        chat_id=CHANNEL_ID, text=cap,
                        entities=ents, disable_web_page_preview=False)
                sent = True
                logger.info(f"✅ Post #{post['id']} sent ({idx+1}/{total})")
                break

            except Exception as e:
                err = str(e)
                logger.error(f"❌ Attempt {attempt}/{MAX_RETRY}: {err}")

                # Bot channel এ নেই → সাথে সাথে থামাও, admin কে জানাও
                if "Forbidden" in err or "not a member" in err or "kicked" in err:
                    cfg["active"] = False
                    save_cfg(cfg)
                    await notify_admin(app,
                        "🚨 <b>Critical Error — Posting বন্ধ!</b>\n\n"
                        f"❌ Bot channel <code>{CHANNEL_ID}</code> এ post করতে পারছে না।\n\n"
                        "কারণ: <b>Bot channel এর admin না।</b>\n\n"
                        "👉 করণীয়:\n"
                        "1. Channel এ যান → Administrators\n"
                        "2. Bot কে admin করুন\n"
                        "3. 'Post Messages' permission দিন\n"
                        "4. তারপর Resume বাটন চাপুন।"
                    )
                    return False

                if attempt < MAX_RETRY:
                    await asyncio.sleep(RETRY_WAIT)

        cfg["post_index"]     = (idx + 1) % total
        cfg["last_posted_at"] = now_str()
        save_cfg(cfg)
        if not sent:
            logger.error(f"⚠️ Post #{post['id']} skipped after {MAX_RETRY} attempts.")
        return sent

# ═══════════════════════ watchdog ═══════════════════════════════

async def watchdog(app):
    global _alerted_empty, _alerted_overdue

    cfg   = load_cfg()
    posts = load_posts()

    # Posts নেই → একবার alert
    if not posts:
        if not _alerted_empty:
            _alerted_empty = True
            await notify_admin(app,
                "⚠️ <b>Bot Alert</b>\n\n"
                "📭 কোনো post নেই! Posting বন্ধ আছে।\n\n"
                "কারণ হতে পারে:\n"
                "• Railway restart — Volume নেই\n"
                "• সব post মুছে ফেলা হয়েছে\n\n"
                "👉 /start দিয়ে post যোগ করুন।")
        return
    _alerted_empty = False

    if not cfg.get("active"):
        return

    # Window check — এই সময় post করার কথা না
    if not is_within_window(cfg):
        return

    elapsed      = secs_since_last(cfg)
    interval_sec = cfg["interval_hours"] * 3600

    # অনেক দেরি → একবার overdue alert
    if elapsed >= interval_sec * 3 and not _alerted_overdue:
        _alerted_overdue = True
        await notify_admin(app,
            "⚠️ <b>Post Overdue!</b>\n\n"
            f"⏰ শেষ post {elapsed/3600:.1f} ঘন্টা আগে হয়েছিল!\n"
            f"Interval: {cfg['interval_hours']} ঘন্টা\n\n"
            "Bot এখনই পাঠানোর চেষ্টা করছে...")

    if elapsed >= interval_sec:
        _alerted_overdue = False
        logger.info(f"⏰ Watchdog: {elapsed/3600:.1f}h elapsed — posting...")
        await send_next_post(app)

# ═══════════════════════ keyboards ══════════════════════════════

def kb_main(cfg, posts):
    active_lbl = "⏸️ Pause" if cfg["active"] else "▶️ Resume"
    win_lbl    = f"⏰ Window: {window_str(cfg)}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 Schedule  ({len(posts)} post)", callback_data="menu_schedule")],
        [InlineKeyboardButton("📤 এখনই Post করুন", callback_data="action_sendnow"),
         InlineKeyboardButton("📊 Status",          callback_data="menu_status")],
        [InlineKeyboardButton(active_lbl,           callback_data="action_toggle"),
         InlineKeyboardButton("⏱️ Interval",         callback_data="menu_interval")],
        [InlineKeyboardButton(win_lbl,              callback_data="menu_window")],
        [InlineKeyboardButton("🗑️ Post মুছুন",      callback_data="menu_delete"),
         InlineKeyboardButton("🗑️ সব মুছুন",        callback_data="menu_clearall")],
        [InlineKeyboardButton("🔄 Refresh",          callback_data="menu_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ মেনুতে ফিরুন", callback_data="menu_main")]])

def kb_confirm(yes_cb, no_cb="menu_main"):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ হ্যাঁ", callback_data=yes_cb),
        InlineKeyboardButton("❌ না",    callback_data=no_cb),
    ]])

def kb_post_confirm():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Schedule করুন", callback_data="post_save"),
        InlineKeyboardButton("❌ বাদ দিন",       callback_data="post_discard"),
    ]])

# ═══════════════════════ main menu ══════════════════════════════

def main_menu_text(cfg, posts):
    last    = cfg.get("last_posted_at") or "কখনো হয়নি"
    win_txt = window_str(cfg)
    now_ok  = "✅ এখন active" if is_within_window(cfg) else "⏳ window এর বাইরে"
    return (
        "🤖 <b>Daraz Affiliate Bot</b>\n\n"
        f"📦 মোট Post: <b>{len(posts)}</b>\n"
        f"⏱️ Interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
        f"🔄 Status: <b>{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}</b>\n"
        f"⏰ Posting Window: <b>{win_txt}</b>  {now_ok}\n"
        f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>\n"
        f"📅 শেষ post: <b>{last}</b>\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "বাটন চাপুন <b>অথবা</b> ছবি/text পাঠান।"
    )

async def show_main_menu(update: Update, edit=False):
    cfg   = load_cfg()
    posts = load_posts()
    text  = main_menu_text(cfg, posts)
    kb    = kb_main(cfg, posts)
    if edit:
        await safe_edit(update.callback_query, text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

# ═══════════════════════ command handlers ═══════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔ Access denied.")
    await show_main_menu(update)

# ═══════════════════════ message handler ════════════════════════

async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    msg = update.message

    # Custom interval input
    if ctx.user_data.pop("awaiting_interval", False):
        try:
            hours = float(msg.text.strip())
            if hours < 0.1: raise ValueError
            cfg = load_cfg()
            cfg["interval_hours"] = hours
            save_cfg(cfg)
            await msg.reply_text(
                f"✅ Interval <b>{hours} ঘন্টা</b> সেট!\n"
                f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>",
                parse_mode="HTML", reply_markup=kb_back())
        except Exception:
            await msg.reply_text("❌ সঠিক সংখ্যা দিন। যেমন: <code>2</code>",
                                 parse_mode="HTML", reply_markup=kb_back())
        return

    # Window hour input
    if ctx.user_data.get("awaiting_window"):
        field = ctx.user_data.pop("awaiting_window")
        try:
            h = int(msg.text.strip())
            if not 0 <= h <= 23: raise ValueError
            cfg = load_cfg()
            cfg[field] = h
            save_cfg(cfg)
            label = "শুরু" if field == "window_start" else "শেষ"
            await msg.reply_text(
                f"✅ Posting window {label} সময় <b>{h}:00</b> সেট!\n"
                f"Window: <b>{window_str(cfg)}</b>",
                parse_mode="HTML", reply_markup=kb_back())
        except Exception:
            await msg.reply_text("❌ 0–23 এর মধ্যে সংখ্যা দিন।",
                                 reply_markup=kb_back())
        return

    # New post
    post = extract_post(msg)
    if not post: return

    is_fwd = bool(
        getattr(msg, "forward_origin",      None) or
        getattr(msg, "forward_from",        None) or
        getattr(msg, "forward_from_chat",   None) or
        getattr(msg, "forward_sender_name", None) or
        getattr(msg, "forward_date",        None)
    )

    if is_fwd:
        posts  = load_posts(); cfg = load_cfg()
        new_id = add_post(post, posts, cfg)
        posts  = load_posts()
        icon   = "🖼️" if post["photo_id"] else "📝"
        await msg.reply_text(
            f"✅ {icon} Post <b>#{new_id}</b> added!\n📦 মোট: <b>{len(posts)}</b>",
            parse_mode="HTML", reply_markup=kb_back())
        return

    ctx.user_data["pending_post"] = post
    if post["photo_id"]:
        await msg.reply_photo(
            photo=post["photo_id"],
            caption="📋 <b>Preview</b> — এভাবেই channel এ যাবে\n\nSchedule করবেন?",
            parse_mode="HTML", reply_markup=kb_post_confirm())
    else:
        await msg.reply_text(
            "📋 <b>Preview</b> — এভাবেই channel এ যাবে\n\nSchedule করবেন?",
            parse_mode="HTML", reply_markup=kb_post_confirm())

# ═══════════════════════ callback router ════════════════════════

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    # Main menu
    if data == "menu_main":
        await show_main_menu(update, edit=True)
        return

    # Schedule list
    if data == "menu_schedule":
        posts = load_posts(); cfg = load_cfg()
        if not posts:
            await safe_edit(q, "📭 কোনো post নেই।\nছবি বা text পাঠিয়ে post যোগ করুন।",
                            reply_markup=kb_back())
            return
        cur   = cfg["post_index"] % len(posts)
        lines = [f"📋 <b>Scheduled Posts ({len(posts)})</b>",
                 f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>\n"]
        for i, p in enumerate(posts):
            marker  = "▶️" if i == cur else "  "
            icon    = "🖼️" if p.get("photo_id") else "📝"
            preview = (p.get("caption") or "")[:45].replace("\n"," ")
            suffix  = "…" if len(p.get("caption","")) > 45 else ""
            lines.append(f"{marker}{icon} <b>#{p['id']}</b> {preview}{suffix}")
        lines.append("\n🔁 শেষ post এর পর আবার #1 থেকে শুরু।")
        await safe_edit(q, "\n".join(lines), parse_mode="HTML", reply_markup=kb_back())
        return

    # Status
    if data == "menu_status":
        cfg   = load_cfg(); posts = load_posts()
        idx   = cfg["post_index"] % len(posts) if posts else 0
        nxt   = posts[idx]["id"] if posts else "-"
        last  = cfg.get("last_posted_at") or "কখনো হয়নি"
        now_ok = "✅ এখন active" if is_within_window(cfg) else "⏳ window এর বাইরে"
        await safe_edit(q,
            "📊 <b>Bot Status</b>\n\n"
            f"📦 মোট Post: <b>{len(posts)}</b>\n"
            f"▶️ পরের Post: <b>#{nxt}</b>\n"
            f"⏱️ Interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
            f"🔄 Status: <b>{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}</b>\n"
            f"⏰ Window: <b>{window_str(cfg)}</b>  {now_ok}\n"
            f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>\n"
            f"📅 শেষ post: <b>{last}</b>\n"
            f"📢 Channel: <code>{CHANNEL_ID}</code>",
            parse_mode="HTML", reply_markup=kb_back())
        return

    # Pause/Resume
    if data == "action_toggle":
        cfg = load_cfg()
        cfg["active"] = not cfg["active"]
        save_cfg(cfg)
        state = "▶️ Bot চালু!" if cfg["active"] else "⏸️ Bot pause।"
        await safe_edit(q,
            f"{state}\n🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>",
            parse_mode="HTML", reply_markup=kb_back())
        return

    # Send now
    if data == "action_sendnow":
        posts = load_posts()
        if not posts:
            await safe_edit(q, "📭 কোনো post নেই।", reply_markup=kb_back())
            return
        await safe_edit(q, "📤 পাঠানো হচ্ছে...")
        ok  = await send_next_post(ctx.application, force=True)
        cfg = load_cfg()
        await safe_edit(q,
            f"{'✅ Post গেছে!' if ok else '❌ পাঠানো যায়নি! Log দেখুন।'}\n"
            f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>",
            parse_mode="HTML", reply_markup=kb_back())
        return

    # Interval menu
    if data == "menu_interval":
        cfg       = load_cfg()
        intervals = [0.5, 1, 2, 3, 4, 6, 8, 12, 24]
        rows, row = [], []
        for h in intervals:
            chk = "✅ " if cfg["interval_hours"] == h else ""
            row.append(InlineKeyboardButton(f"{chk}{h}h", callback_data=f"set_iv_{h}"))
            if len(row) == 3: rows.append(row); row = []
        if row: rows.append(row)
        rows.append([InlineKeyboardButton("✏️ নিজে লিখুন", callback_data="iv_custom")])
        rows.append([InlineKeyboardButton("◀️ মেনুতে ফিরুন", callback_data="menu_main")])
        await safe_edit(q,
            f"⏱️ <b>Interval সেট করুন</b>\n\nএখন: <b>{cfg['interval_hours']} ঘন্টা</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("set_iv_"):
        hours = float(data.replace("set_iv_",""))
        cfg   = load_cfg()
        cfg["interval_hours"] = hours; save_cfg(cfg)
        await safe_edit(q,
            f"✅ Interval <b>{hours} ঘন্টা</b> সেট!\n"
            f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>",
            parse_mode="HTML", reply_markup=kb_back())
        return

    if data == "iv_custom":
        await safe_edit(q,
            "✏️ <b>Interval লিখুন</b>\nযেমন: <code>2</code> বা <code>0.5</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ বাতিল", callback_data="menu_interval")]]))
        ctx.user_data["awaiting_interval"] = True
        return

    # ── Posting Window menu ─────────────────────────────────────
    if data == "menu_window":
        cfg = load_cfg()
        win_on = cfg.get("window_enabled", True)
        toggle_lbl = "🔴 Window বন্ধ করুন" if win_on else "🟢 Window চালু করুন"

        # Hour buttons for start
        start_rows = []
        r = []
        for h in range(0, 24):
            chk = "✅" if cfg["window_start"] == h else ""
            label = f"{chk}{h:02d}:00"
            r.append(InlineKeyboardButton(label, callback_data=f"win_start_{h}"))
            if len(r) == 4: start_rows.append(r); r = []
        if r: start_rows.append(r)

        end_rows = []
        r = []
        for h in range(0, 24):
            chk = "✅" if cfg["window_end"] == h else ""
            label = f"{chk}{h:02d}:00"
            r.append(InlineKeyboardButton(label, callback_data=f"win_end_{h}"))
            if len(r) == 4: end_rows.append(r); r = []
        if r: end_rows.append(r)

        await safe_edit(q,
            f"⏰ <b>Posting Window</b>\n\n"
            f"এখন: <b>{window_str(cfg)}</b>\n"
            f"Status: <b>{'✅ চালু' if win_on else '⏸️ বন্ধ'}</b>\n\n"
            f"<b>শুরু সময় বেছে নিন:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                start_rows +
                [[InlineKeyboardButton("── শেষ সময় ──", callback_data="noop")]] +
                end_rows +
                [[InlineKeyboardButton(toggle_lbl, callback_data="win_toggle"),
                  InlineKeyboardButton("◀️ মেনু",  callback_data="menu_main")]]
            ))
        return

    if data == "noop":
        return

    if data.startswith("win_start_"):
        h   = int(data.replace("win_start_",""))
        cfg = load_cfg(); cfg["window_start"] = h; save_cfg(cfg)
        await safe_edit(q,
            f"✅ Posting শুরু: <b>{h:02d}:00</b>\n"
            f"Window: <b>{window_str(cfg)}</b>",
            parse_mode="HTML", reply_markup=kb_back())
        return

    if data.startswith("win_end_"):
        h   = int(data.replace("win_end_",""))
        cfg = load_cfg(); cfg["window_end"] = h; save_cfg(cfg)
        await safe_edit(q,
            f"✅ Posting শেষ: <b>{h:02d}:00</b>\n"
            f"Window: <b>{window_str(cfg)}</b>",
            parse_mode="HTML", reply_markup=kb_back())
        return

    if data == "win_toggle":
        cfg = load_cfg()
        cfg["window_enabled"] = not cfg.get("window_enabled", True)
        save_cfg(cfg)
        state = "✅ চালু" if cfg["window_enabled"] else "⏸️ বন্ধ (সবসময় post হবে)"
        await safe_edit(q,
            f"⏰ Posting Window: <b>{state}</b>\n"
            f"Window: <b>{window_str(cfg)}</b>",
            parse_mode="HTML", reply_markup=kb_back())
        return

    # Delete menu
    if data == "menu_delete":
        posts = load_posts()
        if not posts:
            await safe_edit(q, "📭 কোনো post নেই।", reply_markup=kb_back())
            return
        rows = []
        for p in posts:
            icon    = "🖼️" if p.get("photo_id") else "📝"
            preview = (p.get("caption") or "")[:28].replace("\n"," ")
            rows.append([InlineKeyboardButton(
                f"🗑️ {icon} #{p['id']} — {preview}…",
                callback_data=f"del_{p['id']}")])
        rows.append([InlineKeyboardButton("◀️ মেনু", callback_data="menu_main")])
        await safe_edit(q, "🗑️ <b>কোন post মুছবেন?</b>",
                        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("del_"):
        del_id = int(data.replace("del_",""))
        posts  = load_posts(); cfg = load_cfg()
        posts  = del_post(del_id, posts, cfg)
        await safe_edit(q,
            f"🗑️ Post <b>#{del_id}</b> মুছে ফেলা হয়েছে।\n📦 বাকি: <b>{len(posts)}</b>",
            parse_mode="HTML", reply_markup=kb_back())
        return

    # Clear all
    if data == "menu_clearall":
        posts = load_posts()
        if not posts:
            await safe_edit(q, "📭 কোনো post নেই।", reply_markup=kb_back())
            return
        await safe_edit(q,
            f"⚠️ <b>সত্যিই সব {len(posts)}টি post মুছবেন?</b>\n<i>undo করা যাবে না।</i>",
            parse_mode="HTML", reply_markup=kb_confirm("clearall_yes"))
        return

    if data == "clearall_yes":
        save_posts([])
        cfg = load_cfg()
        cfg["post_index"] = 0; cfg["last_posted_at"] = None; save_cfg(cfg)
        await safe_edit(q, "🗑️ সব post মুছে ফেলা হয়েছে।", reply_markup=kb_back())
        return

    # Post confirm (photo preview থেকে আসে)
    if data == "post_save":
        pending = ctx.user_data.pop("pending_post", None)
        if not pending:
            await safe_edit(q, "❌ সমস্যা হয়েছে, আবার পাঠান।")
            return
        posts  = load_posts(); cfg = load_cfg()
        new_id = add_post(pending, posts, cfg)
        posts  = load_posts(); cfg = load_cfg()
        txt = (f"✅ Post <b>#{new_id}</b> যোগ হয়েছে!\n"
               f"📦 মোট: <b>{len(posts)}</b>\n"
               f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>")
        try:    await q.edit_message_caption(caption=txt, parse_mode="HTML", reply_markup=kb_back())
        except: await safe_edit(q, txt, parse_mode="HTML", reply_markup=kb_back())
        return

    if data == "post_discard":
        ctx.user_data.pop("pending_post", None)
        try:    await q.edit_message_caption(caption="❌ বাতিল।", reply_markup=kb_back())
        except: await safe_edit(q, "❌ বাতিল।", reply_markup=kb_back())
        return

# ═══════════════════════ main ═══════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
        handle_text_input))

    cfg = load_cfg()
    cfg["bot_started_at"] = now_str()
    save_cfg(cfg)

    scheduler.add_job(watchdog, "interval", seconds=60, args=[app], id="watchdog")
    scheduler.start()

    logger.info(f"🚀 Bot v4 | DATA_DIR={DATA_DIR} | interval={cfg['interval_hours']}h | window={window_str(cfg)}")

    async def on_startup(app):
        posts = load_posts()
        cfg2  = load_cfg()
        last  = cfg2.get("last_posted_at") or "কখনো হয়নি"
        now_ok = "✅ এখন active" if is_within_window(cfg2) else "⏳ window এর বাইরে"
        if not posts:
            txt = ("🔴 <b>Bot চালু — কিন্তু কোনো post নেই!</b>\n\n"
                   "Railway restart এর কারণে data মুছে গেছে।\n"
                   "👉 /start দিয়ে post যোগ করুন।")
        else:
            txt = (f"🟢 <b>Bot চালু!</b>\n\n"
                   f"📦 Post: <b>{len(posts)}</b>\n"
                   f"⏱️ Interval: <b>{cfg2['interval_hours']} ঘন্টা</b>\n"
                   f"⏰ Window: <b>{window_str(cfg2)}</b>  {now_ok}\n"
                   f"🕐 পরবর্তী post: <b>{next_post_in(cfg2)}</b>\n"
                   f"📅 শেষ post: <b>{last}</b>")
        await notify_admin(app, txt)

    app.post_init = on_startup
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
