import os
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@your_channel")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
POSTS_FILE = "posts.json"
CFG_FILE   = "config.json"
TIMEZONE   = "Asia/Dhaka"

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
    default = {"interval_hours": 1, "active": True, "post_index": 0}
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

# ── FIX 3: entities সংরক্ষণ — bold/underline/quote হুবহু থাকবে ──

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
    result = []
    for e in entity_list:
        result.append(MessageEntity(
            type=e["type"],
            offset=e["offset"],
            length=e["length"],
            url=e.get("url") or None
        ))
    return result

def extract_post_from_msg(msg):
    tz = pytz.timezone(TIMEZONE)
    if msg.photo:
        return {
            "photo_id":   msg.photo[-1].file_id,
            "caption":    msg.caption or "",
            "entities":   entities_to_list(msg.caption_entities),
            "created_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        }
    elif msg.text and not msg.text.startswith("/"):
        return {
            "photo_id":   None,
            "caption":    msg.text,
            "entities":   entities_to_list(msg.entities),
            "created_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        }
    return None

# ── FIX 2: add/delete এর পরেও index ঠিক থাকবে ──────────────────
# পরের post কোনটা হবে সেটা id দিয়ে track করি, index দিয়ে না।
# এতে নতুন post add বা যেকোনো post delete করলেও
# পরের post একই থাকবে।

def get_current_next_id(posts, cfg):
    """এই মুহূর্তে next post এর id কত"""
    if not posts:
        return None
    idx = cfg["post_index"] % len(posts)
    return posts[idx]["id"]

def restore_index_by_id(posts, cfg, target_id):
    """target_id এর post কে next বানাই"""
    if not posts:
        cfg["post_index"] = 0
        return
    for i, p in enumerate(posts):
        if p["id"] == target_id:
            cfg["post_index"] = i
            return
    # target_id delete হয়ে গেলে — same position এ থাকি
    old = cfg.get("post_index", 0)
    cfg["post_index"] = old % len(posts)

def add_post_preserve_index(new_post, posts, cfg):
    """
    Post list এ add করি, কিন্তু next post একই থাকে।
    মানে নতুন post add করলে চলমান sequence নষ্ট হবে না।
    """
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
    """
    Post delete করি, কিন্তু next post একই থাকে
    (delete হয়ে গেলে তার পরেরটা next হয়)।
    """
    next_id = get_current_next_id(posts, cfg)
    posts   = [p for p in posts if p["id"] != del_id]
    save_posts(posts)
    if next_id == del_id:
        # deleted post ছিল next — এর পরেরটা next হবে
        # restore_index_by_id match করবে না, তাই same slot রাখি
        old = cfg.get("post_index", 0)
        cfg["post_index"] = old % len(posts) if posts else 0
    else:
        restore_index_by_id(posts, cfg, next_id)
    save_cfg(cfg)
    return posts

# ─────────────────────────── scheduler ──────────────────────────

scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))

async def send_next_post(app):
    cfg   = load_cfg()
    posts = load_posts()
    if not posts or not cfg.get("active"):
        return

    total = len(posts)
    idx   = cfg["post_index"] % total
    post  = posts[idx]

    cfg["post_index"] = (idx + 1) % total
    save_cfg(cfg)

    caption  = post.get("caption", "")
    entities = list_to_entities(post.get("entities", []))

    try:
        if post.get("photo_id"):
            await app.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=post["photo_id"],
                caption=caption,
                caption_entities=entities
            )
        else:
            await app.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                entities=entities,
                disable_web_page_preview=False
            )
        logger.info(f"Sent post #{post['id']} ({idx+1}/{total})")
    except Exception as e:
        logger.error(f"Send error: {e}")

def restart_scheduler(app, hours):
    scheduler.remove_all_jobs()
    scheduler.add_job(send_next_post, "interval", hours=hours, args=[app], id="post_job")

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
        "• একাধিক post <b>forward</b> করুন — সব একসাথে add হবে\n\n"
        "<b>⚙️ কমান্ড:</b>\n"
        "/schedule — সব post দেখুন ও পরের post জানুন\n"
        "/delete — post মুছুন\n"
        "/interval 2 — interval সেট করুন\n"
        "/pause — bot বন্ধ করুন\n"
        "/resume — bot চালু করুন\n"
        "/sendnow — এখনই post পাঠান\n"
        "/status — bot এর অবস্থা",
        parse_mode="HTML"
    )

# ── FIX 1: Forward করে একাধিক post একসাথে add ──────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    msg  = update.message
    post = extract_post_from_msg(msg)
    if not post:
        return

    is_forwarded = bool(
        msg.forward_origin or
        msg.forward_from or
        msg.forward_from_chat or
        msg.forward_sender_name
    )

    if is_forwarded:
        # Forward করা post সরাসরি add হয়, confirmation ছাড়া
        # Index preserve হয়
        posts = load_posts()
        cfg   = load_cfg()
        new_id = add_post_preserve_index(post, posts, cfg)

        icon    = "🖼️" if post["photo_id"] else "📝"
        preview = (post["caption"] or "")[:50].replace("\n", " ")
        await msg.reply_text(
            f"✅ {icon} Post <b>#{new_id}</b> added!\n"
            f"📦 মোট: {len(posts) + 1 if new_id > len(posts) else len(load_posts())}\n"
            f"<i>{preview}{'…' if len(post['caption'] or '') > 50 else ''}</i>\n\n"
            f"💡 একাধিক post forward করলে সব add হবে।",
            parse_mode="HTML"
        )
        return

    # Normal message — preview দেখাই, confirmation নিই
    ctx.bot_data["pending"] = post
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Schedule করুন", callback_data="save"),
        InlineKeyboardButton("❌ বাদ দিন",       callback_data="discard")
    ]])

    if post["photo_id"]:
        await msg.reply_photo(
            photo=post["photo_id"],
            caption="📋 Preview — হুবহু এভাবেই channel এ যাবে\n\nSchedule করবেন?",
            reply_markup=kb
        )
    else:
        await msg.reply_text(
            "📋 Preview — হুবহু এভাবেই channel এ যাবে\n\nSchedule করবেন?",
            reply_markup=kb
        )

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "discard":
        ctx.bot_data.pop("pending", None)
        try:
            await query.edit_message_caption(caption="❌ বাতিল।")
        except:
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
        posts  = load_posts()  # reload after save

        msg = (
            f"✅ Post <b>#{new_id}</b> schedule এ যোগ হয়েছে!\n"
            f"📦 মোট post: {len(posts)}\n"
            f"⏱️ পরবর্তী post: {cfg['interval_hours']} ঘন্টা পর"
        )
        try:
            await query.edit_message_caption(caption=msg, parse_mode="HTML")
        except:
            await query.edit_message_text(msg, parse_mode="HTML")
        return

    if data.startswith("del|"):
        del_id = int(data.split("|")[1])
        posts  = load_posts()
        cfg    = load_cfg()
        posts  = delete_post_preserve_index(del_id, posts, cfg)
        await query.edit_message_text(
            f"🗑️ Post <b>#{del_id}</b> মুছে ফেলা হয়েছে।\n"
            f"📦 বাকি post: {len(posts)}",
            parse_mode="HTML"
        )

async def schedule_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    sub = (ctx.args[0].lower() if ctx.args else "list")

    if sub == "cancel":
        cfg = load_cfg()
        cfg["active"]     = False
        cfg["post_index"] = 0
        save_cfg(cfg)
        await update.message.reply_text(
            "⏸️ Schedule বাতিল। Index #1 এ reset।\n/resume দিয়ে আবার চালু করুন।"
        )
        return

    posts = load_posts()
    if not posts:
        return await update.message.reply_text("📭 কোনো post নেই।")

    cfg     = load_cfg()
    current = cfg["post_index"] % len(posts)
    jobs    = scheduler.get_jobs()
    next_run = "N/A"
    if jobs and jobs[0].next_run_time:
        next_run = jobs[0].next_run_time.strftime("%d %b, %I:%M %p")

    lines = [
        f"📋 <b>Scheduled Posts ({len(posts)})</b>",
        f"🕐 পরবর্তী post: <b>{next_run}</b>\n"
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
            callback_data=f"del|{p['id']}"
        )])
    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="discard")])
    await update.message.reply_text(
        "🗑️ <b>কোন post মুছবেন?</b>\n<i>(index preserve হবে)</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def set_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        cfg = load_cfg()
        return await update.message.reply_text(
            f"⏱️ বর্তমান interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
            f"পরিবর্তন করতে: <code>/interval 2</code>",
            parse_mode="HTML"
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
    restart_scheduler(ctx.application, hours)
    await update.message.reply_text(
        f"✅ Interval <b>{hours} ঘন্টা</b> সেট হয়েছে!",
        parse_mode="HTML"
    )

async def pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cfg = load_cfg()
    cfg["active"] = False
    save_cfg(cfg)
    await update.message.reply_text("⏸️ Bot pause — post যাবে না।")

async def resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cfg = load_cfg()
    cfg["active"] = True
    save_cfg(cfg)
    await update.message.reply_text("▶️ Bot চালু — post যেতে থাকবে।")

async def send_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not load_posts():
        return await update.message.reply_text("📭 কোনো post নেই।")
    await update.message.reply_text("📤 পাঠানো হচ্ছে...")
    await send_next_post(ctx.application)
    await update.message.reply_text("✅ Post channel এ গেছে!")

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cfg   = load_cfg()
    posts = load_posts()
    jobs  = scheduler.get_jobs()
    next_run = "N/A"
    if jobs and jobs[0].next_run_time:
        next_run = jobs[0].next_run_time.strftime("%d %b, %I:%M %p")
    current_idx = cfg["post_index"] % len(posts) if posts else 0
    next_post   = posts[current_idx]["id"] if posts else "-"
    await update.message.reply_text(
        "📊 <b>Bot Status</b>\n\n"
        f"📦 মোট Post: <b>{len(posts)}</b>\n"
        f"▶️ পরের Post: <b>#{next_post}</b>\n"
        f"⏱️ Interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
        f"🔄 Status: <b>{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}</b>\n"
        f"🕐 পরবর্তী run: <b>{next_run}</b>\n"
        f"📢 Channel: <code>{CHANNEL_ID}</code>",
        parse_mode="HTML"
    )

# ──────────────────────────── main ──────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("delete",   delete_post))
    app.add_handler(CommandHandler("interval", set_interval))
    app.add_handler(CommandHandler("pause",    pause))
    app.add_handler(CommandHandler("resume",   resume))
    app.add_handler(CommandHandler("sendnow",  send_now))
    app.add_handler(CommandHandler("status",   status))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
        handle_message
    ))

    cfg = load_cfg()
    restart_scheduler(app, cfg["interval_hours"])
    scheduler.start()
    logger.info(f"Bot started | {cfg['interval_hours']}h | {CHANNEL_ID}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
