import os
import json
import asyncio
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# ─── Logging ───────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID  = os.environ.get("CHANNEL_ID", "@your_channel")   # e.g. @mychannel or -100xxxxxxxx
ADMIN_ID    = int(os.environ.get("ADMIN_ID", "0"))             # your Telegram user ID
POSTS_FILE  = "posts.json"
CONFIG_FILE = "config.json"
TIMEZONE    = "Asia/Dhaka"

# ─── Conversation states ────────────────────────────────────
(
    WAIT_PHOTO, WAIT_PRODUCT_NAME, WAIT_PRICE,
    WAIT_LINK, WAIT_CAPTION, WAIT_CONFIRM,
    WAIT_INTERVAL, WAIT_DELETE_ID
) = range(8)

# ─── Helpers ───────────────────────────────────────────────
def load_posts():
    if os.path.exists(POSTS_FILE):
        with open(POSTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_posts(posts):
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

def load_config():
    default = {"interval_hours": 2, "active": True, "post_index": 0}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
            for k, v in default.items():
                cfg.setdefault(k, v)
            return cfg
    return default

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def is_admin(update: Update):
    return update.effective_user.id == ADMIN_ID

def build_post_text(post):
    lines = []
    if post.get("product_name"):
        lines.append(f"🛍️ *{post['product_name']}*")
    if post.get("price"):
        lines.append(f"💰 মূল্য: *{post['price']}*")
    if post.get("caption"):
        lines.append(f"\n{post['caption']}")
    if post.get("link"):
        lines.append(f"\n🔗 [এখনই কিনুন]({post['link']})")
    lines.append("\n✅ Daraz Affiliate | দ্রুত অর্ডার করুন!")
    return "\n".join(lines)

# ─── Scheduler ─────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))

async def send_next_post(app):
    cfg   = load_config()
    posts = load_posts()
    if not posts or not cfg.get("active"):
        return

    idx  = cfg["post_index"] % len(posts)
    post = posts[idx]
    cfg["post_index"] = (idx + 1) % len(posts)
    save_config(cfg)

    text = build_post_text(post)
    try:
        if post.get("photo_id"):
            await app.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=post["photo_id"],
                caption=text,
                parse_mode="Markdown"
            )
        else:
            await app.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=False
            )
        logger.info(f"✅ Post sent: {post.get('product_name', idx)}")
    except Exception as e:
        logger.error(f"❌ Send error: {e}")

def restart_scheduler(app, interval_hours):
    scheduler.remove_all_jobs()
    scheduler.add_job(
        send_next_post,
        "interval",
        hours=interval_hours,
        args=[app],
        id="post_job",
        next_run_time=None   # don't fire immediately on reschedule
    )

# ─── /start ────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔ আপনি admin নন।")

    cfg   = load_config()
    posts = load_posts()
    text  = (
        f"👋 *Daraz Affiliate Bot*\n\n"
        f"📦 মোট Post: *{len(posts)}*\n"
        f"⏱️ Interval: *{cfg['interval_hours']} ঘন্টা*\n"
        f"🔄 Status: *{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}*\n\n"
        f"*কমান্ড সমূহ:*\n"
        f"/addpost — নতুন post যোগ করুন\n"
        f"/listposts — সব post দেখুন\n"
        f"/deletepost — post মুছুন\n"
        f"/setinterval — interval পরিবর্তন করুন\n"
        f"/pause — bot pause করুন\n"
        f"/resume — bot চালু করুন\n"
        f"/sendnow — এখনই একটি post পাঠান\n"
        f"/status — bot এর অবস্থা দেখুন"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── /addpost conversation ──────────────────────────────────
async def addpost_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    ctx.user_data.clear()
    await update.message.reply_text(
        "📸 *Product এর ছবি পাঠান।*\n_(ছবি না থাকলে /skip লিখুন)_",
        parse_mode="Markdown"
    )
    return WAIT_PHOTO

async def got_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        ctx.user_data["photo_id"] = update.message.photo[-1].file_id
    await update.message.reply_text("✏️ *Product এর নাম লিখুন:*", parse_mode="Markdown")
    return WAIT_PRODUCT_NAME

async def skip_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["photo_id"] = None
    await update.message.reply_text("✏️ *Product এর নাম লিখুন:*", parse_mode="Markdown")
    return WAIT_PRODUCT_NAME

async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["product_name"] = update.message.text.strip()
    await update.message.reply_text("💰 *মূল্য লিখুন* (যেমন: ৳ ১,২৯৯):", parse_mode="Markdown")
    return WAIT_PRICE

async def got_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["price"] = update.message.text.strip()
    await update.message.reply_text(
        "🔗 *Daraz Affiliate Link দিন:*\n_(না থাকলে /skip)_",
        parse_mode="Markdown"
    )
    return WAIT_LINK

async def got_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["link"] = update.message.text.strip()
    await update.message.reply_text(
        "📝 *Extra caption/description লিখুন:*\n_(না থাকলে /skip)_",
        parse_mode="Markdown"
    )
    return WAIT_CAPTION

async def skip_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["link"] = ""
    await update.message.reply_text(
        "📝 *Extra caption/description লিখুন:*\n_(না থাকলে /skip)_",
        parse_mode="Markdown"
    )
    return WAIT_CAPTION

async def got_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["caption"] = update.message.text.strip()
    return await show_preview(update, ctx)

async def skip_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["caption"] = ""
    return await show_preview(update, ctx)

async def show_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    post = ctx.user_data
    preview = build_post_text(post)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Save করুন", callback_data="confirm_save"),
         InlineKeyboardButton("❌ বাদ দিন", callback_data="confirm_cancel")]
    ])
    if post.get("photo_id"):
        await update.message.reply_photo(
            photo=post["photo_id"], caption=f"*Preview:*\n\n{preview}",
            parse_mode="Markdown", reply_markup=kb
        )
    else:
        await update.message.reply_text(
            f"*Preview:*\n\n{preview}", parse_mode="Markdown", reply_markup=kb
        )
    return WAIT_CONFIRM

async def confirm_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_save":
        posts = load_posts()
        new_post = {
            "id": len(posts) + 1,
            "photo_id":     ctx.user_data.get("photo_id"),
            "product_name": ctx.user_data.get("product_name"),
            "price":        ctx.user_data.get("price"),
            "link":         ctx.user_data.get("link"),
            "caption":      ctx.user_data.get("caption"),
            "created_at":   datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M")
        }
        posts.append(new_post)
        save_posts(posts)
        await query.edit_message_caption(
            caption=f"✅ *Post #{new_post['id']} সংরক্ষিত হয়েছে!*\nমোট post: {len(posts)}",
            parse_mode="Markdown"
        ) if new_post["photo_id"] else await query.edit_message_text(
            f"✅ *Post #{new_post['id']} সংরক্ষিত হয়েছে!*\nমোট post: {len(posts)}",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text("❌ বাতিল করা হয়েছে।")
    ctx.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ বাতিল।")
    return ConversationHandler.END

# ─── /listposts ─────────────────────────────────────────────
async def list_posts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    posts = load_posts()
    if not posts:
        return await update.message.reply_text("📭 কোনো post নেই।")

    cfg = load_config()
    current_idx = cfg["post_index"] % len(posts)
    lines = ["📋 *সব Post:*\n"]
    for i, p in enumerate(posts):
        marker = "▶️" if i == current_idx else "  "
        lines.append(
            f"{marker} *#{p['id']}* — {p.get('product_name','—')} | {p.get('price','—')}\n"
            f"    🕐 {p.get('created_at','')}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── /deletepost ────────────────────────────────────────────
async def delete_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    posts = load_posts()
    if not posts:
        return await update.message.reply_text("📭 কোনো post নেই।")
    lines = ["🗑️ *কোন post মুছবেন? ID লিখুন:*\n"]
    for p in posts:
        lines.append(f"#{p['id']} — {p.get('product_name','—')}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return WAIT_DELETE_ID

async def delete_by_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        del_id = int(update.message.text.strip().replace("#",""))
    except ValueError:
        await update.message.reply_text("❌ সঠিক ID দিন।")
        return WAIT_DELETE_ID

    posts = load_posts()
    new_posts = [p for p in posts if p["id"] != del_id]
    if len(new_posts) == len(posts):
        await update.message.reply_text("❌ এই ID এর post পাওয়া যায়নি।")
        return WAIT_DELETE_ID

    # Re-index
    for i, p in enumerate(new_posts, 1):
        p["id"] = i
    save_posts(new_posts)

    cfg = load_config()
    cfg["post_index"] = 0
    save_config(cfg)

    await update.message.reply_text(f"✅ Post #{del_id} মুছে ফেলা হয়েছে। মোট: {len(new_posts)}")
    return ConversationHandler.END

# ─── /setinterval ───────────────────────────────────────────
async def set_interval_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    cfg = load_config()
    await update.message.reply_text(
        f"⏱️ *বর্তমান interval: {cfg['interval_hours']} ঘন্টা*\n\nনতুন interval (ঘন্টায়) লিখুন:\n_উদাহরণ: 1, 2, 3, 6, 12_",
        parse_mode="Markdown"
    )
    return WAIT_INTERVAL

async def got_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        hours = float(update.message.text.strip())
        if hours < 0.1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ সঠিক সংখ্যা দিন (যেমন: 1, 2, 6)")
        return WAIT_INTERVAL

    cfg = load_config()
    cfg["interval_hours"] = hours
    save_config(cfg)

    restart_scheduler(ctx.application, hours)
    await update.message.reply_text(
        f"✅ Interval *{hours} ঘন্টা* সেট হয়েছে!\n_পরবর্তী post {hours} ঘন্টা পর যাবে।_",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ─── /pause /resume ─────────────────────────────────────────
async def pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    cfg = load_config()
    cfg["active"] = False
    save_config(cfg)
    await update.message.reply_text("⏸️ Bot pause করা হয়েছে। Post যাবে না।")

async def resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    cfg = load_config()
    cfg["active"] = True
    save_config(cfg)
    await update.message.reply_text("▶️ Bot চালু হয়েছে! Posts যেতে থাকবে।")

# ─── /sendnow ───────────────────────────────────────────────
async def send_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text("📤 এখনই পাঠানো হচ্ছে...")
    await send_next_post(ctx.application)
    await update.message.reply_text("✅ Post পাঠানো হয়েছে!")

# ─── /status ────────────────────────────────────────────────
async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    cfg   = load_config()
    posts = load_posts()
    jobs  = scheduler.get_jobs()
    next_run = "N/A"
    if jobs:
        nrt = jobs[0].next_run_time
        if nrt:
            next_run = nrt.strftime("%d %b %Y, %I:%M %p")

    await update.message.reply_text(
        f"📊 *Bot Status*\n\n"
        f"📦 মোট Post: *{len(posts)}*\n"
        f"⏱️ Interval: *{cfg['interval_hours']} ঘন্টা*\n"
        f"🔄 Status: *{'✅ চালু' if cfg['active'] else '⏸️ বন্ধ'}*\n"
        f"🕐 পরবর্তী Post: *{next_run}*\n"
        f"📢 Channel: `{CHANNEL_ID}`",
        parse_mode="Markdown"
    )

# ─── Main ──────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Add post conversation
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("addpost", addpost_start)],
        states={
            WAIT_PHOTO:        [MessageHandler(filters.PHOTO, got_photo),
                                CommandHandler("skip", skip_photo)],
            WAIT_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            WAIT_PRICE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, got_price)],
            WAIT_LINK:         [MessageHandler(filters.TEXT & ~filters.COMMAND, got_link),
                                CommandHandler("skip", skip_link)],
            WAIT_CAPTION:      [MessageHandler(filters.TEXT & ~filters.COMMAND, got_caption),
                                CommandHandler("skip", skip_caption)],
            WAIT_CONFIRM:      [CallbackQueryHandler(confirm_handler, pattern="^confirm_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Delete post conversation
    del_conv = ConversationHandler(
        entry_points=[CommandHandler("deletepost", delete_start)],
        states={
            WAIT_DELETE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_by_id)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Interval conversation
    interval_conv = ConversationHandler(
        entry_points=[CommandHandler("setinterval", set_interval_start)],
        states={
            WAIT_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_interval)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("listposts", list_posts))
    app.add_handler(CommandHandler("pause",  pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("sendnow", send_now))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(add_conv)
    app.add_handler(del_conv)
    app.add_handler(interval_conv)

    # Start scheduler
    cfg = load_config()
    restart_scheduler(app, cfg["interval_hours"])
    scheduler.start()
    logger.info(f"✅ Bot started | Interval: {cfg['interval_hours']}h | Channel: {CHANNEL_ID}")

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
