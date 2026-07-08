import logging
import os

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
   filters,
)

from database import Database

# =========================================================
#  CONFIG
# =========================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://yamanakaaoi40_db_user:tdF4sO93jes8y8Wt@cluster0.h8gus3s.mongodb.net/?appName=Cluster0")
DB_NAME = os.getenv("DB_NAME", "vote_giveaway_bot")

# General admins - can use /end to close a giveaway early.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "8325139144").split(",") if x.strip().isdigit()}

# Owner - the ONLY person who can start a giveaway (/create) or manually
# add/remove votes (/addvote).
OWNER_ID = int(os.getenv("OWNER_ID", "8325139144"))

CHANNEL_CHAT_ID = int(os.getenv("CHANNEL_CHAT_ID", "-1003543492167"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@ACN_Updates")

# Force-subscribe chats (username or numeric chat id) - used for verify/participate/vote checks
FORCE_SUB_CHATS = [
    v for v in [os.getenv("FORCE_SUB_1"), os.getenv("FORCE_SUB_2"), os.getenv("FORCE_SUB_3")]
    if v
]

GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK")
CHANNEL2_LINK = os.getenv("CHANNEL2_LINK")
CHANNEL3_LINK = os.getenv("CHANNEL3_LINK")

SPAM_VOTE_THRESHOLD = int(os.getenv("SPAM_VOTE_THRESHOLD", "15"))
SPAM_VOTE_WINDOW_SECONDS = int(os.getenv("SPAM_VOTE_WINDOW_SECONDS", "60"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

db = Database(MONGO_URI, DB_NAME)

# Conversation states for /create
TITLE, WINNERS, DURATION = range(3)


# =========================================================
#  HELPERS
# =========================================================
def mention(user_id: int, name: str) -> str:
    """Clickable HTML mention that works even for users without a username."""
    safe_name = (name or "User").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


async def is_member_of_all(bot, user_id: int) -> bool:
    """Checks the user is a member of every force-sub chat."""
    for chat in FORCE_SUB_CHATS:
        try:
            member = await bot.get_chat_member(chat_id=chat, user_id=user_id)
            if member.status in ("left", "kicked"):
                return False
        except (BadRequest, Forbidden) as e:
            logger.warning("Membership check failed for %s / %s: %s", chat, user_id, e)
            return False
    return True


async def is_member_of_channel(bot, user_id: int) -> bool:
    """Checks membership only in the channel where voting/giveaway happens."""
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_CHAT_ID, user_id=user_id)
        return member.status not in ("left", "kicked")
    except (BadRequest, Forbidden) as e:
        logger.warning("Channel membership check failed for %s: %s", user_id, e)
        return False


def join_buttons() -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton("🎗️ Updates Channel", url=CHANNEL3_LINK),
        InlineKeyboardButton("✨ Community Group", url=CHANNEL2_LINK),
    ]
    row2 = [
        InlineKeyboardButton("⚡ Verified Channel", url=GROUP_INVITE_LINK),
        InlineKeyboardButton("✅ Verify", callback_data="verify_check"),
    ]
    return InlineKeyboardMarkup([row1, row2])


# =========================================================
#  /start
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.upsert_user(user.id, user.username, user.first_name)

    text = (
        f"👋 Welcome, {user.first_name}!\n\n"
        "😳 To take part in our <b>Voting Giveaways</b>, you must first join all of our "
        "official channels/group below, then tap <b>✅ Verify</b>.\n\n"
        "🎁 Once verified, you'll be able to Participate in and Vote on giveaways posted "
        f"in {CHANNEL_USERNAME}."
    )
    await update.message.reply_text(
        text, reply_markup=join_buttons(), parse_mode=ParseMode.HTML
    )


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    if await is_member_of_all(context.bot, user.id):
        await db.set_verified(user.id)
        await query.answer("✅ Verified successfully! You can now participate & vote.", show_alert=True)
        await query.edit_message_text(
            "🎉 You're verified!\n\n"
            f"Head over to {CHANNEL_USERNAME} to join active giveaways and vote for your "
            "favorite participants.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.answer(
            "❌ You haven't joined all the required channels/group yet. Please join all "
            "of them first, then tap Verify again.",
            show_alert=True,
        )


# =========================================================
#  /create  (owner-only conversation) - starts a giveaway
# =========================================================
async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 Only the owner can start a giveaway.")
        return ConversationHandler.END

    existing = await db.get_active_giveaway()
    if existing:
        await update.message.reply_text(
            "⚠️ There's already an active giveaway. Use /end to close it before "
            "starting a new one."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🎁 Let's create a new giveaway.\n\nSend me the <b>title/prize</b> of the giveaway "
        "(or /cancel to abort):",
        parse_mode=ParseMode.HTML,
    )
    return TITLE


async def create_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["gv_title"] = update.message.text.strip()
    await update.message.reply_text("👥 How many total winners will this giveaway have?")
    return WINNERS


async def create_winners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("Please send a valid positive number.")
        return WINNERS
    context.user_data["gv_winners"] = int(text)
    await update.message.reply_text(
        "⏱️ How long should voting last? Send duration in <b>minutes</b> "
        "(e.g. 1440 for 24 hours).",
        parse_mode=ParseMode.HTML,
    )
    return DURATION


async def create_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("Please send a valid positive number of minutes.")
        return DURATION

    duration = int(text)
    title = context.user_data["gv_title"]
    winners = context.user_data["gv_winners"]
    created_by = update.effective_user.id

    if not CHANNEL_CHAT_ID:
        await update.message.reply_text(
            "⚠️ CHANNEL_CHAT_ID is not configured in your .env — can't post the giveaway. "
            "Set CHANNEL_CHAT_ID to your channel's numeric chat id (e.g. -100xxxxxxxxxx) "
            "and try /create again."
        )
        context.user_data.clear()
        return ConversationHandler.END

    try:
        giveaway_id = await db.create_giveaway(title, winners, duration, created_by)
        giveaway = await db.get_giveaway(giveaway_id)

        caption = build_announcement_text(giveaway)
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🟢 Participate", callback_data=f"participate:{giveaway_id}")]]
        )
        msg = await context.bot.send_message(
            chat_id=CHANNEL_CHAT_ID,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        await db.set_channel_message(giveaway_id, CHANNEL_CHAT_ID, msg.message_id)

        if context.job_queue is None:
            # This happens when python-telegram-bot was installed WITHOUT the
            # job-queue extra. The giveaway is still created and posted, but it
            # will never auto-close - only /end will close it.
            logger.error(
                "context.job_queue is None - install with: "
                'pip install "python-telegram-bot[job-queue]". '
                "Auto-close will NOT work until this is fixed."
            )
            await update.message.reply_text(
                f"✅ Giveaway <b>{title}</b> created and posted in {CHANNEL_USERNAME}!\n"
                f"ID: <code>{giveaway_id}</code>\n\n"
                "⚠️ Heads up: auto-close is disabled because job-queue isn't installed. "
                "Use /end to close this giveaway manually, or fix it with:\n"
                '<code>pip install "python-telegram-bot[job-queue]"</code>',
                parse_mode=ParseMode.HTML,
            )
        else:
            context.job_queue.run_once(
                auto_end_giveaway,
                when=duration * 60,
                data={"giveaway_id": giveaway_id},
                name=f"end_{giveaway_id}",
            )
            await update.message.reply_text(
                f"✅ Giveaway <b>{title}</b> created and posted in {CHANNEL_USERNAME}!\n"
                f"ID: <code>{giveaway_id}</code>",
                parse_mode=ParseMode.HTML,
            )

    except (BadRequest, Forbidden) as e:
        logger.error("Failed to post giveaway announcement: %s", e)
        await update.message.reply_text(
            "❌ Couldn't post the announcement in the channel. Make sure the bot is "
            "an admin there and that CHANNEL_CHAT_ID is correct, then try /create again."
        )
    except Exception:
        logger.exception("Unexpected error in create_duration")
        await update.message.reply_text(
            "❌ Something went wrong while creating the giveaway. Check the bot logs "
            "for details."
        )

    context.user_data.clear()
    return ConversationHandler.END


async def create_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Giveaway creation cancelled.")
    return ConversationHandler.END


def build_announcement_text(giveaway) -> str:
    ends_at = giveaway["ends_at"].strftime("%d %b %Y, %H:%M UTC")
    return (
        f"🎉 <b>NEW GIVEAWAY!</b> 🎉\n\n"
        f"🏆 <b>{giveaway['title']}</b>\n"
        f"👑 Winners: <b>{giveaway['total_winners']}</b>\n"
        f"⏰ Voting closes: <b>{ends_at}</b>\n\n"
        "Tap 🟢 <b>Participate</b> below to enter!\n"
        "⚠️ You must be <b>Verified</b> in the bot (start it and join all our channels) "
        "for the button to work.\n\n"
        "Once you join, your own post will appear in this channel where everyone can "
        "vote for you. Voters get ONE vote each (for anyone) — no self-boosting, no bots."
    )


# =========================================================
#  Participate
# =========================================================
async def participate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    giveaway_id = query.data.split(":", 1)[1]

    giveaway = await db.get_giveaway(giveaway_id)
    if not giveaway or giveaway["status"] != "active":
        await query.answer("⏹️ This giveaway has ended.", show_alert=True)
        return

    if not await db.is_verified(user.id):
        await query.answer(
            "❌ You must Verify in the bot first! Open the bot, press /start, join all "
            "channels and tap Verify.",
            show_alert=True,
        )
        return

    if not await is_member_of_all(context.bot, user.id):
        await query.answer(
            "❌ You left one of our required channels/group. Rejoin all of them to participate.",
            show_alert=True,
        )
        return

    existing = await db.get_participant(giveaway_id, user.id)
    if existing:
        await query.answer("✅ You're already participating in this giveaway!", show_alert=True)
        return

    added = await db.add_participant(giveaway_id, user.id, user.username, user.first_name)
    if not added:
        await query.answer("✅ You're already participating in this giveaway!", show_alert=True)
        return

    await query.answer("🎉 You're in! Your entry has been posted in the channel.", show_alert=True)

    entry_text = build_participant_text(giveaway, user.id, user.first_name, votes=0)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🟢 Vote (0)", callback_data=f"vote:{giveaway_id}:{user.id}")]]
    )
    msg = await context.bot.send_message(
        chat_id=CHANNEL_CHAT_ID,
        text=entry_text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    await db.set_participant_message(giveaway_id, user.id, msg.message_id)


def build_participant_text(giveaway, user_id, name, votes) -> str:
    return (
        f"🙋 <b>New Participant!</b>\n\n"
        f"🎁 Giveaway: <b>{giveaway['title']}</b>\n"
        f"👤 Name: {mention(user_id, name)}\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"🗳️ Votes: <b>{votes}</b>\n\n"
        "Tap 🟢 Vote to support this participant! (You must have joined our channel, "
        "and you only get ONE vote per giveaway.)"
    )


# =========================================================
#  Vote
# =========================================================
async def vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    voter = query.from_user
    _, giveaway_id, candidate_id_str = query.data.split(":", 2)
    candidate_id = int(candidate_id_str)

    giveaway = await db.get_giveaway(giveaway_id)
    if not giveaway or giveaway["status"] != "active":
        await query.answer("⏹️ Voting for this giveaway has closed.", show_alert=True)
        return

    candidate = await db.get_participant(giveaway_id, candidate_id)
    if not candidate:
        await query.answer("This participant no longer exists.", show_alert=True)
        return
    if candidate.get("disqualified"):
        await query.answer("🚫 This participant was disqualified for cheating/spam.", show_alert=True)
        return

    if not await is_member_of_channel(context.bot, voter.id):
        await query.answer(
            f"❌ You must join {CHANNEL_USERNAME} before you can vote.", show_alert=True
        )
        return

    if candidate_id == voter.id:
        await query.answer("🚫 You can't vote for yourself!", show_alert=True)
        return

    already = await db.has_voted(giveaway_id, voter.id)
    if already:
        await query.answer(
            "🚫 You've already used your one vote for this giveaway!", show_alert=True
        )
        return

    added = await db.add_vote(giveaway_id, voter.id, candidate_id)
    if not added:
        await query.answer(
            "🚫 You've already used your one vote for this giveaway!", show_alert=True
        )
        return

    await db.increment_vote(giveaway_id, candidate_id)
    await query.answer("✅ Your vote has been counted. Thank you!", show_alert=True)

    updated = await db.get_participant(giveaway_id, candidate_id)
    new_votes = updated["votes"]

    # ---------- Anti-cheat: burst-vote detection ----------
    recent = await db.recent_votes_count(giveaway_id, candidate_id, SPAM_VOTE_WINDOW_SECONDS)
    if recent >= SPAM_VOTE_THRESHOLD:
        await db.disqualify_participant(
            giveaway_id,
            candidate_id,
            f"Abnormal vote spike detected ({recent} votes within {SPAM_VOTE_WINDOW_SECONDS}s) "
            "— suspected vote-bot / spam / cheating.",
        )
        try:
            await context.bot.edit_message_text(
                chat_id=CHANNEL_CHAT_ID,
                message_id=updated["message_id"],
                text=(
                    f"🚫 <b>DISQUALIFIED</b> 🚫\n\n"
                    f"👤 {mention(candidate_id, candidate['first_name'])} was removed from this "
                    "giveaway due to suspicious vote activity (spam/vote-bot detected)."
                ),
                parse_mode=ParseMode.HTML,
            )
        except (BadRequest, Forbidden):
            pass
        return

    # ---------- Update the participant's post with the new vote count ----------
    if candidate.get("message_id"):
        try:
            await context.bot.edit_message_text(
                chat_id=CHANNEL_CHAT_ID,
                message_id=candidate["message_id"],
                text=build_participant_text(
                    giveaway, candidate_id, candidate["first_name"], new_votes
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"🟢 Vote ({new_votes})", callback_data=query.data)]]
                ),
            )
        except (BadRequest, Forbidden):
            pass


# =========================================================
#  /addvote  (owner-only) - manually add/remove votes
# =========================================================
async def addvote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 This command is for the owner only.")
        return

    args = context.args
    if len(args) != 2 or not _is_signed_int(args[0]) or not args[1].isdigit():
        await update.message.reply_text(
            "Usage: <code>/addvote &lt;amount&gt; &lt;user_id&gt;</code>\n"
            "Amount may be negative to remove votes, e.g. <code>/addvote -5 123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    amount = int(args[0])
    target_user_id = int(args[1])

    if amount == 0:
        await update.message.reply_text("Amount must not be zero.")
        return

    giveaway = await db.get_active_giveaway()
    if not giveaway:
        await update.message.reply_text("There is no active giveaway right now.")
        return
    giveaway_id = str(giveaway["_id"])

    candidate = await db.get_participant(giveaway_id, target_user_id)
    if not candidate:
        await update.message.reply_text(
            "❌ That user is not a participant in the active giveaway."
        )
        return

    ok = await db.add_votes(giveaway_id, target_user_id, amount)
    if not ok:
        await update.message.reply_text("❌ Failed to update votes.")
        return

    updated = await db.get_participant(giveaway_id, target_user_id)
    new_votes = updated["votes"]

    await update.message.reply_text(
        f"✅ Adjusted votes for <code>{target_user_id}</code> by {amount:+d}. "
        f"New total: <b>{new_votes}</b>",
        parse_mode=ParseMode.HTML,
    )

    # Reflect the new count on the participant's channel post (unless disqualified,
    # in which case the post already shows the disqualified message).
    if candidate.get("message_id") and not candidate.get("disqualified"):
        try:
            await context.bot.edit_message_text(
                chat_id=CHANNEL_CHAT_ID,
                message_id=candidate["message_id"],
                text=build_participant_text(
                    giveaway, target_user_id, candidate["first_name"], new_votes
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(
                        f"🟢 Vote ({new_votes})",
                        callback_data=f"vote:{giveaway_id}:{target_user_id}",
                    )]]
                ),
            )
        except (BadRequest, Forbidden):
            pass


def _is_signed_int(s: str) -> bool:
    return s.lstrip("-").isdigit() and s not in ("", "-")


# =========================================================
#  /leaderboard
# =========================================================
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    giveaway = await db.get_active_giveaway() or await db.get_latest_giveaway()
    if not giveaway:
        await update.message.reply_text("No giveaway has been created yet.")
        return

    top = await db.top_participants(str(giveaway["_id"]), limit=10)
    if not top:
        await update.message.reply_text(
            f"🎁 <b>{giveaway['title']}</b>\n\nNo participants yet.", parse_mode=ParseMode.HTML
        )
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>Leaderboard — {giveaway['title']}</b>\n"]
    for i, p in enumerate(top):
        rank = medals[i] if i < 3 else f"{i + 1}."
        lines.append(
            f"{rank} {mention(p['user_id'], p['first_name'])} — <b>{p['votes']}</b> votes"
        )

    status = "🟢 Active" if giveaway["status"] == "active" else "🔴 Ended"
    lines.append(f"\nStatus: {status}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# =========================================================
#  /end (admin) - manually close a giveaway
# =========================================================
async def end_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 This command is for admins only.")
        return

    giveaway = await db.get_active_giveaway()
    if not giveaway:
        await update.message.reply_text("There is no active giveaway right now.")
        return

    await close_giveaway(context, str(giveaway["_id"]))
    await update.message.reply_text("✅ Giveaway closed. Final leaderboard posted in the channel.")


async def auto_end_giveaway(context: ContextTypes.DEFAULT_TYPE):
    giveaway_id = context.job.data["giveaway_id"]
    await close_giveaway(context, giveaway_id)


async def close_giveaway(context: ContextTypes.DEFAULT_TYPE, giveaway_id: str):
    giveaway = await db.get_giveaway(giveaway_id)
    if not giveaway or giveaway["status"] != "active":
        return

    await db.end_giveaway(giveaway_id)
    top = await db.top_participants(giveaway_id, limit=10)

    lines = [f"⏹️ <b>Voting closed for: {giveaway['title']}</b>\n", "📊 <b>Final Leaderboard:</b>"]
    if top:
        medals = ["🥇", "🥈", "🥉"]
        for i, p in enumerate(top):
            rank = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{rank} {mention(p['user_id'], p['first_name'])} — {p['votes']} votes")
    else:
        lines.append("No participants.")
    lines.append(
        f"\n👑 Admins will now manually select the {giveaway['total_winners']} winner(s) "
        "from the results above. The bot does not auto-pick winners."
    )

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_CHAT_ID, text="\n".join(lines), parse_mode=ParseMode.HTML
        )
    except (BadRequest, Forbidden) as e:
        logger.warning("Could not post closing message: %s", e)

    # Cancel any pending auto-close job for this giveaway (in case /end was used early)
    jobs = context.job_queue.get_jobs_by_name(f"end_{giveaway_id}") if context.job_queue else []
    for job in jobs:
        job.schedule_removal()

    # Disable the original announcement's Participate button
    if giveaway.get("channel_message_id"):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=giveaway["channel_chat_id"],
                message_id=giveaway["channel_message_id"],
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⏹️ Giveaway Closed", callback_data="closed")]]
                ),
            )
        except (BadRequest, Forbidden):
            pass


async def closed_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("⏹️ This giveaway is closed.", show_alert=True)


# =========================================================
#  /cancel fallback (outside conversation) & unknown command
# =========================================================
async def cancel_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Nothing to cancel.")


# =========================================================
#  MAIN
# =========================================================
async def post_init(application: Application):
    await db.init_indexes()
    logger.info("Bot started. Owner ID: %s | Admin IDs: %s", OWNER_ID, ADMIN_IDS)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in the environment (.env).")
    if not OWNER_ID:
        logger.warning("OWNER_ID is not set — /create and /addvote will be unusable until it is.")
    if not CHANNEL_CHAT_ID:
        logger.warning(
            "CHANNEL_CHAT_ID is not set — giveaway announcements cannot be posted until it is."
        )

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    if application.job_queue is None:
        logger.warning(
            'JobQueue is not available. Install with: pip install "python-telegram-bot[job-queue]" '
            "— giveaways will not auto-close without it (use /end manually instead)."
        )

    # /create conversation (owner only, enforced inside create_start)
    create_conv = ConversationHandler(
        entry_points=[CommandHandler("create", create_start)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_title)],
            WINNERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_winners)],
            DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_duration)],
        },
        fallbacks=[CommandHandler("cancel", create_cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(create_conv)
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("create", create_start))
    application.add_handler(CommandHandler("end", end_command))
    application.add_handler(CommandHandler("addvote", addvote_command))
    application.add_handler(CommandHandler("cancel", cancel_fallback))

    application.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^verify_check$"))
    application.add_handler(CallbackQueryHandler(participate_callback, pattern=r"^participate:"))
    application.add_handler(CallbackQueryHandler(vote_callback, pattern=r"^vote:"))
    application.add_handler(CallbackQueryHandler(closed_noop, pattern=r"^closed$"))

    logger.info("Starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()