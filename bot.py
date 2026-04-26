import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# ── Logging: console + file দুটোতেই যাবে ──
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@your_channel")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
POSTS_FILE = "posts.json"
CFG_FILE   = "config.json"
TIMEZONE   = "Asia/Dhaka"
MAX_RETRY  = 3          # send fail হলে কতবার retry
RETRY_WAIT = 15         # retry এর মাঝে কত সেকেন্ড অপেক্ষা

# একসাথে দুটো post যেন না যায়
_posting_lock = asyncio.Lock()

# ─────────────────────────── helpers ────────────────────────────

def load_posts():
    if os.path.exists(POSTS_FILE):
        with open(POSTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_posts(posts):
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

def load_cfg():
    default = {
        "interval_hours":  1,
        "active":          True,
        "post_index":      0,
        "last_posted_at":  None,   # সফলভাবে শেষ post কখন গেছে
        "bot_started_at":  None,   # bot কখন চালু হয়েছে
    }
    if os.path.exists(CFG_FILE):
        with open(CFG_FILE, "r") as f:
            cfg = json.load(f)
            for k, v in default.items():
                cfg.setdefault(k, v)
            return cfg
    return default

def save_cfg(cfg):
    with open(CFG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def is_admin(update: Update):
    return update.effective_user.id == ADMIN_ID

def now_str():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

def parse_dt(s):
    """String → timezone-aware datetime। Fail হলে None।"""
    if not s:
        return None
    tz = pytz.timezone(TIMEZONE)
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return tz.localize(dt)
    except Exception:
        return None

# ── entities: bold/italic/link হুবহু থাকবে ──

def entities_to_list(entities):
    if not entities:
        return []
    result = []
    for e in entities:
        entry = {
            "type":   e.type.value if hasattr(e.type, "value") else str(e.type),
            "offset": e.offset,
            "length": e.length,
        }
        if e.url:
            entry["url"] = e.url
        result.append(entry)
    return result

def list_to_entities(entity_list):
    if not entity_list:
        return None
    return [
        MessageEntity(
            type=e["type"], offset=e["offset"],
            length=e["length"], url=e.get("url") or None
        )
        for e in entity_list
    ]

def extract_post_from_msg(msg):
    tz = pytz.timezone(TIMEZONE)
    if msg.photo:
        return {
            "photo_id":   msg.photo[-1].file_id,
            "caption":    msg.caption or "",
            "entities":   entities_to_list(msg.caption_entities),
            "created_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
        }
    elif msg.text and not msg.text.startswith("/"):
        return {
            "photo_id":   None,
            "caption":    msg.text,
            "entities":   entities_to_list(msg.entities),
            "created_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
        }
    return None

# ── Index management ─────────────────────────────────────────────

def get_current_next_id(posts, cfg):
    if not posts:
        return None
    return posts[cfg["post_index"] % len(posts)]["id"]

def restore_index_by_id(posts, cfg, target_id):
    if not posts:
        cfg["post_index"] = 0
        return
    for i, p in enumerate(posts):
        if p["id"] == target_id:
            cfg["post_index"] = i
            return
    cfg["post_index"] = cfg.get("post_index", 0) % len(posts)

def add_post_preserve_index(new_post, posts, cfg):
    next_id = get_current_next_id(posts, cfg)
    new_id  = (max(p["id"] for p in posts) + 1) if posts else 1
    new_post["id"] = new_id
    posts.append(new_post)
    save_posts(posts)
    if next_id is not None:
        restore_index_by_id(posts, cfg, next_id)
    save_cfg(cfg)
    return new_id

def delete_post_preserve_index(del_id, posts, cfg):
    next_id = get_current_next_id(posts, cfg)
    posts   = [p for p in posts if p["id"] != del_id]
    save_posts(posts)
    if not posts:
        cfg["post_index"] = 0
    elif next_id == del_id:
        cfg["post_index"] = cfg.get("post_index", 0) % len(posts)
    else:
        restore_index_by_id(posts, cfg, next_id)
    save_cfg(cfg)
    return posts

# ─────────────────── CORE: post due check ────────────────────────

def seconds_since_last_post(cfg):
    """
    শেষ post কত সেকেন্ড আগে হয়েছিল।
    কোনো post হয়নি হলে → bot চালু হওয়ার পর কত সেকেন্ড।
    """
    tz  = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    ref = parse_dt(cfg.get("last_posted_at")) or \
          parse_dt(cfg.get("bot_started_at"))

    if ref is None:
        return 0

    return (now - ref).total_seconds()

def is_post_due(cfg):
    interval_sec = cfg.get("interval_hours", 1) * 3600
    return seconds_since_last_post(cfg) >= interval_sec

# ─────────────────── CORE: send with retry ───────────────────────

async def send_next_post(app, force=False):
    """
    পরের post পাঠাও।
    - Lock দিয়ে double-send আটকানো হয়
    - MAX_RETRY বার চেষ্টা করবে
    - সফল হলে last_posted_at আপডেট হবে
    """
    if _posting_lock.locked():
        logger.info("Already posting — skipping duplicate trigger.")
        return False

    async with _posting_lock:
        cfg   = load_cfg()
        posts = load_posts()

        if not posts:
            logger.info("No posts to send.")
            return False
        if not cfg.get("active") and not force:
            logger.info("Bot is paused — skipping.")
            return False

        total = len(posts)
        idx   = cfg["post_index"] % total
        post  = posts[idx]

        caption  = post.get("caption", "")
        entities = list_to_entities(post.get("entities", []))

        sent = False
        for attempt in range(1, MAX_RETRY + 1):
            try:
                if post.get("photo_id"):
                    await app.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=post["photo_id"],
                        caption=caption,
                        caption_entities=entities,
                    )
                else:
                    await app.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=caption,
                        entities=entities,
                        disable_web_page_preview=False,
                    )
                sent = True
                logger.info(f"✅ Post #{post['id']} sent ({idx+1}/{total})")
                break

            except Exception as e:
                logger.error(f"❌ Attempt {attempt}/{MAX_RETRY} failed for post #{post['id']}: {e}")
                if attempt < MAX_RETRY:
                    logger.info(f"   Retrying in {RETRY_WAIT}s...")
                    await asyncio.sleep(RETRY_WAIT)

        # সফল হোক বা না হোক — index ও time আপডেট করি
        # (ব্যর্থ post আটকে থেকে সব আটকে দেবে না)
        cfg["post_index"]    = (idx + 1) % total
        cfg["last_posted_at"] = now_str()
        save_cfg(cfg)

        if not sent:
            logger.error(
                f"⚠️ Post #{post['id']} skipped after {MAX_RETRY} failed attempts. "
                f"Check logs/bot.log for details."
            )
        return sent

# ─────────────────── WATCHDOG ─────────────────────────────────────

async def watchdog(app):
    """
    প্রতি ৬০ সেকেন্ডে চলে।
    Bot crash করে restart হলেও — missed post সাথে সাথে পাঠাবে।
    Scheduler fail করলেও — এটা backup হিসেবে কাজ করবে।
    """
    cfg   = load_cfg()
    posts = load_posts()

    if not cfg.get("active") or not posts:
        return

    if is_post_due(cfg):
        elapsed_h = seconds_since_last_post(cfg) / 3600
        logger.info(f"⏰ Watchdog: post due (last was {elapsed_h:.1f}h ago). Sending...")
        await send_next_post(app)

scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))

# ──────────────────────────── commands ──────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔ Access denied.")
    cfg   = load_cfg()
    posts = load_posts()
    await update.message.reply_text(
        "🤖 <b>Daraz Affiliate Bot</b>\n\n"
        f"📦 মোট Post: <b>{len(posts)}</b>\n"
        f"⏱️ Interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
        f"🔄 Status: <b>{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}</b>\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "<b>📤 Post যোগ করতে:</b>\n"
        "• ছবি পাঠান (caption সহ বা ছাড়া)\n"
        "• শুধু text পাঠান\n"
        "• একাধিক post <b>forward</b> করুন\n\n"
        "<b>⚙️ কমান্ড:</b>\n"
        "/schedule — সব post ও পরের post\n"
        "/delete — একটি post মুছুন\n"
        "/clearall — সব post মুছুন\n"
        "/interval 2 — interval সেট করুন\n"
        "/pause — posting বন্ধ করুন\n"
        "/resume — posting চালু করুন\n"
        "/sendnow — এখনই post পাঠান\n"
        "/status — বিস্তারিত অবস্থা",
        parse_mode="HTML",
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    msg  = update.message
    post = extract_post_from_msg(msg)
    if not post:
        return

    is_forwarded = bool(
        getattr(msg, "forward_origin",      None) or
        getattr(msg, "forward_from",        None) or
        getattr(msg, "forward_from_chat",   None) or
        getattr(msg, "forward_sender_name", None) or
        getattr(msg, "forward_date",        None)
    )

    if is_forwarded:
        posts  = load_posts()
        cfg    = load_cfg()
        new_id = add_post_preserve_index(post, posts, cfg)
        posts  = load_posts()
        icon   = "🖼️" if post["photo_id"] else "📝"
        prev   = (post["caption"] or "")[:50].replace("\n", " ")
        await msg.reply_text(
            f"✅ {icon} Post <b>#{new_id}</b> added!\n"
            f"📦 মোট: <b>{len(posts)}</b>\n"
            f"<i>{prev}{'…' if len(post['caption'] or '') > 50 else ''}</i>",
            parse_mode="HTML",
        )
        return

    ctx.bot_data["pending"] = post
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Schedule করুন", callback_data="save"),
        InlineKeyboardButton("❌ বাদ দিন",       callback_data="discard"),
    ]])
    if post["photo_id"]:
        await msg.reply_photo(
            photo=post["photo_id"],
            caption="📋 Preview — এভাবেই channel এ যাবে\n\nSchedule করবেন?",
            reply_markup=kb,
        )
    else:
        await msg.reply_text(
            "📋 Preview — এভাবেই channel এ যাবে\n\nSchedule করবেন?",
            reply_markup=kb,
        )

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "discard":
        ctx.bot_data.pop("pending", None)
        try:
            await query.edit_message_caption(caption="❌ বাতিল।")
        except Exception:
            await query.edit_message_text("❌ বাতিল।")
        return

    if data == "save":
        pending = ctx.bot_data.pop("pending", None)
        if not pending:
            await query.edit_message_text("❌ সমস্যা হয়েছে, আবার পাঠান।")
            return
        posts  = load_posts()
        cfg    = load_cfg()
        new_id = add_post_preserve_index(pending, posts, cfg)
        posts  = load_posts()
        cfg    = load_cfg()
        elapsed = seconds_since_last_post(cfg)
        remaining = max(0, cfg["interval_hours"] * 3600 - elapsed)
        next_in = f"{remaining/3600:.1f} ঘন্টা পর"
        msg = (
            f"✅ Post <b>#{new_id}</b> যোগ হয়েছে!\n"
            f"📦 মোট: <b>{len(posts)}</b>\n"
            f"🕐 পরবর্তী post: <b>{next_in}</b>"
        )
        try:
            await query.edit_message_caption(caption=msg, parse_mode="HTML")
        except Exception:
            await query.edit_message_text(msg, parse_mode="HTML")
        return

    if data.startswith("del|"):
        del_id = int(data.split("|")[1])
        posts  = load_posts()
        cfg    = load_cfg()
        posts  = delete_post_preserve_index(del_id, posts, cfg)
        await query.edit_message_text(
            f"🗑️ Post <b>#{del_id}</b> মুছে ফেলা হয়েছে।\n"
            f"📦 বাকি post: <b>{len(posts)}</b>",
            parse_mode="HTML",
        )
        return

    if data == "clearall_confirm":
        save_posts([])
        cfg = load_cfg()
        cfg["post_index"]     = 0
        cfg["last_posted_at"] = None
        save_cfg(cfg)
        await query.edit_message_text("🗑️ সব post মুছে ফেলা হয়েছে।")
        return

async def schedule_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if ctx.args and ctx.args[0].lower() == "cancel":
        cfg = load_cfg()
        cfg["active"]     = False
        cfg["post_index"] = 0
        save_cfg(cfg)
        await update.message.reply_text(
            "⏸️ Schedule বাতিল। Index #1 এ reset।\n/resume দিয়ে চালু করুন।"
        )
        return

    posts = load_posts()
    if not posts:
        return await update.message.reply_text("📭 কোনো post নেই।")

    cfg     = load_cfg()
    current = cfg["post_index"] % len(posts)
    elapsed = seconds_since_last_post(cfg)
    remaining_sec = max(0, cfg["interval_hours"] * 3600 - elapsed)
    next_in = f"{remaining_sec/3600:.1f} ঘন্টা পর"

    lines = [
        f"📋 <b>Scheduled Posts ({len(posts)})</b>",
        f"🕐 পরবর্তী post: <b>{next_in}</b>\n",
    ]
    for i, p in enumerate(posts):
        marker  = "▶️" if i == current else "   "
        preview = (p.get("caption") or "")[:50].replace("\n", " ")
        suffix  = "…" if len(p.get("caption", "")) > 50 else ""
        icon    = "🖼️" if p.get("photo_id") else "📝"
        lines.append(
            f"{marker}{icon} <b>#{p['id']}</b> {preview}{suffix}\n"
            f"      🕐 {p.get('created_at', '')}"
        )
    lines.append("\n🔁 শেষ post এর পর আবার #1 থেকে শুরু।")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def delete_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    posts = load_posts()
    if not posts:
        return await update.message.reply_text("📭 কোনো post নেই।")
    buttons = []
    for p in posts:
        preview = (p.get("caption") or "")[:30].replace("\n", " ")
        icon    = "🖼️" if p.get("photo_id") else "📝"
        buttons.append([InlineKeyboardButton(
            f"{icon} #{p['id']} — {preview}…",
            callback_data=f"del|{p['id']}",
        )])
    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="discard")])
    await update.message.reply_text(
        "🗑️ <b>কোন post মুছবেন?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def clear_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    posts = load_posts()
    if not posts:
        return await update.message.reply_text("📭 কোনো post নেই।")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ হ্যাঁ, সব মুছুন", callback_data="clearall_confirm"),
        InlineKeyboardButton("❌ না",               callback_data="discard"),
    ]])
    await update.message.reply_text(
        f"⚠️ <b>সত্যিই সব {len(posts)}টি post মুছবেন?</b>\n"
        "<i>এই কাজ undo করা যাবে না।</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )

async def set_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        cfg = load_cfg()
        return await update.message.reply_text(
            f"⏱️ বর্তমান interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
            f"পরিবর্তন করতে: <code>/interval 2</code>",
            parse_mode="HTML",
        )
    try:
        hours = float(ctx.args[0])
        if hours < 0.1:
            raise ValueError
    except Exception:
        return await update.message.reply_text("❌ সঠিক সংখ্যা দিন। যেমন: /interval 2")

    cfg = load_cfg()
    cfg["interval_hours"] = hours
    save_cfg(cfg)

    elapsed = seconds_since_last_post(cfg)
    remaining_sec = max(0, hours * 3600 - elapsed)
    next_in = f"{remaining_sec/3600:.1f} ঘন্টা পর"

    await update.message.reply_text(
        f"✅ Interval <b>{hours} ঘন্টা</b> সেট হয়েছে!\n"
        f"🕐 পরবর্তী post: <b>{next_in}</b>",
        parse_mode="HTML",
    )

async def pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cfg = load_cfg()
    if not cfg.get("active"):
        return await update.message.reply_text("⚠️ Bot আগে থেকেই বন্ধ আছে।")
    cfg["active"] = False
    save_cfg(cfg)
    await update.message.reply_text(
        "⏸️ Bot pause করা হয়েছে।\n/resume দিয়ে চালু করুন।"
    )

async def resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cfg = load_cfg()
    if cfg.get("active"):
        return await update.message.reply_text("⚠️ Bot আগে থেকেই চালু আছে।")
    cfg["active"] = True
    save_cfg(cfg)

    elapsed = seconds_since_last_post(cfg)
    remaining_sec = max(0, cfg["interval_hours"] * 3600 - elapsed)
    next_in = f"{remaining_sec/3600:.1f} ঘন্টা পর"

    await update.message.reply_text(
        f"▶️ Bot চালু!\n"
        f"🕐 পরবর্তী post: <b>{next_in}</b>",
        parse_mode="HTML",
    )

async def send_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not load_posts():
        return await update.message.reply_text("📭 কোনো post নেই।")
    await update.message.reply_text("📤 পাঠানো হচ্ছে...")
    ok = await send_next_post(ctx.application, force=True)

    cfg = load_cfg()
    elapsed = seconds_since_last_post(cfg)
    remaining_sec = max(0, cfg["interval_hours"] * 3600 - elapsed)
    next_in = f"{remaining_sec/3600:.1f} ঘন্টা পর"

    if ok:
        await update.message.reply_text(
            f"✅ Post channel এ গেছে!\n🕐 পরবর্তী post: <b>{next_in}</b>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "❌ Post পাঠানো যায়নি! logs/bot.log দেখুন।"
        )

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cfg   = load_cfg()
    posts = load_posts()
    current_idx = cfg["post_index"] % len(posts) if posts else 0
    next_post   = posts[current_idx]["id"] if posts else "-"
    last_posted = cfg.get("last_posted_at") or "কখনো হয়নি"
    elapsed     = seconds_since_last_post(cfg)
    remaining   = max(0, cfg["interval_hours"] * 3600 - elapsed)
    next_in     = f"{remaining/3600:.1f} ঘন্টা পর"

    await update.message.reply_text(
        "📊 <b>Bot Status</b>\n\n"
        f"📦 মোট Post: <b>{len(posts)}</b>\n"
        f"▶️ পরের Post: <b>#{next_post}</b>\n"
        f"⏱️ Interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
        f"🔄 Status: <b>{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}</b>\n"
        f"🕐 পরবর্তী post: <b>{next_in}</b>\n"
        f"📅 শেষ post: <b>{last_posted}</b>\n"
        f"📢 Channel: <code>{CHANNEL_ID}</code>",
        parse_mode="HTML",
    )

# ──────────────────────────── main ──────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("delete",   delete_post))
    app.add_handler(CommandHandler("clearall", clear_all))
    app.add_handler(CommandHandler("interval", set_interval))
    app.add_handler(CommandHandler("pause",    pause))
    app.add_handler(CommandHandler("resume",   resume))
    app.add_handler(CommandHandler("sendnow",  send_now))
    app.add_handler(CommandHandler("status",   status))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
        handle_message,
    ))

    # Bot start time save করি
    cfg = load_cfg()
    cfg["bot_started_at"] = now_str()
    save_cfg(cfg)

    # Watchdog: প্রতি ৬০ সেকেন্ডে check করবে
    scheduler.add_job(watchdog, "interval", seconds=60, args=[app], id="watchdog")
    scheduler.start()

    logger.info(f"🚀 Bot started | interval={cfg['interval_hours']}h | channel={CHANNEL_ID}")
    logger.info(f"   last_posted_at = {cfg.get('last_posted_at') or 'never'}")

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
