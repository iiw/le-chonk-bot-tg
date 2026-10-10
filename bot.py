#!/usr/bin/env python3
"""Le Chonk — a Telegram bot.

• In groups: answers when @mentioned, replied to, or called by a word from WAKE_WORDS;
  the request context takes the last N–2N chat messages: the window grows from N to 2N
  and trims back to N (CONTEXT_MESSAGES=25 by default; tunable 10–50). The grow→trim
  cycle keeps Mistral's prefix cache warm.
• In DMs: answers everything; /new is a new-session marker
  (context before the command is dropped; only what follows stays in the window).
• Understands text and images (photos go to the API as image_url).
• Access: owner, allowlist (managed by the owner), anyone with a personal Mistral key
  (/setkey in DM), and strangers with a prepaid request pack bought with Telegram Stars
  (/buy; credits in data/credits.json). The owner and trusted users run on the key from
  .env; everyone else — on their own key or purchased credits (REQUIRE_USER_KEYS=true).
  A stranger without access in a group gets a short canned reply about access (no LLM;
  limits: NOACCESS_OFFER_*).

Works with any OpenAI-compatible API (Mistral by default); configuration — in .env.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    MessageEntity,
    Update,
)
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from llm import BufferedMessage, LLMError, build_llm_messages, call_llm, make_client
from settings import IMAGES_DIR, USAGE_LOG, AccessStore, ChatSettingsStore, Config, CreditsStore, load_config
from tg_format import md_to_tg_html

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
for _lg in ("httpx", "httpx2"):
    logging.getLogger(_lg).setLevel(logging.WARNING)
logger = logging.getLogger("lechonk")

TG_LIMIT = 4000        # margin below Telegram's 4096 limit
MAX_MSG_CHARS = 1500   # very long messages are clipped in the buffer
MAX_IMAGE_BYTES = 8 * 1024 * 1024

OWNER_COMMANDS = [
    "reset", "status", "setprompt", "clearprompt", "setcontext", "model", "setmodel",
    "allowed", "allow", "deny",
]

KEY_NEEDED_DM = (
    "To get answers from me, pick one:\n"
    "• your own Mistral API key — /setkey <key> (create it at admin.mistral.ai; stored on this "
    "server only, I'll try to delete the message with it)\n"
    "• or a prepaid request pack — /buy"
)
KEY_NEEDED_GROUP = "I need access first — DM me: /setkey (own Mistral key) or /buy (request packs)"
KEY_NEEDED_STRANGER = (
    "To use this bot you need access. Two options:\n"
    "• Free: your own Mistral API key — create one at admin.mistral.ai and send me:\n"
    "/setkey <API Key>\n"
    "• Or, if you'd rather skip the setup: a prepaid pack of requests for Telegram Stars — /buy."
)
# Canned reply (no LLM) for a stranger without access in a group; limits — NOACCESS_OFFER_* (settings)
NOACCESS_OFFER_GROUP = (
    "No access yet, so I can't reply here.\n"
    "DM me to set it up: /setkey <your Mistral key> (free) or /buy (prepaid packs for Telegram Stars)."
)
LAST_REQUEST_NOTE = "⚡ That was your last request — /buy to top up, or /setkey to use your own key."
NEW_SESSION_NOTE = "✅ New session started — I'll use only the messages from here on."


def terms_text(packages: list[int]) -> str:
    packs = ", ".join(f"{n}⭐ = {n} requests" for n in packages)
    return (
        "Terms of use (short):\n"
        "• Le Chonk is an AI assistant powered by a large language model — answers may be "
        "inaccurate or incomplete; don't rely on them for critical decisions.\n"
        f"• Access: your own Mistral API key (free), or prepaid request packs in Telegram Stars ({packs}). "
        "One request = one answer.\n"
        "• Sponsorship: if a member sponsors a chat (/sponsor), replies to other members there run "
        "on that sponsor's key or balance.\n"
        "• Refunds: unused packs can be refunded on request (see /paysupport); requests already used "
        "are non-refundable; credits of a refunded pack are removed from the balance.\n"
        "• The service is provided as-is, without warranties; availability may vary.\n"
        "• Support: /support"
    )


def support_text(contact: str) -> str:
    return f"Support: message {contact}." if contact else "Support: write to the bot owner."


def paysupport_text(contact: str) -> str:
    who = contact or "the bot owner"
    return (
        f"For payment issues (refunds, failed purchases) message {who} — please include the date and "
        "amount of the payment.\n"
        "Refunds are issued for unused packs; requests already used are non-refundable; credits of a refunded pack are removed from the balance."
    )


def buy_offer_text(packages: list[int]) -> str:
    packs = " · ".join(f"{n}⭐ = {n} requests" for n in packages)
    return (
        "⚡ Request packs (Telegram Stars):\n" + packs + "\n\n"
        "Pick one below — by paying you accept /terms."
    )

HELP_HEAD = (
    "I'm Le Chonk — an assistant bot in this chat.\n\n"
    "• In groups: mention me (@…) or reply to my message — I'll answer using the recent chat context.\n"
    "• In DMs: just write to me.\n"
    "• You can send pictures — I can see them.\n\n"
)
HELP_ACCESS_ALLOWED = (
    "Access is by the allowlist (managed by the owner).\n"
    "If you're allowed but I keep asking for a key — send it to me in DM:\n"
    "/setkey <your Mistral key>\n\n"
)
HELP_ACCESS_OTHER = KEY_NEEDED_STRANGER + "\n\n"
HELP_COMMANDS = (
    "Commands:\n"
    "/help — this list\n"
    "/new — start a new session (earlier messages are forgotten)\n"
    "/id — ID of this chat and yours\n"
    "/setkey — set your Mistral key (in DM)\n"
    "/delkey — delete your key\n"
    "/buy — buy a request pack (Telegram Stars)\n"
    "/balance — your request balance\n"
    "/sponsor — cover other members' replies in this chat\n"
    "/unsponsor — stop sponsoring this chat\n"
    "/terms — terms of use"
)

OWNER_HELP = (
    "Owner commands:\n"
    "/status — bot status and chat settings\n"
    "/reset — clear the memory of this chat\n"
    "/setprompt <text> — custom system prompt for the chat\n"
    "/clearprompt — restore the default prompt\n"
    "/setcontext <10–50> — context window (grows up to 2×, then trims back)\n"
    "/model — show the model\n"
    "/setmodel <id> | clear — change the model for this chat\n"
    "/allowed — access list\n"
    "/allow <user_id | @username> [mykey] — allow a user (mykey = run on my key)\n"
    "/deny <user_id | @username> — deny a user"
)

# Command menu per scope (setMyCommands): groups get only sponsorship,
# everything else lives in DMs; the owner's DM also gets the owner set.
COMMANDS_DM = [
    ("help", "Command list"),
    ("new", "Start a new session"),
    ("id", "Chat ID and your ID"),
    ("buy", "Buy a request pack (Telegram Stars)"),
    ("balance", "Your request balance"),
    ("setkey", "Set your Mistral key (in DM)"),
    ("delkey", "Delete your key"),
    ("terms", "Terms of use"),
    ("support", "Support"),
    ("paysupport", "Payment support"),
]
COMMANDS_GROUP = [
    ("sponsor", "Sponsor this chat"),
    ("unsponsor", "Stop sponsoring this chat"),
]
COMMANDS_GROUP_NAMES = {n for n, _ in COMMANDS_GROUP}  # what actually works in groups
COMMANDS_OWNER_EXTRA = [
    ("status", "Bot status (owner)"),
    ("reset", "Clear chat memory (owner)"),
    ("setprompt", "Chat system prompt (owner)"),
    ("clearprompt", "Reset prompt (owner)"),
    ("setcontext", "Context window 10–50 (owner)"),
    ("model", "Show model (owner)"),
    ("setmodel", "Change model (owner)"),
    ("allowed", "Access list (owner)"),
    ("allow", "Allow id/@nick [mykey] (owner)"),
    ("deny", "Deny id or @username (owner)"),
]

BUSY_LINES = [
    "Still thinking about the previous message — one moment…",
    "One moment — I'm finishing the previous answer…",
]

RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def entity_mentions_bot(text: str, entities, bot_username: str, bot_id: int | None) -> bool:
    """Whether the entities mention the bot (@username or a text mention)."""
    if not entities:
        return False
    for e in entities:
        etype = e.type if isinstance(e.type, str) else str(e.type)
        if etype == MessageEntity.MENTION:
            frag = text[e.offset : e.offset + e.length]
            if frag.lower() == f"@{bot_username}".lower():
                return True
        elif etype == MessageEntity.TEXT_MENTION:
            user = getattr(e, "user", None)
            if user is not None and bot_id is not None and user.id == bot_id:
                return True
    return False


def is_addressed(
    *,
    text: str | None,
    entities,
    reply_to_user_id: int | None,
    chat_type: str,
    bot_username: str,
    bot_id: int | None,
    wake_words: list[str],
) -> bool:
    """Whether the message is addressed to the bot."""
    if chat_type == ChatType.PRIVATE:
        return True
    if reply_to_user_id is not None and bot_id is not None and reply_to_user_id == bot_id:
        return True
    text = text or ""
    if entity_mentions_bot(text, entities, bot_username, bot_id):
        return True
    low = text.lower()
    for w in wake_words:
        # word-start match (tolerates Russian inflections: «Лечонку», «Лечонка»)
        if w and re.search(rf"(?<![@\w]){re.escape(w)}", low):
            return True
    return False


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Splits a long answer into chunks on line/word boundaries."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def tech_block(
    *,
    chat_kind: str,
    title: str,
    model: str,
    ctx_size: int,
    max_images: int,
) -> str:
    """Service block «# Текущий чат» — appended to the system prompt."""
    lines = [
        "# Текущий чат",
        f"Telegram-{chat_kind}: «{title}».",
        f"Ты работаешь на языковой модели «{model}» — если спросят о модели, не скрывай этого.",
        f"Ты видишь от {ctx_size} до {2 * ctx_size} последних сообщений этого чата (включая свои ответы); "
        "более ранний контекст тебе недоступен.",
    ]
    if max_images > 0:
        lines.append(
            f"Ты умеешь читать картинки: до {max_images} самых свежих фото из контекста "
            "передаются тебе как изображения, более старые отмечены пометкой «[фото]»."
        )
    else:
        lines.append("Ты умеешь читать картинки, но сейчас они тебе не передаются.")
    lines.append("Ниже — последние сообщения этого чата в формате «Имя: текст».")
    return "\n\n" + "\n".join(lines)


def time_note(now: datetime | None = None) -> dict:
    """Service note with the current time — ALWAYS the last message of the request.

    If it ends up earlier (or inside the system prompt), a changing time
    would break Mistral's prefix cache on every request.
    """
    now = now or datetime.now()
    return {
        "role": "system",
        "content": (
            "# Текущее время (служебная заметка)\n"
            f"Сегодня: {RU_WEEKDAYS[now.weekday()]}, {now.strftime('%d.%m.%Y %H:%M')}."
        ),
    }


class LeChonkBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = ChatSettingsStore()
        self.access = AccessStore(initial_allowed=cfg.allowed_users, initial_usernames=cfg.allowed_usernames)
        self.credits = CreditsStore()
        self.buffers: dict[int, deque[BufferedMessage]] = {}
        self.locks: dict[tuple[int, int], asyncio.Lock] = {}  # (chat_id, user_id) -> lock
        self.user_clients: dict[str, object] = {}  # api-key -> AsyncOpenAI
        self.client = None  # client on the .env key (the owner's)
        self.bot_id: int | None = None
        self.bot_username: str = "lechonk_bot"
        # anti-spam for the canned no-access offer in groups: RAM only
        self._offer_last: dict[tuple[int, int], float] = {}  # (user_id, chat_id) -> time.monotonic()
        self._offer_day: dict[int, tuple[str, int]] = {}     # chat_id -> (date, offer count)
        # chat session number: /new and /reset increment it; an answer started before
        # the reset is not appended to the new context (epoch check in respond)
        self.session_epochs: dict[int, int] = {}  # chat_id -> session number

    # ---------- buffers, settings, clients ----------

    def context_size(self, chat_id: int) -> int:
        n = self.store.get(chat_id, "context_size")
        return int(n) if n else self.cfg.context_messages

    def grow_size(self, chat_id: int) -> int:
        """Window growth ceiling: the buffer grows to 2N, then trims back to N."""
        return 2 * self.context_size(chat_id)

    def buffer_for(self, chat_id: int) -> deque[BufferedMessage]:
        buf = self.buffers.get(chat_id)
        if buf is None:
            buf = deque(maxlen=self.grow_size(chat_id))
            self.buffers[chat_id] = buf
        return buf

    def buffer_append(self, chat_id: int, msg: BufferedMessage) -> None:
        """Appends a message; on overflow trims the window down to the last N.

        The window grows by appending (Mistral's prefix cache reuses the prefix),
        and at the 2N ceiling it is trimmed straight to N — one cache miss per
        cycle instead of one per message.
        """
        buf = self.buffer_for(chat_id)
        if buf.maxlen and len(buf) >= buf.maxlen:
            self._trim_buffer(chat_id, buf)
        buf.append(msg)

    def _trim_buffer(self, chat_id: int, buf: deque[BufferedMessage]) -> None:
        keep_n = self.context_size(chat_id)
        old = list(buf)
        keep = old[-keep_n:]
        for m in old[:-keep_n]:
            if m.image_path:
                self._unlink_image(m.image_path)
        buf.clear()
        buf.extend(keep)

    def set_context_size(self, chat_id: int, n: int) -> int:
        n = min(max(n, 10), 50)
        self.store.set(chat_id, "context_size", n)
        old = list(self.buffers.get(chat_id, []))
        keep = old[-n:] if len(old) > 2 * n else old
        for m in old[: len(old) - len(keep)]:
            if m.image_path:
                self._unlink_image(m.image_path)
        self.buffers[chat_id] = deque(keep, maxlen=2 * n)
        return n

    def drop_buffer(self, chat_id: int) -> int:
        buf = self.buffers.pop(chat_id, None)
        n = len(buf) if buf else 0
        for m in buf or []:
            if m.image_path:
                self._unlink_image(m.image_path)
        # new session: nothing from before the reset may return to the context
        self.session_epochs[chat_id] = self.session_epochs.get(chat_id, 0) + 1
        return n

    @staticmethod
    def _unlink_image(path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    def log_usage(self, chat_id: int, model: str, usage: dict) -> None:
        """Writes per-request token counters to data/usage.log (no message texts)."""
        try:
            USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "chat_id": chat_id,
                "model": model,
                "prompt": usage.get("prompt_tokens"),
                "cached": usage.get("cached_tokens"),
                "completion": usage.get("completion_tokens"),
            }
            with USAGE_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("failed to write usage: %s", e)

    @staticmethod
    def cleanup_images_dir() -> int:
        """Deletes images left by previous runs: buffers start empty, the files aren't needed."""
        n = 0
        try:
            if IMAGES_DIR.exists():
                for f in IMAGES_DIR.iterdir():
                    if f.is_file():
                        try:
                            f.unlink()
                            n += 1
                        except Exception:
                            pass
        except Exception:
            pass
        return n

    def get_client(self, key: str):
        client = self.user_clients.get(key)
        if client is None:
            client = make_client(key, self.cfg.openai_base_url)
            self.user_clients[key] = client
        return client

    def model_for(self, chat_id: int, has_images: bool) -> str:
        model = self.store.get(chat_id, "model") or self.cfg.model
        if has_images and self.cfg.vision_model:
            model = self.cfg.vision_model
        return model

    # ---------- messages ----------

    @staticmethod
    def sender_name(user) -> str:
        if user is None:
            return "Кто-то"
        return user.full_name or (f"@{user.username}" if user.username else str(user.id))

    @staticmethod
    def extract_content(msg: Message) -> tuple[str, str | None]:
        """(text, image file_id) for any message type."""
        if msg.text:
            return msg.text, None
        if msg.photo:
            return msg.caption or "", msg.photo[-1].file_id
        if msg.sticker:
            return "[стикер]", None
        if msg.voice:
            return "[голосовое сообщение]", None
        if msg.video_note:
            return "[видео-кружок]", None
        if msg.video:
            return "[видео]", None
        if msg.animation:
            return "[гифка]", None
        if msg.audio:
            return "[аудио]", None
        if msg.document:
            mime = msg.document.mime_type or ""
            if mime.startswith("image/"):
                return msg.caption or "", msg.document.file_id
            return f"[файл: {msg.document.file_name or 'без имени'}]", None
        return "", None

    def reply_quote(self, msg: Message) -> str | None:
        """Quote of the message the trigger replies to (reply context).

        None: no reply / a reply to the bot's own message (already visible in the
        history) / nothing to quote. A reply to someone else's message is folded
        into the buffered text so the bot answers knowing what was replied to.
        """
        r = msg.reply_to_message
        if r is None:
            return None
        author = r.from_user
        if author is not None and self.bot_id is not None and author.id == self.bot_id:
            return None
        qtext, qfile = self.extract_content(r)
        qtext = (qtext or "").strip()
        if qfile:
            qtext = (qtext + " [фото]").strip()
        if not qtext:
            return None
        qtext = qtext[:MAX_MSG_CHARS]
        if author is not None:
            return f"↪ В ответ на сообщение от {self.sender_name(author)}: «{qtext}»"
        return f"↪ В ответ на сообщение: «{qtext}»"

    async def download_photo(self, bot, msg: Message, file_id: str) -> str | None:
        """Downloads an image and shrinks it; returns the path to the local file."""
        try:
            tg_file = await bot.get_file(file_id)
            path = IMAGES_DIR / f"{msg.chat_id}_{msg.message_id}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            await tg_file.download_to_drive(custom_path=str(path))
            if path.stat().st_size > MAX_IMAGE_BYTES:
                self._unlink_image(str(path))
                return None
            await asyncio.to_thread(self._shrink_image, path)
            return str(path)
        except Exception as e:
            logger.warning("failed to download the photo: %s", e)
            return None

    @staticmethod
    def _shrink_image(path: Path) -> None:
        try:
            from PIL import Image

            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((1024, 1024))
                im.save(path, "JPEG", quality=85)
        except Exception as e:
            logger.warning("failed to shrink the image: %s", e)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if msg is None or chat is None or user is None or user.is_bot:
            return

        text, image_file_id = self.extract_content(msg)
        if not text and not image_file_id:
            return

        image_path = None
        if image_file_id:
            image_path = await self.download_photo(context.bot, msg, image_file_id)

        # addressing is computed before buffering: if the bot was addressed via a reply
        # to someone else's message, that message is quoted into the text (it may not be in the buffer)
        reply_to_user = msg.reply_to_message.from_user if msg.reply_to_message else None
        addressed = is_addressed(
            text=(msg.text or msg.caption),
            entities=(msg.entities or msg.caption_entities),
            reply_to_user_id=(reply_to_user.id if reply_to_user else None),
            chat_type=chat.type,
            bot_username=self.bot_username,
            bot_id=self.bot_id,
            wake_words=self.cfg.wake_words,
        )

        buffered_text = (text or "")[:MAX_MSG_CHARS]
        if addressed:
            quote = self.reply_quote(msg)
            if quote:
                buffered_text = f"{quote}\n{buffered_text}".rstrip()

        # 1) every message goes into the chat buffer (context for future requests)
        self.buffer_append(
            chat.id,
            BufferedMessage(
                user_id=user.id,
                sender=self.sender_name(user),
                text=buffered_text,
                image_path=image_path,
            ),
        )

        # 2) react only when addressed
        if not addressed:
            return

        # 3) access: owner, allowlist, personal key (BYO-key), purchased packs (Stars)
        #    — or the chat sponsor, who covers members without access of their own
        is_owner = self.cfg.owner_id is not None and user.id == self.cfg.owner_id
        if not is_owner:
            await self.maybe_bind_wanted(context, user)
        personal_key = self.access.get_key(user.id)
        has_credits = self.credits.get(user.id) > 0
        sponsor = None  # (sponsor id, their key | None = charge their balance)
        if not is_owner and not self.access.is_allowed(user.id) and not personal_key and not has_credits:
            if chat.type != ChatType.PRIVATE:
                sponsor = self.sponsor_funding(chat.id)
            if sponsor is None:
                if chat.type == ChatType.PRIVATE:
                    # explain the access options: personal key or a Stars pack (the owner is not notified)
                    await self.safe_reply(msg, KEY_NEEDED_STRANGER)
                else:
                    # in groups — a canned reply (no LLM) with a cooldown and per-chat cap; no API spent
                    await self.maybe_offer_no_access(msg, chat, user)
                logger.info("no access: %s @%s id=%s", user.full_name, user.username, user.id)
                return

        # 4) key: personal → chat sponsor (their key, otherwise their balance) → owner's key
        #    (owner, trusted users, REQUIRE_USER_KEYS=false, and strangers with credits)
        user_key = personal_key
        uses_owner_key = False
        sponsored_by = None
        if user_key is None and sponsor is not None:
            sponsored_by, sponsor_key = sponsor
            if sponsor_key:
                user_key = sponsor_key
            else:
                user_key = self.cfg.openai_api_key
                uses_owner_key = True
            logger.info(
                "sponsored request: %s @%s id=%s (chat %s, sponsor %s)",
                user.full_name, user.username, user.id, chat.id, sponsored_by,
            )
        if user_key is None and (
            is_owner
            or not self.cfg.require_user_keys
            or self.access.is_owner_key_user(user.id, user.username)
            or has_credits
        ):
            user_key = self.cfg.openai_api_key
            uses_owner_key = True
        if not user_key:
            await self.safe_reply(msg, KEY_NEEDED_DM if chat.type == ChatType.PRIVATE else KEY_NEEDED_GROUP)
            return

        # 4b) billing: a sponsored request charges the sponsor's balance; otherwise the
        #     user's pack (not the owner, not trusted users, not free by config)
        bill_user_id = None
        if sponsored_by is not None:
            if uses_owner_key and sponsored_by != self.cfg.owner_id:
                bill_user_id = sponsored_by
        elif (
            uses_owner_key
            and not is_owner
            and not self.access.is_owner_key_user(user.id, user.username)
            and (self.cfg.require_user_keys or not self.access.is_allowed(user.id))
        ):
            bill_user_id = user.id

        # 5) one user can't stack parallel requests
        #    (different users in one chat are handled in parallel)
        lock = self.locks.setdefault((chat.id, user.id), asyncio.Lock())
        if lock.locked():
            await self.safe_reply(msg, random.choice(BUSY_LINES))
            return

        async with lock:
            await self.respond(
                context, chat, msg,
                user_key=user_key, bill_user_id=bill_user_id, sponsored_by=sponsored_by,
            )

    async def respond(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat,
        trigger_msg: Message,
        *,
        user_key: str,
        bill_user_id: int | None = None,
        sponsored_by: int | None = None,
    ) -> None:
        chat_id = chat.id
        epoch = self.session_epochs.get(chat_id, 0)  # session at request time
        buf = list(self.buffer_for(chat_id))
        has_kept_images = bool([m for m in buf if m.image_path]) and self.cfg.max_images > 0
        client = self.get_client(user_key)

        model = self.model_for(chat_id, has_kept_images)
        system_prompt = self.store.get(chat_id, "system_prompt") or self.cfg.system_prompt
        chat_kind = "личный чат" if chat.type == ChatType.PRIVATE else "групповой чат"
        system_prompt += tech_block(
            chat_kind=chat_kind,
            title=chat.title or "без названия",
            model=model,
            ctx_size=self.context_size(chat_id),
            max_images=self.cfg.max_images,
        )

        messages = build_llm_messages(
            system_prompt=system_prompt,
            buffer=buf,
            bot_id=self.bot_id,
            max_images=self.cfg.max_images,
            image_style="object",
        )
        # The current time goes as a service note at the END of the request (after the history):
        # that way a changing time doesn't break Mistral's prefix cache.
        messages.append(time_note())

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_loop(context.bot, chat_id, stop_typing))
        usage: dict = {}
        try:
            answer = await call_llm(
                client, model, messages,
                max_tokens=self.cfg.max_tokens, temperature=self.cfg.temperature,
                usage_out=usage,
            )
            ok = True
        except LLMError as e:
            logger.error("LLM error (chat %s, model %s): %s", chat_id, model, e)
            answer = "Couldn't think it through — please try again in a moment."
            ok = False
        finally:
            stop_typing.set()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        if usage:
            self.log_usage(chat_id, model, usage)

        if not answer:
            answer = "Couldn't put an answer together — please try again in a moment."

        sent_ok = False
        for i, chunk in enumerate(split_message(answer)):
            html = md_to_tg_html(chunk)
            try:
                if i == 0:
                    await trigger_msg.reply_text(html, parse_mode="HTML")
                else:
                    await context.bot.send_message(chat_id, html, parse_mode="HTML")
                sent_ok = True
            except TelegramError as e:
                # markup failed — send this chunk as plain text
                logger.warning("failed to send with markup (%s), sending it plain", e)
                try:
                    if i == 0:
                        await trigger_msg.reply_text(chunk)
                    else:
                        await context.bot.send_message(chat_id, chunk)
                    sent_ok = True
                except TelegramError as e2:
                    logger.warning("failed to send the answer: %s", e2)

        # a purchased credit is charged only for a successfully delivered answer
        if ok and sent_ok and bill_user_id is not None:
            balance = self.credits.spend(bill_user_id)
            logger.info("1 request charged from %s (%d left)", bill_user_id, balance)
            if balance == 0:
                if sponsored_by is not None:
                    # the sponsor's balance ran out — one quiet DM note (nothing posted to the chat)
                    try:
                        await context.bot.send_message(
                            sponsored_by,
                            "⚡ Your sponsored balance has run out — top up to keep covering "
                            "that chat: /buy",
                        )
                    except TelegramError:
                        pass
                else:
                    try:
                        await context.bot.send_message(chat_id, LAST_REQUEST_NOTE)
                    except TelegramError:
                        pass

        # the bot's answer also goes into the buffer so it sees its past replies;
        # but if the session was reset meanwhile (/new, /reset) it is not appended
        if ok and sent_ok and self.session_epochs.get(chat_id, 0) == epoch:
            self.buffer_append(
                chat_id,
                BufferedMessage(user_id=self.bot_id, sender="Le Chonk", text=answer, is_bot=True),
            )

    @staticmethod
    async def _typing_loop(bot, chat_id: int, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except TelegramError:
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.5)
            except asyncio.TimeoutError:
                pass

    async def safe_reply(self, msg: Message, text: str) -> None:
        try:
            await msg.reply_text(text)
        except TelegramError as e:
            logger.warning("failed to reply: %s", e)

    @staticmethod
    async def try_delete(msg: Message) -> bool:
        try:
            await msg.delete()
            return True
        except TelegramError:
            return False

    async def notify_owner(self, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        if not self.cfg.owner_id:
            return
        try:
            await context.bot.send_message(self.cfg.owner_id, text)
        except TelegramError as e:
            logger.info("failed to notify the owner: %s", e)

    async def maybe_bind_wanted(self, context: ContextTypes.DEFAULT_TYPE, user) -> None:
        """Binds an @username invite to the user's id at first contact."""
        if user.is_bot or not user.username or self.access.is_allowed(user.id):
            return
        if self.access.is_wanted_username(user.username) and self.access.bind_username(user.id, user.username):
            logger.info("invited @%s activated (id=%s)", user.username, user.id)
            extra = " — on my key" if self.access.is_owner_key_user(user.id) else ""
            await self.notify_owner(
                context,
                f"🎉 @{user.username} activated access (id {user.id} bound){extra}.",
            )

    async def maybe_offer_no_access(self, msg: Message, chat, user) -> None:
        """Canned reply for a stranger in a group (no LLM), with anti-spam limits.

        At most once per (user, chat) per NOACCESS_OFFER_COOLDOWN_HOURS, and no more
        than NOACCESS_OFFER_CHAT_DAILY_CAP messages per chat per day
        (cap 0 disables the canned replies entirely). State is RAM-only.
        """
        cap = self.cfg.noaccess_offer_chat_daily_cap
        if cap <= 0:
            return
        now = time.monotonic()
        last = self._offer_last.get((user.id, chat.id))
        if last is not None and now - last < self.cfg.noaccess_offer_cooldown_hours * 3600:
            logger.debug("canned offer suppressed (cooldown): id=%s chat %s", user.id, chat.id)
            return
        day = datetime.now().strftime("%Y-%m-%d")
        d, n = self._offer_day.get(chat.id, ("", 0))
        if d != day:
            d, n = day, 0
        if n >= cap:
            logger.debug("canned offer suppressed (cap): chat %s", chat.id)
            return
        self._offer_last[(user.id, chat.id)] = now
        self._offer_day[chat.id] = (d, n + 1)
        await self.safe_reply(msg, NOACCESS_OFFER_GROUP)
        logger.info("canned access offer sent: %s id=%s (chat %s)", user.full_name, user.id, chat.id)

    def sponsor_funding(self, chat_id: int) -> tuple[int, str | None] | None:
        """Who funds requests in this chat: (sponsor id, their key | None = from their balance).

        Source priority matches regular requests: personal key, otherwise purchased
        credits (the answer runs on the owner's key but the credit is charged to the
        sponsor). None — no sponsor, or the sponsor can't pay right now (the regular
        "no access" path with the canned offer applies).
        """
        sid = self.store.get(chat_id, "sponsor_id")
        if not sid:
            return None
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return None
        if self.cfg.owner_id is not None and sid == self.cfg.owner_id:
            return (sid, None)  # the owner pays with their own key (no charging needed)
        key = self.access.get_key(sid)
        if key:
            return (sid, key)
        if self.credits.get(sid) > 0:
            return (sid, None)
        return None

    # ---------- commands ----------

    async def on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if msg is None or chat is None or user is None or not msg.text:
            return
        parts = msg.text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lstrip("/").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        logger.info("command /%s from %s in chat %s", cmd, user.id, chat.id)
        await self.maybe_bind_wanted(context, user)
        is_owner = self.cfg.owner_id is not None and user.id == self.cfg.owner_id

        # in groups commands don't work: only sponsorship ones (plus owner commands, for the owner).
        # silently ignored, so the group gets no replies to command calls
        if chat.type != ChatType.PRIVATE:
            if cmd == "setkey":
                # a key may have been sent to the group by mistake — try to delete it, stay silent
                deleted = await self.try_delete(msg)
                logger.info(
                    "key in a group (id=%s): command ignored, message %s",
                    user.id, "deleted" if deleted else "failed to delete",
                )
                return
            if cmd not in COMMANDS_GROUP_NAMES and not (is_owner and cmd in OWNER_COMMANDS):
                return

        if cmd in ("start", "help"):
            in_list = is_owner or self.access.is_allowed(user.id)
            text = HELP_HEAD + (HELP_ACCESS_ALLOWED if in_list else HELP_ACCESS_OTHER) + HELP_COMMANDS
            if is_owner:
                text += "\n\n" + OWNER_HELP
            await self.safe_reply(msg, text)
            return

        if cmd == "id":
            await self.safe_reply(msg, f"🆔 Chat: {chat.id}\nYou: {user.id}")
            return

        if cmd == "setkey":
            if chat.type != ChatType.PRIVATE:
                await self.safe_reply(msg, "I only accept keys in DM — message me there: /setkey <key>")
                return
            if not arg or len(arg) < 12:
                await self.safe_reply(msg, "Usage: /setkey <Mistral API key>")
                return
            self.access.set_key(user.id, arg)
            deleted = await self.try_delete(msg)
            note = "I deleted the message with the key." if deleted else "I recommend deleting the message with the key."
            await self.safe_reply(msg, f"✅ Key saved — your requests will now run on it. {note}")
            return

        if cmd == "delkey":
            self.access.remove_key(user.id)
            await self.safe_reply(msg, "✅ Your key has been deleted.")
            return

        if cmd == "buy":
            if chat.type != ChatType.PRIVATE:
                await self.safe_reply(msg, "Pack prices are in Telegram Stars — message me in DM to buy: /buy")
                return
            if arg.isdigit() and int(arg) in self.cfg.star_packages:
                await self.send_pack_invoice(context, chat.id, user.id, int(arg))
                return
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{n} requests — {n}⭐", callback_data=f"buy:{n}")]
                for n in self.cfg.star_packages
            ])
            try:
                await context.bot.send_message(chat.id, buy_offer_text(self.cfg.star_packages), reply_markup=keyboard)
            except TelegramError as e:
                logger.warning("failed to send the pack list: %s", e)
            return

        if cmd == "balance":
            n = self.credits.get(user.id)
            text = f"⚡ Your request balance: {n}."
            if n <= 0:
                text += " Top up: /buy — or use your own key: /setkey."
            await self.safe_reply(msg, text)
            return

        if cmd == "terms":
            await self.safe_reply(msg, terms_text(self.cfg.star_packages))
            return

        if cmd in ("support", "paysupport"):
            text = paysupport_text(self.cfg.support_contact) if cmd == "paysupport" else support_text(self.cfg.support_contact)
            await self.safe_reply(msg, text)
            return

        if cmd == "sponsor":
            if chat.type == ChatType.PRIVATE:
                await self.safe_reply(msg, "Use /sponsor in the group you want to sponsor.")
                return
            can_fund = (
                is_owner
                or bool(self.access.get_key(user.id))
                or self.credits.get(user.id) > 0
            )
            if not can_fund:
                await self.safe_reply(
                    msg,
                    "To sponsor a chat you need your own Mistral key (send it in DM: /setkey) "
                    "or a request pack (/buy).",
                )
                return
            current = self.store.get(chat.id, "sponsor_id")
            try:
                cur_id = int(current) if current else None
            except (TypeError, ValueError):
                cur_id = None
            if cur_id is not None and cur_id == user.id:
                await self.safe_reply(msg, "✅ You're already sponsoring this chat. /unsponsor to stop.")
                return
            if cur_id is not None and cur_id != user.id and not is_owner:
                await self.safe_reply(msg, "This chat is already sponsored.")
                return
            self.store.set(chat.id, "sponsor_id", user.id)
            await self.safe_reply(
                msg,
                "✅ This chat is now sponsored — replies to other members here run on your "
                "key/balance. /unsponsor to stop.",
            )
            return

        if cmd == "unsponsor":
            if chat.type == ChatType.PRIVATE:
                await self.safe_reply(msg, "Use /unsponsor in the group whose sponsorship you want to remove.")
                return
            current = self.store.get(chat.id, "sponsor_id")
            try:
                cur_id = int(current) if current else None
            except (TypeError, ValueError):
                cur_id = None
            if cur_id is None:
                await self.safe_reply(msg, "This chat isn't sponsored.")
                return
            allowed = cur_id == user.id or is_owner
            if not allowed:
                # a chat administrator can also remove the sponsorship
                try:
                    member = await context.bot.get_chat_member(chat.id, user.id)
                    allowed = member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
                except TelegramError as e:
                    logger.info("failed to check the member status in chat %s: %s", chat.id, e)
            if not allowed:
                await self.safe_reply(
                    msg,
                    "Only the sponsor, a chat administrator, or the bot owner can remove sponsorship.",
                )
                return
            self.store.set(chat.id, "sponsor_id", None)
            logger.info("sponsorship removed: chat %s, it was sponsor %s, removed by %s", chat.id, cur_id, user.id)
            await self.safe_reply(msg, "✅ Sponsorship removed — this chat is no longer sponsored.")
            return

        if cmd == "new":
            # /new works in DMs only: a new-session marker; all context before the command is dropped
            if chat.type != ChatType.PRIVATE:
                return
            dropped = self.drop_buffer(chat.id)
            logger.info("new session: chat %s, %d messages dropped", chat.id, dropped)
            await self.safe_reply(msg, NEW_SESSION_NOTE)
            return

        if cmd not in OWNER_COMMANDS:
            await self.safe_reply(msg, "Unknown command. Try /help")
            return

        # everything below is owner-only
        if not is_owner:
            await self.safe_reply(msg, "Only the bot owner can use this command.")
            return

        if cmd == "reset":
            n = self.drop_buffer(chat.id)
            await self.safe_reply(msg, f"🧹 Chat memory cleared ({n} messages removed).")
        elif cmd == "status":
            buf = self.buffer_for(chat.id)
            img_count = sum(1 for m in buf if m.image_path)
            own_model = self.store.get(chat.id, "model")
            n_allowed = len(self.access.allowed)
            n_ctx = self.context_size(chat.id)
            lines = [
                "Le Chonk • status",
                f"Chat: {chat.id} ({chat.type})",
                f"Messages in context: {len(buf)} (window {n_ctx}–{2 * n_ctx})",
                f"Images in context: {img_count} (limit {self.cfg.max_images}/request)",
                f"Model: {own_model or self.cfg.model}" + (" (custom for this chat)" if own_model else ""),
                f"Vision model: {self.cfg.vision_model or '— (main model is used)'}",
                f"Prompt: {'custom for this chat' if self.store.get(chat.id, 'system_prompt') else 'default'}",
                f"Sponsor: {self.store.get(chat.id, 'sponsor_id') or '—'}",
                f"Access: {n_allowed} allowed (besides the owner); personal key required: "
                + ("yes" if self.cfg.require_user_keys else "no"),
            ]
            await self.safe_reply(msg, "\n".join(lines))
        elif cmd == "setprompt":
            if not arg:
                await self.safe_reply(msg, "Usage: /setprompt <prompt text>")
            else:
                self.store.set(chat.id, "system_prompt", arg)
                await self.safe_reply(msg, "✅ System prompt for this chat updated.")
        elif cmd == "clearprompt":
            self.store.set(chat.id, "system_prompt", None)
            await self.safe_reply(msg, "✅ Prompt reset to default.")
        elif cmd == "setcontext":
            if not arg.isdigit():
                await self.safe_reply(msg, "Usage: /setcontext <10–50>")
            else:
                n = self.set_context_size(chat.id, int(arg))
                await self.safe_reply(msg, f"✅ Context window: {n}–{2 * n} messages.")
        elif cmd == "model":
            cur = self.store.get(chat.id, "model") or self.cfg.model
            await self.safe_reply(
                msg,
                f"Current model: {cur}\nMain: {self.cfg.model}\nVision: {self.cfg.vision_model or '—'}",
            )
        elif cmd == "setmodel":
            if arg.lower() == "clear":
                self.store.set(chat.id, "model", None)
                await self.safe_reply(msg, f"✅ Reverted to the main model: {self.cfg.model}")
            elif arg:
                self.store.set(chat.id, "model", arg)
                await self.safe_reply(msg, f"✅ Model for this chat: {arg}")
            else:
                await self.safe_reply(msg, "Usage: /setmodel <model-id> | /setmodel clear")
        elif cmd == "allowed":
            lines = ["👥 Access:"]
            if self.cfg.owner_id:
                lines.append(f"• {self.cfg.owner_id} — owner")
            for uid in sorted(self.access.allowed):
                if uid == self.cfg.owner_id:
                    continue
                marks = ["personal key" if self.access.get_key(uid) else "no personal key"]
                if self.access.is_owner_key_user(uid):
                    marks.append("on owner's key")
                lines.append(f"• {uid} — {', '.join(marks)}")
            for un in sorted(self.access.wanted):
                mark = " — on owner's key" if self.access.is_owner_key_user(None, un) else ""
                lines.append(f"• @{un} — invited, awaiting first contact{mark}")
            if len(lines) == 1:
                lines.append("(nobody besides the owner yet)")
            lines.append(f"Personal key for others: {'required' if self.cfg.require_user_keys else 'not required'}")
            await self.safe_reply(msg, "\n".join(lines))
        elif cmd == "allow":
            parts = arg.split()
            target = parts[0].lstrip("@") if parts else ""
            mykey = len(parts) > 1 and parts[1].lower() in ("mykey", "мой", "owner")
            if target.isdigit():
                self.access.allow(int(target))
                if mykey:
                    self.access.trust_id(int(target))
                note = " (on my key)" if mykey else ""
                await self.safe_reply(msg, f"✅ User {target} added to the allowlist{note}.")
            elif re.fullmatch(r"[A-Za-z0-9_]{4,32}", target):
                un = self.access.want_username(target)
                if mykey:
                    self.access.trust_name(un)
                note = " — on my key" if mykey else ""
                await self.safe_reply(
                    msg,
                    f"✅ @{un} added to the invite list{note} — access will turn on as soon as they message me "
                    "(or mention me in a group).",
                )
            else:
                await self.safe_reply(
                    msg,
                    "Usage: /allow <user_id | @username> [mykey]\n"
                    "mykey — allow them to run on the owner's key.\n"
                    "You can find the user ID by asking them to send me /id",
                )
        elif cmd == "deny":
            target = arg.lstrip("@")
            if target.isdigit():
                self.access.deny(int(target))
                await self.safe_reply(msg, f"✅ User {target} removed from the allowlist (and their key).")
            elif target:
                self.access.unwant_username(target)
                await self.safe_reply(msg, f"✅ @{target.lower()} removed from invites. If they're already activated by id — use /deny <id>.")
            else:
                await self.safe_reply(msg, "Usage: /deny <user_id | @username>")

    # ---------- Telegram Stars packs ----------

    async def send_pack_invoice(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, n: int) -> None:
        """Sends an XTR invoice for a pack of n requests."""
        try:
            await context.bot.send_invoice(
                chat_id=chat_id,
                title=f"{n} requests",
                description=(
                    f"Prepaid pack: {n} requests to Le Chonk (runs on the owner's API key). "
                    "One request = one answer. Unused packs are refundable via /paysupport; "
                    "by paying you accept /terms."
                ),
                payload=f"buy:{n}:{user_id}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=f"{n} requests", amount=n)],
                # non-empty start_parameter: a forwarded invoice can't be paid by a second person (single-chat invoice)
                start_parameter="lechonk_pack",
            )
            logger.info("invoice %s⭐ created for %s", n, user_id)
        except TelegramError as e:
            logger.warning("failed to create the invoice: %s", e)
            try:
                await context.bot.send_message(chat_id, "Couldn't create the invoice — please try again in a moment.")
            except TelegramError:
                pass

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Buttons under /buy: create the invoice for the chosen pack."""
        query = update.callback_query
        if query is None or not query.data:
            return
        try:
            await query.answer()
        except TelegramError:
            pass
        m = re.fullmatch(r"buy:(\d+)", query.data)
        if not m or query.from_user is None:
            return
        n = int(m.group(1))
        if n not in self.cfg.star_packages:
            return
        chat_id = query.message.chat.id if query.message else query.from_user.id
        await self.send_pack_invoice(context, chat_id, query.from_user.id, n)

    async def on_precheckout(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mandatory pre_checkout_query confirmation (10 seconds to answer)."""
        q = update.pre_checkout_query
        if q is None:
            return
        ok = q.currency == "XTR" and (q.invoice_payload or "").startswith("buy:")
        try:
            if ok:
                await q.answer(ok=True)
            else:
                logger.warning("pre_checkout rejected: payload=%s", q.invoice_payload)
                await q.answer(ok=False, error_message="This invoice is no longer available.")
        except TelegramError as e:
            logger.warning("failed to answer pre_checkout: %s", e)

    async def on_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Payment succeeded: credit the pack (1⭐ = 1 request) and confirm."""
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None or msg.successful_payment is None:
            return
        sp = msg.successful_payment
        if sp.currency != "XTR" or not (sp.invoice_payload or "").startswith("buy:"):
            logger.info("unexpected successful_payment: payload=%s, currency=%s", sp.invoice_payload, sp.currency)
            return
        charge_id = sp.telegram_payment_charge_id or ""
        if self.credits.is_known_charge(charge_id):
            logger.info("duplicate charge_id %s — skipping", charge_id)
            return
        credits = max(int(sp.total_amount or 0), 0)
        balance = self.credits.add(user.id, credits, credits, charge_id)
        logger.info("payment: +%d requests for %s (charge %s, balance %d)", credits, user.id, charge_id, balance)
        try:
            await context.bot.send_message(
                user.id,
                f"✅ Payment received: {sp.total_amount}⭐ → +{credits} requests.\n"
                f"Your balance: {balance} requests. Just send me a message!",
            )
        except TelegramError as e:
            logger.warning("failed to confirm the payment: %s", e)

    # ---------- startup ----------

    async def post_init(self, app: Application) -> None:
        n_imgs = self.cleanup_images_dir()
        if n_imgs:
            logger.info("cleaned %d leftover images from previous runs", n_imgs)
        me = await app.bot.get_me()
        self.bot_id = me.id
        self.bot_username = me.username or self.bot_username
        self.client = self.get_client(self.cfg.openai_api_key)
        def _cmds(items):
            return [BotCommand(n, d) for n, d in items]

        # Scoped menus: groups get only sponsorship; DMs the rest of the commands;
        # the owner's DM additionally the owner set. Default — the public set.
        await app.bot.set_my_commands(_cmds(COMMANDS_DM), scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(_cmds(COMMANDS_DM), scope=BotCommandScopeAllPrivateChats())
        if self.cfg.owner_id:
            try:
                await app.bot.set_my_commands(
                    _cmds(COMMANDS_DM + COMMANDS_OWNER_EXTRA),
                    scope=BotCommandScopeChat(chat_id=self.cfg.owner_id),
                )
            except TelegramError as e:
                logger.warning("failed to set the owner-DM menu: %s", e)
        await app.bot.set_my_commands(_cmds(COMMANDS_GROUP), scope=BotCommandScopeAllGroupChats())
        can_read_all = getattr(me, "can_read_all_group_messages", None)
        if can_read_all is False:
            logger.warning(
                "⚠️ Privacy mode is ON: in groups the bot sees only mentions and commands, "
                "not the full context. Disable it: @BotFather → /setprivacy → Disable, "
                "or make the bot a group admin."
            )
        n_allowed = len(self.access.allowed)
        logger.info(
            "Le Chonk started: @%s (id=%s), model %s, allowed besides the owner: %d",
            self.bot_username, self.bot_id, self.cfg.model, n_allowed,
        )
        print(f"✅ Le Chonk started: @{self.bot_username} (id={self.bot_id}), model {self.cfg.model}")


def main() -> None:
    cfg = load_config()
    if not cfg.telegram_token:
        raise SystemExit("❌ TELEGRAM_BOT_TOKEN is not set — put the token from @BotFather into .env")
    if not cfg.openai_api_key:
        raise SystemExit("❌ OPENAI_API_KEY is not set in .env")

    bot = LeChonkBot(cfg)
    app = (
        ApplicationBuilder()
        .token(cfg.telegram_token)
        .concurrent_updates(16)
        .post_init(bot.post_init)
        .build()
    )
    app.add_handler(CommandHandler(
        ["start", "help", "id", "new", "buy", "balance", "sponsor", "unsponsor", "terms", "support", "paysupport", "setkey", "delkey"]
        + OWNER_COMMANDS,
        bot.on_command,
    ))
    app.add_handler(CallbackQueryHandler(bot.on_callback, pattern=r"^buy:"))
    app.add_handler(PreCheckoutQueryHandler(bot.on_precheckout))
    # successful payment — a separate MessageHandler BEFORE the general one (otherwise on_message grabs it)
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, bot.on_payment))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, bot.on_message))
    logger.info("Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
