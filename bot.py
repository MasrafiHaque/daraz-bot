"""
Daraz Affiliate Bot — সম্পূর্ণ Button-based
/start ছাড়া আর কিছু টাইপ করতে হবে না।
"""

import os, json, logging, asyncio
from datetime import datetime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# ── Data directory (Railway Volume: /data) ───────────────────────
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

# ConversationHandler state
WAITING_INTERVAL = 1

_posting_lock = asyncio.Lock()
scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))

# ═══════════════════════════ helpers ════════════════════════════

def tz(): return pytz.timezone(TIMEZONE)
def now_dt(): return datetime.now(tz())
def now_str(): return now_dt().strftime("%Y-%m-%d %H:%M:%S")

def parse_dt(s):
    if not s: return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return tz().localize(dt)
    except Exception:
        return None

def load_posts():
    if os.path.exists(POSTS_FILE):
        with open(POSTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_posts(posts):
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

def load_cfg():
    d = {"interval_hours": 1, "active": True,
         "post_index": 0, "last_posted_at": None, "bot_started_at": None}
    if os.path.exists(CFG_FILE):
        with open(CFG_FILE, "r") as f:
            c = json.load(f)
            for k, v in d.items(): c.setdefault(k, v)
            return c
    return d

def save_cfg(cfg):
    with open(CFG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def is_admin(update: Update):
    return update.effective_user.id == ADMIN_ID

def entities_to_list(entities):
    if not entities: return []
    out = []
    for e in entities:
        entry = {
            "type":   e.type.value if hasattr(e.type, "value") else str(e.type),
            "offset": e.offset, "length": e.length,
        }
        if e.url: entry["url"] = e.url
        out.append(entry)
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

# ── index management ─────────────────────────────────────────────

def current_next_id(posts, cfg):
    if not posts: return None
    return posts[cfg["post_index"] % len(posts)]["id"]

def restore_index(posts, cfg, target_id):
    if not posts: cfg["post_index"] = 0; return
    for i, p in enumerate(posts):
        if p["id"] == target_id: cfg["post_index"] = i; return
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
    if not posts:                   cfg["post_index"] = 0
    elif nid == del_id:             cfg["post_index"] = cfg.get("post_index",0) % len(posts)
    else:                           restore_index(posts, cfg, nid)
    save_cfg(cfg); return posts

# ── timing helpers ────────────────────────────────────────────────

def secs_since_last(cfg):
    ref = parse_dt(cfg.get("last_posted_at")) or parse_dt(cfg.get("bot_started_at"))
    if ref is None: return 0
    return (now_dt() - ref).total_seconds()

def next_post_in(cfg):
    rem = max(0, cfg["interval_hours"] * 3600 - secs_since_last(cfg))
    h, m = divmod(int(rem), 3600)
    m //= 60
    if h > 0: return f"{h}h {m}m পর"
    return f"{m}m পর"

# ═══════════════════════════ keyboards ══════════════════════════

def kb_main(cfg, posts):
    status = "✅ চালু" if cfg["active"] else "⏸️ বন্ধ"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 Schedule ({len(posts)} post)", callback_data="menu_schedule")],
        [InlineKeyboardButton("📤 এখনই Post করুন",  callback_data="action_sendnow"),
         InlineKeyboardButton("📊 Status",            callback_data="menu_status")],
        [InlineKeyboardButton(f"{'⏸️ Pause' if cfg['active'] else '▶️ Resume'}",
                              callback_data="action_toggle"),
         InlineKeyboardButton("⏱️ Interval পরিবর্তন", callback_data="menu_interval")],
        [InlineKeyboardButton("🗑️ Post মুছুন",        callback_data="menu_delete"),
         InlineKeyboardButton("🗑️ সব মুছুন",          callback_data="menu_clearall")],
        [InlineKeyboardButton("🔄 Refresh",            callback_data="menu_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ মেনুতে ফিরুন", callback_data="menu_main")
    ]])

def kb_confirm(yes_cb, no_cb="menu_main"):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ হ্যাঁ",  callback_data=yes_cb),
        InlineKeyboardButton("❌ না",     callback_data=no_cb),
    ]])

def kb_post_confirm():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Schedule করুন", callback_data="post_save"),
        InlineKeyboardButton("❌ বাদ দিন",       callback_data="post_discard"),
    ]])

# ═══════════════════════════ send logic ═════════════════════════

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
                    await app.bot.send_photo(chat_id=CHANNEL_ID, photo=post["photo_id"],
                                             caption=cap, caption_entities=ents)
                else:
                    await app.bot.send_message(chat_id=CHANNEL_ID, text=cap,
                                               entities=ents, disable_web_page_preview=False)
                sent = True
                logger.info(f"✅ Post #{post['id']} sent ({idx+1}/{total})")
                break
            except Exception as e:
                logger.error(f"❌ Attempt {attempt}/{MAX_RETRY}: {e}")
                if attempt < MAX_RETRY: await asyncio.sleep(RETRY_WAIT)

        cfg["post_index"]    = (idx + 1) % total
        cfg["last_posted_at"] = now_str()
        save_cfg(cfg)
        if not sent:
            logger.error(f"⚠️ Post #{post['id']} skipped after {MAX_RETRY} attempts.")
        return sent

async def watchdog(app):
    cfg   = load_cfg()
    posts = load_posts()
    if not cfg.get("active") or not posts: return
    if secs_since_last(cfg) >= cfg["interval_hours"] * 3600:
        logger.info("⏰ Watchdog triggered — sending post...")
        await send_next_post(app)

# ═══════════════════════════ main menu ══════════════════════════

def main_menu_text(cfg, posts):
    last = cfg.get("last_posted_at") or "কখনো হয়নি"
    return (
        "🤖 <b>Daraz Affiliate Bot</b>\n\n"
        f"📦 মোট Post: <b>{len(posts)}</b>\n"
        f"⏱️ Interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
        f"🔄 Status: <b>{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}</b>\n"
        f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>\n"
        f"📅 শেষ post: <b>{last}</b>\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "নিচের বাটন চাপুন <b>অথবা</b>\n"
        "ছবি/text পাঠান post যোগ করতে।"
    )

async def show_main_menu(update: Update, cfg=None, posts=None, edit=False):
    if cfg   is None: cfg   = load_cfg()
    if posts is None: posts = load_posts()
    text = main_menu_text(cfg, posts)
    kb   = kb_main(cfg, posts)

    if edit:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

# ═══════════════════════════ handlers ═══════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔ Access denied.")
    await show_main_menu(update)

# ── message (new post) ────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    msg  = update.message
    post = extract_post(msg)
    if not post: return

    # Forward করা → সরাসরি add
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
        prev   = (post["caption"] or "")[:50].replace("\n"," ")
        await msg.reply_text(
            f"✅ {icon} Post <b>#{new_id}</b> added!\n"
            f"📦 মোট: <b>{len(posts)}</b>",
            parse_mode="HTML",
            reply_markup=kb_back()
        )
        return

    # Normal → preview দেখিয়ে confirm নাও
    ctx.user_data["pending_post"] = post
    if post["photo_id"]:
        await msg.reply_photo(
            photo=post["photo_id"],
            caption="📋 <b>Preview</b> — এভাবেই channel এ যাবে\n\nSchedule করবেন?",
            parse_mode="HTML",
            reply_markup=kb_post_confirm()
        )
    else:
        await msg.reply_text(
            "📋 <b>Preview</b> — এভাবেই channel এ যাবে\n\nSchedule করবেন?",
            parse_mode="HTML",
            reply_markup=kb_post_confirm()
        )

# ── interval text input ───────────────────────────────────────────

async def handle_interval_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    text = update.message.text.strip()
    try:
        hours = float(text)
        if hours < 0.1: raise ValueError
    except Exception:
        await update.message.reply_text(
            "❌ সঠিক সংখ্যা দিন।\nযেমন: <code>2</code> বা <code>0.5</code>",
            parse_mode="HTML",
            reply_markup=kb_back()
        )
        return ConversationHandler.END

    cfg = load_cfg()
    cfg["interval_hours"] = hours
    save_cfg(cfg)
    await update.message.reply_text(
        f"✅ Interval <b>{hours} ঘন্টা</b> সেট হয়েছে!\n"
        f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>",
        parse_mode="HTML",
        reply_markup=kb_back()
    )
    return ConversationHandler.END

# ── callback router ───────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    # ── Main menu ────────────────────────────────────────────────
    if data == "menu_main":
        await show_main_menu(update, edit=True)
        return

    # ── Schedule list ────────────────────────────────────────────
    if data == "menu_schedule":
        posts = load_posts()
        cfg   = load_cfg()
        if not posts:
            await q.edit_message_text(
                "📭 কোনো post নেই।\nছবি বা text পাঠিয়ে post যোগ করুন।",
                reply_markup=kb_back()
            )
            return

        current = cfg["post_index"] % len(posts)
        lines   = [
            f"📋 <b>Scheduled Posts ({len(posts)})</b>",
            f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>\n",
        ]
        for i, p in enumerate(posts):
            marker  = "▶️" if i == current else "  "
            icon    = "🖼️" if p.get("photo_id") else "📝"
            preview = (p.get("caption") or "")[:45].replace("\n"," ")
            suffix  = "…" if len(p.get("caption","")) > 45 else ""
            lines.append(f"{marker}{icon} <b>#{p['id']}</b> {preview}{suffix}")
        lines.append("\n🔁 শেষ post এর পর আবার #1 থেকে শুরু।")

        await q.edit_message_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb_back()
        )
        return

    # ── Status ───────────────────────────────────────────────────
    if data == "menu_status":
        cfg   = load_cfg()
        posts = load_posts()
        idx   = cfg["post_index"] % len(posts) if posts else 0
        nxt   = posts[idx]["id"] if posts else "-"
        last  = cfg.get("last_posted_at") or "কখনো হয়নি"
        await q.edit_message_text(
            "📊 <b>Bot Status</b>\n\n"
            f"📦 মোট Post: <b>{len(posts)}</b>\n"
            f"▶️ পরের Post: <b>#{nxt}</b>\n"
            f"⏱️ Interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
            f"🔄 Status: <b>{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}</b>\n"
            f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>\n"
            f"📅 শেষ post: <b>{last}</b>\n"
            f"📢 Channel: <code>{CHANNEL_ID}</code>",
            parse_mode="HTML", reply_markup=kb_back()
        )
        return

    # ── Pause / Resume toggle ────────────────────────────────────
    if data == "action_toggle":
        cfg = load_cfg()
        cfg["active"] = not cfg["active"]
        save_cfg(cfg)
        state = "▶️ Bot চালু করা হয়েছে!" if cfg["active"] else "⏸️ Bot pause করা হয়েছে।"
        await q.edit_message_text(
            f"{state}\n🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>",
            parse_mode="HTML", reply_markup=kb_back()
        )
        return

    # ── Send now ─────────────────────────────────────────────────
    if data == "action_sendnow":
        posts = load_posts()
        if not posts:
            await q.edit_message_text("📭 কোনো post নেই।", reply_markup=kb_back())
            return
        await q.edit_message_text("📤 পাঠানো হচ্ছে...")
        ok  = await send_next_post(ctx.application, force=True)
        cfg = load_cfg()
        await q.edit_message_text(
            f"{'✅ Post channel এ গেছে!' if ok else '❌ পাঠানো যায়নি! Log দেখুন।'}\n"
            f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>",
            parse_mode="HTML", reply_markup=kb_back()
        )
        return

    # ── Interval menu ────────────────────────────────────────────
    if data == "menu_interval":
        cfg      = load_cfg()
        intervals = [0.5, 1, 2, 3, 4, 6, 8, 12, 24]
        rows = []
        row  = []
        for h in intervals:
            label = f"{'✅ ' if cfg['interval_hours']==h else ''}{h}h"
            row.append(InlineKeyboardButton(label, callback_data=f"set_interval_{h}"))
            if len(row) == 3:
                rows.append(row); row = []
        if row: rows.append(row)
        rows.append([InlineKeyboardButton("✏️ নিজে লিখুন", callback_data="interval_custom")])
        rows.append([InlineKeyboardButton("◀️ মেনুতে ফিরুন", callback_data="menu_main")])
        await q.edit_message_text(
            f"⏱️ <b>Interval সেট করুন</b>\n\nএখন: <b>{cfg['interval_hours']} ঘন্টা</b>\n\nকত ঘন্টা পর পর post যাবে?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if data.startswith("set_interval_"):
        hours = float(data.replace("set_interval_", ""))
        cfg   = load_cfg()
        cfg["interval_hours"] = hours
        save_cfg(cfg)
        await q.edit_message_text(
            f"✅ Interval <b>{hours} ঘন্টা</b> সেট হয়েছে!\n"
            f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>",
            parse_mode="HTML", reply_markup=kb_back()
        )
        return

    if data == "interval_custom":
        await q.edit_message_text(
            "✏️ <b>Interval লিখুন</b>\n\nযেমন: <code>2</code> বা <code>0.5</code>\n\n"
            "এখন সংখ্যাটি টাইপ করুন:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ বাতিল", callback_data="menu_interval")
            ]])
        )
        ctx.user_data["awaiting_interval"] = True
        return

    # ── Delete menu ──────────────────────────────────────────────
    if data == "menu_delete":
        posts = load_posts()
        if not posts:
            await q.edit_message_text("📭 কোনো post নেই।", reply_markup=kb_back())
            return
        rows = []
        for p in posts:
            icon    = "🖼️" if p.get("photo_id") else "📝"
            preview = (p.get("caption") or "")[:28].replace("\n"," ")
            rows.append([InlineKeyboardButton(
                f"🗑️ {icon} #{p['id']} — {preview}…",
                callback_data=f"del_{p['id']}"
            )])
        rows.append([InlineKeyboardButton("◀️ মেনুতে ফিরুন", callback_data="menu_main")])
        await q.edit_message_text(
            "🗑️ <b>কোন post মুছবেন?</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if data.startswith("del_"):
        del_id = int(data.replace("del_",""))
        posts  = load_posts(); cfg = load_cfg()
        posts  = del_post(del_id, posts, cfg)
        await q.edit_message_text(
            f"🗑️ Post <b>#{del_id}</b> মুছে ফেলা হয়েছে।\n"
            f"📦 বাকি: <b>{len(posts)}</b> post",
            parse_mode="HTML", reply_markup=kb_back()
        )
        return

    # ── Clear all ────────────────────────────────────────────────
    if data == "menu_clearall":
        posts = load_posts()
        if not posts:
            await q.edit_message_text("📭 কোনো post নেই।", reply_markup=kb_back())
            return
        await q.edit_message_text(
            f"⚠️ <b>সত্যিই সব {len(posts)}টি post মুছবেন?</b>\n"
            "<i>এই কাজ undo করা যাবে না।</i>",
            parse_mode="HTML",
            reply_markup=kb_confirm("clearall_yes")
        )
        return

    if data == "clearall_yes":
        save_posts([])
        cfg = load_cfg()
        cfg["post_index"] = 0; cfg["last_posted_at"] = None
        save_cfg(cfg)
        await q.edit_message_text("🗑️ সব post মুছে ফেলা হয়েছে।", reply_markup=kb_back())
        return

    # ── Post confirm ─────────────────────────────────────────────
    if data == "post_save":
        pending = ctx.user_data.pop("pending_post", None)
        if not pending:
            await q.edit_message_text("❌ সমস্যা হয়েছে, আবার পাঠান।")
            return
        posts  = load_posts(); cfg = load_cfg()
        new_id = add_post(pending, posts, cfg)
        posts  = load_posts(); cfg = load_cfg()
        msg = (
            f"✅ Post <b>#{new_id}</b> schedule এ যোগ হয়েছে!\n"
            f"📦 মোট: <b>{len(posts)}</b>\n"
            f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>"
        )
        try:    await q.edit_message_caption(caption=msg, parse_mode="HTML", reply_markup=kb_back())
        except: await q.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_back())
        return

    if data == "post_discard":
        ctx.user_data.pop("pending_post", None)
        try:    await q.edit_message_caption(caption="❌ বাতিল।", reply_markup=kb_back())
        except: await q.edit_message_text("❌ বাতিল।", reply_markup=kb_back())
        return

# ── text input for custom interval ───────────────────────────────

async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    msg = update.message

    # Custom interval input
    if ctx.user_data.pop("awaiting_interval", False):
        text = msg.text.strip()
        try:
            hours = float(text)
            if hours < 0.1: raise ValueError
            cfg = load_cfg()
            cfg["interval_hours"] = hours
            save_cfg(cfg)
            await msg.reply_text(
                f"✅ Interval <b>{hours} ঘন্টা</b> সেট হয়েছে!\n"
                f"🕐 পরবর্তী post: <b>{next_post_in(cfg)}</b>",
                parse_mode="HTML", reply_markup=kb_back()
            )
        except Exception:
            await msg.reply_text(
                "❌ সঠিক সংখ্যা দিন। যেমন: <code>2</code>",
                parse_mode="HTML", reply_markup=kb_back()
            )
        return

    # Otherwise treat as new post
    await handle_message(update, ctx)

# ═══════════════════════════ watchdog ═══════════════════════════

# ═══════════════════════════ main ═══════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_start))   # /menu ও কাজ করবে
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
        handle_text_input
    ))

    cfg = load_cfg()
    cfg["bot_started_at"] = now_str()
    save_cfg(cfg)

    scheduler.add_job(watchdog, "interval", seconds=60, args=[app], id="watchdog")
    scheduler.start()

    logger.info(f"🚀 Bot started | DATA_DIR={DATA_DIR} | interval={cfg['interval_hours']}h")
    logger.info(f"   last_posted_at = {cfg.get('last_posted_at') or 'never'}")

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
