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

def entities_to_list(entities):
    if not entities:
        return []
    return [{
        "type":   e.type.value if hasattr(e.type, "value") else str(e.type),
        "offset": e.offset,
        "length": e.length,
        "url":    e.url or ""
    } for e in entities]

def list_to_entities(entity_list):
    if not entity_list:
        return None
    return [MessageEntity(
        type=e["type"], offset=e["offset"],
        length=e["length"], url=e.get("url") or None
    ) for e in entity_list]

def extract_post_from_msg(msg):
    tz = pytz.timezone(TIMEZONE)
    if msg.photo:
        return {
            "photo_id":   msg.photo[-1].file_id,
            "caption":    msg.caption or "",
            "entities":   entities_to_list(msg.caption_entities),
            "created_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        }
    elif msg.text:
        return {
            "photo_id":   None,
            "caption":    msg.text,
            "entities":   entities_to_list(msg.entities),
            "created_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        }
    return None

def get_next_post_id(posts, cfg):
    """Currently queued next post er id return koro"""
    if not posts:
        return None
    idx = cfg["post_index"] % len(posts)
    return posts[idx]["id"]

def fix_index_after_change(cfg, posts, old_next_id):
    """
    Add/delete er pore index thik koro.
    old_next_id = age je post next hoto tar id.
    Sei id ekhono thakle same post e thako.
    Delete hoye gele same slot e thako (circular).
    """
    if not posts:
        cfg["post_index"] = 0
        return
    total = len(posts)
    for i, p in enumerate(posts):
        if p["id"] == old_next_id:
            cfg["post_index"] = i
            return
    # deleted — same position e thako
    old_idx = cfg.get("post_index", 0)
    cfg["post_index"] = old_idx % total

def append_post_safe(post, posts, cfg):
    """
    Post add koro index preserve kore.
    Returns: (updated_posts, new_id)
    """
    old_next_id = get_next_post_id(posts, cfg)
    new_id = (posts[-1]["id"] + 1) if posts else 1
    post["id"] = new_id
    posts.append(post)
    save_posts(posts)
    if old_next_id is not None:
        fix_index_after_change(cfg, posts, old_next_id)
    # if no posts existed before, index stays 0 (first post)
    save_cfg(cfg)
    return posts, new_id

# ─────────────────────────── scheduler ──────────────────────────

scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))

async def send_next_post(app):
    cfg   = load_cfg()
    posts = load_posts()
    if not posts or not cfg.get("active"):
        return

    total    = len(posts)
    idx      = cfg["post_index"] % total
    post     = posts[idx]
    next_idx = (idx + 1) % total
    cfg["post_index"] = next_idx

    if next_idx == 0:
        logger.info("All posts cycled — restarting from #1")

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
        logger.info(f"Sent post {idx + 1}/{total}")
    except Exception as e:
        logger.error(f"Send error: {e}")

def restart_scheduler(app, hours):
    scheduler.remove_all_jobs()
    scheduler.add_job(send_next_post, "interval", hours=hours, args=[app], id="post_job")

# ──────────────────────────── commands ──────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("Access denied.")
    cfg   = load_cfg()
    posts = load_posts()
    await update.message.reply_text(
        "<b>Daraz Affiliate Bot</b>\n\n"
        f"Total Posts: <b>{len(posts)}</b>\n"
        f"Interval: <b>{cfg['interval_hours']}h</b>\n"
        f"Status: <b>{'Active' if cfg['active'] else 'Paused'}</b>\n"
        "Cycle: After last post, restarts from #1\n\n"
        "<b>Add Posts:</b>\n"
        "- Send photo (with or without caption)\n"
        "- Send text\n"
        "- Forward one or multiple posts\n\n"
        "<b>Commands:</b>\n"
        "/schedule — view all scheduled posts\n"
        "/schedule cancel — cancel and reset to #1\n"
        "/delete — remove a post\n"
        "/interval 2 — set interval (hours)\n"
        "/pause — pause posting\n"
        "/resume — resume posting\n"
        "/sendnow — send immediately\n"
        "/status — current status",
        parse_mode="HTML"
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    msg  = update.message
    post = extract_post_from_msg(msg)
    if not post:
        return

    # Forwarded: directly add, preserve index
    if msg.forward_origin or msg.forward_from or msg.forward_from_chat:
        posts = load_posts()
        cfg   = load_cfg()
        posts, new_id = append_post_safe(post, posts, cfg)

        icon    = "IMG" if post["photo_id"] else "TXT"
        preview = (post["caption"] or "")[:40].replace("\n", " ")
        suffix  = "..." if len(post["caption"] or "") > 40 else ""
        await msg.reply_text(
            f"Added [{icon}] #{new_id} | Total: {len(posts)}\n{preview}{suffix}"
        )
        return

    # Normal message: ask confirmation
    ctx.bot_data["pending"] = post
    icon = "Photo" if post["photo_id"] else "Text"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Add to schedule", callback_data="save"),
        InlineKeyboardButton("Discard",         callback_data="discard")
    ]])

    if post["photo_id"]:
        await msg.reply_photo(
            photo=post["photo_id"],
            caption=f"Preview [{icon}] — Add to schedule?",
            reply_markup=kb
        )
    else:
        await msg.reply_text(
            f"Preview:\n\n{post['caption']}\n\n---\nAdd to schedule?",
            reply_markup=kb
        )

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "discard":
        ctx.bot_data.pop("pending", None)
        await query.edit_message_text("Discarded.")
        return

    if data == "save":
        pending = ctx.bot_data.pop("pending", None)
        if not pending:
            await query.edit_message_text("Error — please send again.")
            return

        posts = load_posts()
        cfg   = load_cfg()
        posts, new_id = append_post_safe(pending, posts, cfg)

        await query.edit_message_text(
            f"Added #{new_id} | Total: {len(posts)}"
        )
        return

    if data.startswith("del|"):
        del_id = int(data.split("|")[1])
        posts  = load_posts()
        cfg    = load_cfg()

        # Save next post id BEFORE deleting
        old_next_id = get_next_post_id(posts, cfg)

        posts = [p for p in posts if p["id"] != del_id]
        save_posts(posts)

        fix_index_after_change(cfg, posts, old_next_id)
        save_cfg(cfg)

        await query.edit_message_text(
            f"Deleted #{del_id} | Remaining: {len(posts)}"
        )

async def schedule_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /schedule       — list all posts
    /schedule all   — same
    /schedule cancel — pause + reset index to 0
    """
    if not is_admin(update):
        return

    sub = (ctx.args[0].lower() if ctx.args else "all")

    if sub == "cancel":
        cfg = load_cfg()
        cfg["active"]     = False
        cfg["post_index"] = 0
        save_cfg(cfg)
        await update.message.reply_text(
            "Schedule cancelled. Index reset to #1.\n"
            "Use /resume to start again."
        )
        return

    # list
    posts = load_posts()
    if not posts:
        return await update.message.reply_text(
            "No posts yet. Send a photo or text to add."
        )

    cfg     = load_cfg()
    current = cfg["post_index"] % len(posts)
    jobs    = scheduler.get_jobs()
    next_run = "N/A"
    if jobs and jobs[0].next_run_time:
        next_run = jobs[0].next_run_time.strftime("%d %b, %I:%M %p")

    lines = [
        f"<b>Scheduled Posts ({len(posts)})</b>",
        f"Next post at: <b>{next_run}</b>\n"
    ]
    for i, p in enumerate(posts):
        marker  = ">" if i == current else " "
        preview = (p.get("caption") or "")[:50].replace("\n", " ")
        suffix  = "..." if len(p.get("caption", "")) > 50 else ""
        icon    = "[IMG]" if p.get("photo_id") else "[TXT]"
        lines.append(
            f"{marker} {icon} <b>#{p['id']}</b> {preview}{suffix}\n"
            f"    {p.get('created_at', '')}"
        )

    lines.append("\nCycles from #1 after last post.")
    lines.append("Use /schedule cancel to reset index.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def delete_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    posts = load_posts()
    if not posts:
        return await update.message.reply_text("No posts.")

    buttons = []
    for p in posts:
        preview = (p.get("caption") or "")[:30].replace("\n", " ")
        icon    = "[IMG]" if p.get("photo_id") else "[TXT]"
        buttons.append([InlineKeyboardButton(
            f"{icon} #{p['id']} {preview}",
            callback_data=f"del|{p['id']}"
        )])
    buttons.append([InlineKeyboardButton("Cancel", callback_data="discard")])
    await update.message.reply_text(
        "<b>Select post to delete:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def set_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        cfg = load_cfg()
        return await update.message.reply_text(
            f"Current interval: <b>{cfg['interval_hours']}h</b>\n"
            f"Change with: <code>/interval 2</code>",
            parse_mode="HTML"
        )
    try:
        hours = float(ctx.args[0])
        if hours < 0.1:
            raise ValueError
    except Exception:
        return await update.message.reply_text("Invalid. Example: /interval 2")

    cfg = load_cfg()
    cfg["interval_hours"] = hours
    save_cfg(cfg)
    restart_scheduler(ctx.application, hours)
    await update.message.reply_text(
        f"Interval set to <b>{hours}h</b>",
        parse_mode="HTML"
    )

async def pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cfg = load_cfg()
    cfg["active"] = False
    save_cfg(cfg)
    await update.message.reply_text("Bot paused.")

async def resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cfg = load_cfg()
    cfg["active"] = True
    save_cfg(cfg)
    await update.message.reply_text("Bot resumed.")

async def send_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not load_posts():
        return await update.message.reply_text("No posts.")
    await update.message.reply_text("Sending now...")
    await send_next_post(ctx.application)
    await update.message.reply_text("Done!")

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cfg   = load_cfg()
    posts = load_posts()
    jobs  = scheduler.get_jobs()
    next_run = "N/A"
    if jobs and jobs[0].next_run_time:
        next_run = jobs[0].next_run_time.strftime("%d %b, %I:%M %p")
    current_idx   = cfg["post_index"] % len(posts) if posts else 0
    await update.message.reply_text(
        "<b>Bot Status</b>\n\n"
        f"Total Posts: <b>{len(posts)}</b>\n"
        f"Next Post: <b>#{current_idx + 1}</b> / {len(posts)}\n"
        f"Interval: <b>{cfg['interval_hours']}h</b>\n"
        f"Status: <b>{'Active' if cfg['active'] else 'Paused'}</b>\n"
        f"Next run: <b>{next_run}</b>\n"
        f"Channel: <code>{CHANNEL_ID}</code>",
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
