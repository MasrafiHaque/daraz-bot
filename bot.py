import os
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

def load_posts():
    if os.path.exists(POSTS_FILE):
        with open(POSTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_posts(posts):
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

def load_cfg():
    default = {"interval_hours": 2, "active": True, "post_index": 0}
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

scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))

async def send_next_post(app):
    cfg   = load_cfg()
    posts = load_posts()
    if not posts or not cfg.get("active"):
        return
    idx  = cfg["post_index"] % len(posts)
    post = posts[idx]
    cfg["post_index"] = (idx + 1) % len(posts)
    save_cfg(cfg)
    try:
        if post.get("photo_id"):
            await app.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=post["photo_id"],
                caption=post.get("caption", ""),
                parse_mode="HTML"
            )
        else:
            await app.bot.send_message(
                chat_id=CHANNEL_ID,
                text=post.get("caption", ""),
                parse_mode="HTML",
                disable_web_page_preview=False
            )
        logger.info(f"Post {idx+1} sent.")
    except Exception as e:
        logger.error(f"Send error: {e}")

def restart_scheduler(app, hours):
    scheduler.remove_all_jobs()
    scheduler.add_job(send_next_post, "interval", hours=hours, args=[app], id="post_job")

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔ আপনি admin নন।")
    cfg   = load_cfg()
    posts = load_posts()
    await update.message.reply_text(
        f"👋 <b>Daraz Affiliate Bot</b>\n\n"
        f"📦 মোট Post: <b>{len(posts)}</b>\n"
        f"⏱️ Interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
        f"🔄 Status: <b>{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}</b>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>📤 Post যোগ করতে:</b>\n"
        f"ছবি পাঠান (caption সহ) অথবা শুধু text পাঠান।\n"
        f"Bot preview দেখাবে → ✅ বা ❌ চাপুন।\n\n"
        f"<b>⚙️ কমান্ড:</b>\n"
        f"/list — সব post দেখুন\n"
        f"/delete — post মুছুন\n"
        f"/interval 3 — interval সেট করুন\n"
        f"/pause — bot বন্ধ করুন\n"
        f"/resume — bot চালু করুন\n"
        f"/sendnow — এখনই post পাঠান\n"
        f"/status — bot এর অবস্থা",
        parse_mode="HTML"
    )

# ── Message Handler ─────────────────────────────────────────
# Bug fix: photo_id কে callback_data তে রাখা যায় না (64 byte limit)
# তাই pending post টা context.bot_data তে রাখছি

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    msg = update.message

    if msg.photo:
        photo_id = msg.photo[-1].file_id
        caption  = msg.caption or ""

        # pending post সংরক্ষণ করি bot_data তে
        ctx.bot_data["pending"] = {"photo_id": photo_id, "caption": caption}

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Schedule করুন", callback_data="save"),
            InlineKeyboardButton("❌ বাদ দিন",       callback_data="cancel")
        ]])
        await msg.reply_photo(
            photo=photo_id,
            caption=f"<b>📋 Preview:</b>\n\n{caption or '(caption নেই)'}\n\n<i>Channel এ schedule করবেন?</i>",
            parse_mode="HTML",
            reply_markup=kb
        )

    elif msg.text and not msg.text.startswith("/"):
        text = msg.text
        ctx.bot_data["pending"] = {"photo_id": None, "caption": text}

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Schedule করুন", callback_data="save"),
            InlineKeyboardButton("❌ বাদ দিন",       callback_data="cancel")
        ]])
        await msg.reply_text(
            f"<b>📋 Preview:</b>\n\n{text}\n\n<i>Channel এ schedule করবেন?</i>",
            parse_mode="HTML",
            reply_markup=kb
        )

# ── Callback Handler ────────────────────────────────────────
async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "cancel":
        ctx.bot_data.pop("pending", None)
        try:
            await query.edit_message_caption(caption="❌ বাতিল করা হয়েছে।")
        except:
            await query.edit_message_text("❌ বাতিল করা হয়েছে।")
        return

    if data == "save":
        pending = ctx.bot_data.pop("pending", None)
        if not pending:
            await query.edit_message_text("❌ কিছু একটা সমস্যা হয়েছে, আবার পাঠান।")
            return

        posts    = load_posts()
        new_post = {
            "id":         len(posts) + 1,
            "photo_id":   pending["photo_id"],
            "caption":    pending["caption"],
            "created_at": datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M")
        }
        posts.append(new_post)
        save_posts(posts)

        cfg = load_cfg()
        msg = (
            f"✅ <b>Post #{new_post['id']} সংরক্ষিত!</b>\n"
            f"📦 মোট post: {len(posts)}\n"
            f"⏱️ পরবর্তী post যাবে <b>{cfg['interval_hours']} ঘন্টা</b> পর।"
        )
        try:
            await query.edit_message_caption(caption=msg, parse_mode="HTML")
        except:
            await query.edit_message_text(msg, parse_mode="HTML")

    elif data.startswith("del|"):
        del_id = int(data.split("|")[1])
        posts  = [p for p in load_posts() if p["id"] != del_id]
        for i, p in enumerate(posts, 1):
            p["id"] = i
        save_posts(posts)
        cfg = load_cfg()
        cfg["post_index"] = 0
        save_cfg(cfg)
        await query.edit_message_text(f"🗑️ Post #{del_id} মুছে ফেলা হয়েছে।")

async def list_posts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    posts = load_posts()
    if not posts:
        return await update.message.reply_text("📭 কোনো post নেই। ছবি বা text পাঠিয়ে post যোগ করুন।")

    cfg     = load_cfg()
    current = cfg["post_index"] % len(posts)
    lines   = ["📋 <b>সব Post:</b>\n"]
    for i, p in enumerate(posts):
        marker  = "▶️" if i == current else "  "
        preview = (p.get("caption") or "")[:45].replace("\n", " ")
        icon    = "🖼️" if p.get("photo_id") else "📝"
        lines.append(f"{marker}{icon} <b>#{p['id']}</b> — {preview}…\n    🕐 {p.get('created_at','')}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def delete_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
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
    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="cancel")])
    await update.message.reply_text(
        "🗑️ <b>কোন post মুছবেন?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def set_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    args = ctx.args
    if not args:
        cfg = load_cfg()
        return await update.message.reply_text(
            f"⏱️ বর্তমান interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n\n"
            f"পরিবর্তন করতে: <code>/interval 3</code>",
            parse_mode="HTML"
        )
    try:
        hours = float(args[0])
        if hours < 0.1: raise ValueError
    except:
        return await update.message.reply_text("❌ সঠিক সংখ্যা দিন। যেমন: /interval 3")

    cfg = load_cfg()
    cfg["interval_hours"] = hours
    save_cfg(cfg)
    restart_scheduler(ctx.application, hours)
    await update.message.reply_text(f"✅ Interval <b>{hours} ঘন্টা</b> সেট হয়েছে!", parse_mode="HTML")

async def pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    cfg = load_cfg(); cfg["active"] = False; save_cfg(cfg)
    await update.message.reply_text("⏸️ Bot pause — আর post যাবে না।")

async def resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    cfg = load_cfg(); cfg["active"] = True; save_cfg(cfg)
    await update.message.reply_text("▶️ Bot চালু — post যেতে থাকবে।")

async def send_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not load_posts():
        return await update.message.reply_text("📭 কোনো post নেই।")
    await update.message.reply_text("📤 পাঠানো হচ্ছে...")
    await send_next_post(ctx.application)
    await update.message.reply_text("✅ Post channel এ গেছে!")

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    cfg   = load_cfg()
    posts = load_posts()
    jobs  = scheduler.get_jobs()
    next_run = "N/A"
    if jobs and jobs[0].next_run_time:
        next_run = jobs[0].next_run_time.strftime("%d %b, %I:%M %p")
    await update.message.reply_text(
        f"📊 <b>Bot Status</b>\n\n"
        f"📦 মোট Post: <b>{len(posts)}</b>\n"
        f"⏱️ Interval: <b>{cfg['interval_hours']} ঘন্টা</b>\n"
        f"🔄 Status: <b>{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}</b>\n"
        f"🕐 পরবর্তী Post: <b>{next_run}</b>\n"
        f"📢 Channel: <code>{CHANNEL_ID}</code>",
        parse_mode="HTML"
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("list",     list_posts))
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
