#!/usr/bin/env python3
"""Le Chonk bot checks without Telegram polling:

1) config; 2) addressing logic; 3) AccessStore; 4) context assembly;
5) live API calls (text + image); 6) a dry run of synthetic Telegram
updates through the REAL bot handler with a fake bot object.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram import Update

import bot as bot_mod
import settings as settings_mod
from bot import LeChonkBot, is_addressed, tech_block, time_note
from tg_format import md_to_tg_html
from llm import BufferedMessage, _extract_text, build_llm_messages, call_llm, image_to_data_uri, make_client
from settings import AccessStore, ChatSettingsStore, CreditsStore, load_config

# Sandbox every default store path and the images dir: the test must never touch data/.
# (With ENCRYPTION_KEY set, opening a real plaintext file would already migrate it,
# and the photo fixtures used to leak an avatar copy into data/images/.)
# Distinct filenames keep the bots' default stores from colliding with section 3's files.
tmpdir = Path(tempfile.mkdtemp(prefix="lechonk_test_"))
settings_mod.ACCESS_FILE = tmpdir / "default_access.json"
settings_mod.CHAT_SETTINGS_FILE = tmpdir / "default_settings.json"
settings_mod.CREDITS_FILE = tmpdir / "default_credits.json"
bot_mod.IMAGES_DIR = tmpdir / "images"

AVATAR = Path(__file__).resolve().parent.parent / "assets" / "lechonk_avatar.png"
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))


# ── 1. config ─────────────────────────────────────────────────────────
print("== 1. config ==")
cfg = load_config()
check("TELEGRAM_BOT_TOKEN is set", bool(cfg.telegram_token))
check("OPENAI_API_KEY is set", bool(cfg.openai_api_key))
check("MODEL is set", bool(cfg.model), cfg.model)
check("OWNER_ID is set", cfg.owner_id is not None, str(cfg.owner_id))
check("base_url", bool(cfg.openai_base_url), cfg.openai_base_url)
check("personal key required from non-owners", cfg.require_user_keys)
check("NOACCESS_OFFER: canned-offer limits (cooldown/cap)",
      cfg.noaccess_offer_cooldown_hours >= 0 and cfg.noaccess_offer_chat_daily_cap >= 0,
      f"{cfg.noaccess_offer_cooldown_hours}h / {cfg.noaccess_offer_chat_daily_cap} per day")

# ── 1b. command menu by scope ────────────────────────────────────────
print("== 1b. command menu ==")
_gset = {n for n, _ in bot_mod.COMMANDS_GROUP}
_dset = {n for n, _ in bot_mod.COMMANDS_DM}
_oset = {n for n, _ in bot_mod.COMMANDS_DM + bot_mod.COMMANDS_OWNER_EXTRA}
check("menu: groups get only /sponsor and /unsponsor", _gset == {"sponsor", "unsponsor"}, str(sorted(_gset)))
check("menu: group and DM commands don't overlap", not (_gset & _dset), str(sorted(_gset & _dset)))
check("DM menu: the public set is in place",
      {"help", "id", "new", "buy", "balance", "setkey", "delkey", "terms", "support", "paysupport"} <= _dset,
      str(sorted(_dset)))
check("menu: owner commands only in the owner set",
      {"status", "reset", "setprompt", "allow", "deny"} <= _oset
      and not (_dset & {"status", "reset", "setprompt", "allow", "deny"}))

# ── 2. addressing logic ───────────────────────────────────────────────
print("== 2. addressing logic ==")
kw = dict(bot_username="lechonk_bot", bot_id=1000, wake_words=["лечонк", "lechonk"])
mention = [SimpleNamespace(type="mention", offset=0, length=len("@lechonk_bot"))]
check("DM — always addressed", is_addressed(text="привет", entities=None, reply_to_user_id=None, chat_type="private", **kw))
check("@mention in a group", is_addressed(text="@lechonk_bot привет", entities=mention, reply_to_user_id=None, chat_type="supergroup", **kw))
check("reply to the bot's message", is_addressed(text="а?", entities=None, reply_to_user_id=1000, chat_type="supergroup", **kw))
check("wake word («Лечонк, ...»)", is_addressed(text="Лечонк, как дела?", entities=None, reply_to_user_id=None, chat_type="supergroup", **kw))
check("wake word in an inflected form («Лечонку»)", is_addressed(text="Лечонку привет", entities=None, reply_to_user_id=None, chat_type="supergroup", **kw))
check("plain chatter — stays silent", not is_addressed(text="всем привет, что нового", entities=None, reply_to_user_id=None, chat_type="supergroup", **kw))
check("someone else's @mention — stays silent", not is_addressed(text="@vasya привет", entities=mention, reply_to_user_id=555, chat_type="supergroup", **kw))

# ── 2b. answer formatting (md → HTML for Telegram) ────────────────
print("== 2b. formatting ==")
check("md→HTML: bold", md_to_tg_html("**важно**") == "<b>важно</b>")
check("md→HTML: italic", md_to_tg_html("*наклон*") == "<i>наклон</i>")
check("md→HTML: inline code", md_to_tg_html("`a < b`") == "<code>a &lt; b</code>")
check("md→HTML: strikethrough", md_to_tg_html("~~нет~~") == "<s>нет</s>")
check("md→HTML: link", md_to_tg_html("[текст](https://a.b/?x=1&y=2)") == '<a href="https://a.b/?x=1&amp;y=2">текст</a>')
check("md→HTML: heading", md_to_tg_html("# Заголовок") == "<b>Заголовок</b>")
check("md→HTML: escaping", md_to_tg_html("1 < 2 & 3 > 2") == "1 &lt; 2 &amp; 3 &gt; 2")
_h = md_to_tg_html("```python\nprint('<hi>')\n```")
check("md→HTML: code block", _h.startswith('<pre><code class="language-python">') and "&lt;hi&gt;" in _h and _h.endswith("</code></pre>"), _h)
check("md→HTML: mixed", md_to_tg_html("**a** и *b* и `c`") == "<b>a</b> и <i>b</i> и <code>c</code>")

# ── 2c. prompt and tech block ──────────────────────────────────────────────
print("== 2c. prompt and tech block ==")
check("prompt: the name «Le Chonk»", "Ты — Le Chonk" in cfg.system_prompt)
check("prompt: model nickname", "неофициальное название модели Mistral" in cfg.system_prompt)
check("prompt: permissions block (tone/consultations/adult humor)", "Тебе разрешено" in cfg.system_prompt and "подбадривающе" in cfg.system_prompt)
_tb = tech_block(chat_kind="групповой чат", title="Тест", model="mistral-large-4", ctx_size=25, max_images=4)
check("tech block: window visibility 25–50", "от 25 до 50 последних сообщений" in _tb)
check("tech block: images go in as real images", "как изображения" in _tb and "[фото]" in _tb)
_tb0 = tech_block(chat_kind="личный чат", title="без названия", model="m", ctx_size=10, max_images=0)
check("tech block: images disabled", "не передаются" in _tb0)

# ── 2d. reply quote (reply_quote) ──────────────────────────────────
print("== 2d. reply quote ==")
q_bot = LeChonkBot(cfg)
q_bot.bot_id = 1000
_q_vasya = SimpleNamespace(id=555, full_name="Вася", username=None)
_q_alex = SimpleNamespace(id=77, full_name="Alex", username=None)
_q_botu = SimpleNamespace(id=1000, full_name="Le Chonk", username="lechonk_bot")


def _stub_msg(*, text=None, caption=None, photo: bool = False, from_user=None, reply_to=None):
    return SimpleNamespace(
        text=text, caption=caption,
        photo=[SimpleNamespace(file_id="F")] if photo else None,
        sticker=None, voice=None, video_note=None, video=None, animation=None, audio=None,
        document=None, from_user=from_user, reply_to_message=reply_to,
    )


_q1 = q_bot.reply_quote(_stub_msg(
    text="@lechonk_bot нам бояться?", from_user=_q_vasya,
    reply_to=_stub_msg(text="В 2026 обещают конец света", from_user=_q_alex),
))
check("reply quote: someone else's message", bool(_q1) and "В ответ на сообщение от Alex" in _q1 and "конец света" in _q1, repr(_q1))
check("reply quote: replying to the bot — no quote",
      q_bot.reply_quote(_stub_msg(text="а?", from_user=_q_vasya, reply_to=_stub_msg(text="мой ответ", from_user=_q_botu))) is None)
_q2 = q_bot.reply_quote(_stub_msg(
    text="что тут?", from_user=_q_vasya,
    reply_to=_stub_msg(photo=True, caption="смотри", from_user=_q_alex),
))
check("reply quote: photo with a caption", bool(_q2) and "смотри [фото]" in _q2, repr(_q2))
check("reply quote: no reply — none", q_bot.reply_quote(_stub_msg(text="а?")) is None)

# ── 3. AccessStore ────────────────────────────────────────────────────
print("== 3. AccessStore ==")
a1 = AccessStore(path=tmpdir / "access.json", initial_allowed=[111])
check("seed from initial_allowed", a1.is_allowed(111) and not a1.is_allowed(222))
a1.allow(222)
a1.set_key(111, "mstrl_testkey123")
a2 = AccessStore(path=tmpdir / "access.json")
check("persisted to disk", a2.is_allowed(222) and a2.get_key(111) == "mstrl_testkey123")
a2.deny(111)
check("deny drops the key", not a2.is_allowed(111) and a2.get_key(111) is None)
a3 = AccessStore(path=tmpdir / "access.json")
check("deny persisted", not a3.is_allowed(111))
a3.want_username("@Lena_Test")
a4 = AccessStore(path=tmpdir / "access.json")
check("@username invite persisted", a4.is_wanted_username("lena_test"))
check("bind on first contact", a4.bind_username(777, "LENA_TEST") and a4.is_allowed(777) and not a4.is_wanted_username("lena_test"))
a5 = AccessStore(path=tmpdir / "access.json")
check("bind persisted", a5.is_allowed(777) and not a5.is_wanted_username("lena_test"))
a5.trust_name("@Kate_Test")
a5.want_username("kate_test")
a6 = AccessStore(path=tmpdir / "access.json")
check("@username trust persisted", a6.is_owner_key_user(None, "kate_test"))
check("bind moves trust to the id", a6.bind_username(999, "kate_test") and a6.is_owner_key_user(999) and not a6.is_owner_key_user(None, "kate_test"))
a7 = AccessStore(path=tmpdir / "access.json")
check("id trust persisted", a7.is_owner_key_user(999))
a7.trust_id(8888)
a8 = AccessStore(path=tmpdir / "access.json")
check("trust_id persisted", a8.is_owner_key_user(8888))

# ── 3b. access store: AES-256-GCM encryption at rest ──────────────────
print("== 3b. access store encryption ==")
_enc_backup = os.environ.get("ENCRYPTION_KEY")
os.environ["ENCRYPTION_KEY"] = "ab" * 32  # throwaway key for the sandbox (32 bytes of hex)
enc_file = tmpdir / "enc_access.json"
e1 = AccessStore(path=enc_file)
e1.allow(4321)
e1.set_key(4321, "mstrl_enc_testkey")
_raw = enc_file.read_text("utf-8")
check("encrypted store: the file is an AES-256-GCM envelope", "_encrypted" in _raw and "AES-256-GCM" in _raw)
check("encrypted store: no plaintext keys on disk", "mstrl_enc_testkey" not in _raw)
e2 = AccessStore(path=enc_file)
check("encrypted store: round-trips through decryption", e2.is_allowed(4321) and e2.get_key(4321) == "mstrl_enc_testkey")
legacy_file = tmpdir / "legacy_access.json"
legacy_file.write_text(json.dumps({"allowed": [555], "keys": {"555": "mstrl_legacykey"}}), "utf-8")
e3 = AccessStore(path=legacy_file)
check("legacy plaintext file: still loads", e3.is_allowed(555) and e3.get_key(555) == "mstrl_legacykey")
_lraw = legacy_file.read_text("utf-8")
check("legacy plaintext file: auto-migrates to encrypted", "_encrypted" in _lraw and "mstrl_legacykey" not in _lraw)
os.environ["ENCRYPTION_KEY"] = "cd" * 32  # a different key
try:
    AccessStore(path=enc_file)
    check("wrong key: raises instead of touching the file", False, "no exception")
except RuntimeError:
    check("wrong key: raises instead of touching the file", True)
os.environ.pop("ENCRYPTION_KEY", None)
try:
    AccessStore(path=enc_file)
    check("no key + encrypted file: raises", False, "no exception")
except RuntimeError:
    check("no key + encrypted file: raises", True)
pt_file = tmpdir / "plain_access.json"
e5 = AccessStore(path=pt_file)
e5.allow(66)
_ptraw = pt_file.read_text("utf-8")
try:
    _pt_ok = "_encrypted" not in _ptraw and json.loads(_ptraw).get("allowed") == [66]
except Exception:
    _pt_ok = False
check("no key: a fresh file stays plaintext (backward compatible)", _pt_ok)
os.environ["ENCRYPTION_KEY"] = "ab" * 32  # back to the sandbox key
e4 = AccessStore(path=enc_file)
check("rejected attempts left the file intact", e4.is_allowed(4321) and e4.get_key(4321) == "mstrl_enc_testkey")
if _enc_backup is None:
    os.environ.pop("ENCRYPTION_KEY", None)
else:
    os.environ["ENCRYPTION_KEY"] = _enc_backup

# ── 4. context assembly ───────────────────────────────────────────────
print("== 4. context assembly ==")
buf = [
    BufferedMessage(user_id=1, sender="Вася", text="привет"),
    BufferedMessage(user_id=2, sender="Петя", text="как дела"),
    BufferedMessage(user_id=1000, sender="Le Chonk", text="ответ", is_bot=True),
]
if AVATAR.exists():
    buf.append(BufferedMessage(user_id=1, sender="Вася", text="смотри кто пришёл", image_path=str(AVATAR)))
msgs = build_llm_messages("SYS", buf, bot_id=1000, max_images=4)
check("first — system", msgs[0]["role"] == "system")
check("participant lines merged", msgs[1]["role"] == "user" and "Вася: привет" in msgs[1]["content"] and "Петя: как дела" in msgs[1]["content"])
check("the bot's line — assistant", msgs[2]["role"] == "assistant")
roles = [m["role"] for m in msgs]
check("role sequence", roles == (["system", "user", "assistant", "user"] if AVATAR.exists() else ["system", "user", "assistant"]), str(roles))
if AVATAR.exists():
    img_msg = msgs[3]
    has_img = isinstance(img_msg["content"], list) and img_msg["content"][1]["image_url"]["url"].startswith("data:image/")
    check("image — data:URI", has_img)
print("   example:", json.dumps(msgs, ensure_ascii=False)[:220], "…")

# ── 4b. text extraction from content (thinking blocks) ────────────────
print("== 4b. text extraction ==")
check("content string", _extract_text("привет") == "привет")
check("thinking+text list", _extract_text([{"type": "thinking", "thinking": [{"type": "text", "text": "скрыто"}]}, {"type": "text", "text": "ответ"}]) == "ответ")
check("None → empty", _extract_text(None) == "")

# ── 4c. context window: grow to 2N → trim to N ──────────────────────
print("== 4c. context window (grow/trim) ==")
w_bot = LeChonkBot(cfg)
w_bot.store = ChatSettingsStore(path=tmpdir / "win_settings.json")
w_bot.access = AccessStore(path=tmpdir / "win_access.json")
w_bot.credits = CreditsStore(path=tmpdir / "win_credits.json")
W_CHAT = 555000111
check("setcontext: window 10–20", w_bot.set_context_size(W_CHAT, 10) == 10 and w_bot.grow_size(W_CHAT) == 20)
for i in range(20):
    w_bot.buffer_append(W_CHAT, BufferedMessage(user_id=1, sender="Вася", text=f"м{i}"))
check("window growth: 20 messages buffered", len(w_bot.buffer_for(W_CHAT)) == 20)
w_bot.buffer_append(W_CHAT, BufferedMessage(user_id=1, sender="Вася", text="м20"))
_buf_w = w_bot.buffer_for(W_CHAT)
check(
    "overflow: trims to the last 10 (+the new one)",
    len(_buf_w) == 11 and _buf_w[0].text == "м10" and _buf_w[-1].text == "м20",
    f"len={len(_buf_w)}",
)
w_bot.drop_buffer(W_CHAT)
_img_old = tmpdir / "old_photo.jpg"
_img_new = tmpdir / "new_photo.jpg"
_img_old.write_bytes(b"x")
_img_new.write_bytes(b"x")
for i in range(20):
    w_bot.buffer_append(W_CHAT, BufferedMessage(user_id=1, sender="Вася", text=f"z{i}", image_path=str(_img_old) if i == 0 else None))
check("an image inside the window is still on disk", _img_old.exists())
w_bot.buffer_append(W_CHAT, BufferedMessage(user_id=1, sender="Вася", text="z20", image_path=str(_img_new)))
check("the trim deletes the image of the dropped message", (not _img_old.exists()) and _img_new.exists())
w_bot.set_context_size(W_CHAT, 25)
for i in range(60):
    w_bot.buffer_append(W_CHAT, BufferedMessage(user_id=1, sender="Вася", text=f"r{i}"))
w_bot.set_context_size(W_CHAT, 10)
_buf_r = w_bot.buffer_for(W_CHAT)
check("shrinking the window trims the buffer to N", len(_buf_r) == 10 and _buf_r[-1].text == "r59", f"len={len(_buf_r)}")

# cache invariant: growing the window doesn't change the prefix; only the trailing note varies
_base = [
    BufferedMessage(user_id=1, sender="Вася", text="привет"),
    BufferedMessage(user_id=1000, sender="Le Chonk", text="привет-привет", is_bot=True),
    BufferedMessage(user_id=1, sender="Вася", text="как жизнь"),
    BufferedMessage(user_id=1000, sender="Le Chonk", text="хорошо", is_bot=True),
]
_r1 = build_llm_messages(system_prompt="SYS", buffer=_base, bot_id=1000, max_images=4)
_r1.append(time_note(datetime(2026, 10, 7, 12, 0, 0)))
_r2 = build_llm_messages(
    system_prompt="SYS",
    buffer=_base + [BufferedMessage(user_id=2, sender="Петя", text="а у меня вопрос")],
    bot_id=1000,
    max_images=4,
)
_r2.append(time_note(datetime(2026, 10, 7, 12, 1, 0)))
check("cache invariant: the prefix doesn't change as the window grows", _r2[: len(_r1) - 1] == _r1[: len(_r1) - 1])
check(
    "cache invariant: the time note is last and changes",
    _r1[-1]["role"] == "system" and "# Текущее время" in _r1[-1]["content"] and _r1[-1]["content"] != _r2[-1]["content"],
)
_m1 = build_llm_messages("SYS", [BufferedMessage(user_id=1, sender="Вася", text="первое")], bot_id=1000)
_m2 = build_llm_messages(
    "SYS",
    [
        BufferedMessage(user_id=1, sender="Вася", text="первое"),
        BufferedMessage(user_id=1, sender="Вася", text="второе сообщение"),
    ],
    bot_id=1000,
)
check("cache invariant: merging appends to the end", _m2[1]["content"] == _m1[1]["content"] + "\n" + "Вася: второе сообщение")

# ── 4d. image auto-cleanup at startup ───────────────────────────────
print("== 4d. image cleanup ==")
_imgs_bak = bot_mod.IMAGES_DIR
_clean_dir = tmpdir / "images_cleanup"
_clean_dir.mkdir(parents=True, exist_ok=True)
(_clean_dir / "a.jpg").write_bytes(b"x")
(_clean_dir / "b.jpg").write_bytes(b"x")
bot_mod.IMAGES_DIR = _clean_dir
_n_rm = LeChonkBot.cleanup_images_dir()
bot_mod.IMAGES_DIR = _imgs_bak
check("cleanup: files from previous runs deleted", _n_rm == 2 and not list(_clean_dir.iterdir()), f"removed {_n_rm}")

# ── 5. live API calls ────────────────────────────────────────────
print("== 5. live API calls ==")


async def api_tests() -> None:
    client = make_client(cfg.openai_api_key, cfg.openai_base_url)
    text = await call_llm(client, cfg.model, [{"role": "user", "content": "Ответь одним словом: работает?"}], max_tokens=4096, temperature=0.1)
    check("text request", bool(text), repr(text[:60]))
    if AVATAR.exists():
        vmsgs = [{"role": "user", "content": [
            {"type": "text", "text": "Что на картинке? Ответь коротко."},
            {"type": "image_url", "image_url": {"url": image_to_data_uri(AVATAR)}},
        ]}]
        v = await call_llm(client, cfg.vision_model or cfg.model, vmsgs, max_tokens=4096)
        check("image request", bool(v), repr(v[:100]))
    else:
        print("  ⏭ skipped: no assets/lechonk_avatar.png")


asyncio.run(api_tests())

# ── 6. run through the real handler (fake Telegram) ──────────
print("== 6. handler with fake Telegram ==")


class FakeFile:
    def __init__(self, src: str):
        self.src = src

    async def download_to_drive(self, custom_path=None):
        dst = Path(custom_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.src, dst)
        return dst


class FakeBot:
    def __init__(self, avatar: str | None = None):
        self.id = 1000
        self.username = "lechonk_bot"
        self.avatar = avatar
        self.sent: list[tuple[int, str]] = []
        self.modes: list = []  # parse_mode of each send_message
        self.invoices: list[dict] = []
        self.precheckout_answers: list[bool] = []
        self.callback_answers: list[int] = []
        self.deleted: list[tuple[int, int]] = []
        self.chat_member_status: dict[tuple[int, int], str] = {}

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        self.modes.append(kw.get("parse_mode"))
        return None

    async def send_chat_action(self, chat_id, action, **kw):
        return True

    async def delete_message(self, chat_id, message_id, **kw):
        self.deleted.append((chat_id, message_id))
        return True

    async def get_chat_member(self, chat_id, user_id, **kw):
        return SimpleNamespace(status=self.chat_member_status.get((chat_id, user_id), "member"))

    async def get_file(self, file_id, **kw):
        return FakeFile(self.avatar)

    async def send_invoice(self, chat_id, title=None, description=None, payload=None,
                           provider_token=None, currency=None, prices=None, **kw):
        self.invoices.append({
            "chat_id": chat_id, "title": title, "payload": payload,
            "provider_token": provider_token, "currency": currency,
            "prices": [(p.label, p.amount) for p in (prices or [])],
            "start_parameter": kw.get("start_parameter"),
        })
        return SimpleNamespace(message_id=900 + len(self.invoices))

    async def answer_pre_checkout_query(self, *args, **kw):
        ok = kw.get("ok", args[1] if len(args) > 1 else None)
        self.precheckout_answers.append(bool(ok))
        return True

    async def answer_callback_query(self, *args, **kw):
        self.callback_answers.append(1)
        return True


class FakeMessage(SimpleNamespace):
    async def delete(self):
        self.deleted = True


def make_update(bot, *, chat: dict, user: dict, message_id: int, text: str | None = None,
                caption: str | None = None, entities=None, caption_entities=None, photo: bool = False,
                reply_to: dict | None = None):
    msg: dict = {"message_id": message_id, "date": int(time.time()), "chat": chat, "from": user}
    if text is not None:
        msg["text"] = text
    if caption is not None:
        msg["caption"] = caption
    if entities:
        msg["entities"] = entities
    if caption_entities:
        msg["caption_entities"] = caption_entities
    if photo:
        msg["photo"] = [{"file_id": "FAKE", "file_unique_id": "U1", "width": 512, "height": 512}]
    if reply_to is not None:
        msg["reply_to_message"] = reply_to
    return Update.de_json({"update_id": message_id, "message": msg}, bot)


def make_callback_update(bot, *, user: dict, data: str, chat_id: int):
    return Update.de_json({
        "update_id": 701,
        "callback_query": {
            "id": f"cb-{data}",
            "from": user,
            "chat_instance": "test",
            "data": data,
            "message": {
                "message_id": 995,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": bot.id, "is_bot": True, "first_name": "Le Chonk"},
                "text": "⚡ Request packs (Telegram Stars)",
            },
        },
    }, bot)


def make_precheckout_update(bot, *, user: dict, payload: str, amount: int, qid: str = "pc1"):
    return Update.de_json({
        "update_id": 800,
        "pre_checkout_query": {
            "id": qid, "from": user, "currency": "XTR",
            "total_amount": amount, "invoice_payload": payload,
        },
    }, bot)


def make_payment_update(bot, *, user: dict, chat_id: int, message_id: int = 997,
                        stars: int = 50, payload: str = "buy:50", charge: str = "chg_t1"):
    return Update.de_json({
        "update_id": message_id,
        "message": {
            "message_id": message_id, "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "from": user,
            "successful_payment": {
                "currency": "XTR", "total_amount": stars, "invoice_payload": payload,
                "telegram_payment_charge_id": charge, "provider_payment_charge_id": "",
            },
        },
    }, bot)


async def handler_tests() -> None:
    bot_app = LeChonkBot(cfg)
    bot_mod.USAGE_LOG = tmpdir / "usage.log"  # sandbox: don't write to the real data/usage.log
    fb = FakeBot(avatar=str(AVATAR) if AVATAR.exists() else None)
    bot_app.bot_id = fb.id
    bot_app.bot_username = fb.username
    bot_app.client = bot_app.get_client(cfg.openai_api_key)
    # sandbox access store so the real data/access.json isn't touched
    bot_app.access = AccessStore(path=tmpdir / "bot_access.json")
    bot_app.credits = CreditsStore(path=tmpdir / "bot_credits.json")
    bot_app.store = ChatSettingsStore(path=tmpdir / "bot_settings.json")  # sandbox chat settings (sponsorship)
    ctx = SimpleNamespace(bot=fb)

    owner_id = cfg.owner_id or 100000001  # synthetic fallback when OWNER_ID isn't set
    group = {"id": -1001234567890, "type": "supergroup", "title": "Тест-группа"}
    vasya = {"id": 555, "is_bot": False, "first_name": "Вася"}
    lena = {"id": 888, "is_bot": False, "first_name": "Лена", "username": "lena_test"}
    dm_lena = {"id": 888, "type": "private"}
    kate = {"id": 889, "is_bot": False, "first_name": "Катя", "username": "kate_test"}
    dm_kate = {"id": 889, "type": "private"}
    dm_owner = {"id": owner_id, "type": "private"}
    owner = {"id": owner_id, "is_bot": False, "first_name": "Owner"}
    dm_stranger = {"id": 999, "type": "private"}
    stranger = {"id": 999, "is_bot": False, "first_name": "Незнакомец"}
    mention = [{"type": "mention", "offset": 0, "length": len("@lechonk_bot")}]

    # A. chatter without addressing — buffered only
    await bot_app.on_message(make_update(fb, chat=group, user=vasya, message_id=11, text="всем привет"), ctx)
    ok = len(fb.sent) == 0 and len(bot_app.buffer_for(group["id"])) == 1
    check("group: no tag — silent but remembers", ok)

    # B. a non-allowed user tags — canned access offer (no LLM); pin the limits explicitly
    bot_app.cfg.noaccess_offer_cooldown_hours = 12
    bot_app.cfg.noaccess_offer_chat_daily_cap = 3
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=vasya, message_id=12,
                                        text="@lechonk_bot привет", entities=mention), ctx)
    sent_new = fb.sent[n0:]
    ok = (len(sent_new) == 1 and sent_new[0][1] == bot_mod.NOACCESS_OFFER_GROUP
          and sent_new[0][0] == group["id"] and len(bot_app.buffer_for(group["id"])) == 2)
    check("group: not allowed — canned access offer", ok, repr(sent_new[0][1][:60]) if sent_new else "none")

    # B2. the same person again immediately — the cooldown holds, silence
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=vasya, message_id=112,
                                        text="@lechonk_bot ну как дела?", entities=mention), ctx)
    check("group: the offer cooldown holds", len(fb.sent) == n0)

    # B3. cooldown expired (simulate 12h+5s) — the offer returns
    bot_app._offer_last[(555, group["id"])] = time.monotonic() - (12 * 3600 + 5)
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=vasya, message_id=113,
                                        text="@lechonk_bot ты тут?", entities=mention), ctx)
    check("group: the offer returns after the cooldown",
          len(fb.sent) == n0 + 1 and fb.sent[-1][1] == bot_mod.NOACCESS_OFFER_GROUP)

    # B4. a third user (Лена) — an offer (the chat cap reaches 3)
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=lena, message_id=114,
                                        text="@lechonk_bot привет!", entities=mention), ctx)
    check("group: another person — their own offer",
          len(fb.sent) == n0 + 1 and fb.sent[-1][1] == bot_mod.NOACCESS_OFFER_GROUP)

    # B5. the chat's daily cap (3) is used up — silence
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=kate, message_id=115,
                                        text="@lechonk_bot хеллоу", entities=mention), ctx)
    check("group: the daily offer cap holds", len(fb.sent) == n0)

    # C. the owner allows Вася (no key)
    bot_app.access.allow(555)
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=vasya, message_id=13,
                                        text="@lechonk_bot ну как дела?", entities=mention), ctx)
    ok = len(fb.sent) == n0 + 1 and "setkey" in fb.sent[-1][1]
    check("group: allowed without a key — asks for /setkey", ok, repr(fb.sent[-1][1][:70]) if fb.sent else "none")

    # D. Вася sets his key (in the sandbox — a real key for the live call)
    bot_app.access.set_key(555, cfg.openai_api_key)
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=vasya, message_id=14,
                                        text="@lechonk_bot привет, ты тут?", entities=mention), ctx)
    ok = len(fb.sent) == n0 + 1 and "please try again in a moment" not in fb.sent[-1][1]
    check("group: with a personal key — a live answer", ok, repr(fb.sent[-1][1][:70]) if fb.sent else "none")

    # D2. a tagged reply to someone else's message — it gets quoted into the context
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=lena, message_id=41,
                                         text="Смотрите, в 2026 обещают конец света"), ctx)
    await bot_app.on_message(make_update(
        fb, chat=group, user=vasya, message_id=42,
        text="@lechonk_bot нам уже начинать бояться?", entities=mention,
        reply_to={"message_id": 41, "date": int(time.time()), "chat": group, "from": lena,
                  "text": "Смотрите, в 2026 обещают конец света"},
    ), ctx)
    _qmsgs = [m for m in bot_app.buffer_for(group["id"]) if m.text and "↪ В ответ на сообщение от Лена" in m.text]
    ok = (len(fb.sent) == n0 + 1 and "please try again in a moment" not in fb.sent[-1][1]
          and bool(_qmsgs) and "бояться" in _qmsgs[-1].text)
    check("group: reply to someone else — quoted into the request", ok, repr(fb.sent[-1][1][:60]) if fb.sent else "none")

    # E. a photo from an allowed user — vision
    if AVATAR.exists():
        n0 = len(fb.sent)
        await bot_app.on_message(make_update(
            fb, chat=group, user=vasya, message_id=15,
            caption="@lechonk_bot что на картинке?",
            caption_entities=[{"type": "mention", "offset": 0, "length": len("@lechonk_bot")}],
            photo=True,
        ), ctx)
        ok = len(fb.sent) == n0 + 1 and "please try again in a moment" not in fb.sent[-1][1]
        check("group: photo — an answer", ok, repr(fb.sent[-1][1][:80]) if fb.sent else "none")
    else:
        print("  ⏭ photo test skipped (no avatar)")

    # F. the owner in DM
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_owner, user=owner, message_id=16, text="ЛеЧонк, ты кто?"), ctx)
    ok = len(fb.sent) == n0 + 1 and "please try again in a moment" not in fb.sent[-1][1]
    check("DM: owner — an answer", ok, repr(fb.sent[-1][1][:70]) if fb.sent else "none")
    check("the answer goes out with parse_mode=HTML", bool(fb.modes) and fb.modes[-1] == "HTML", str(fb.modes[-1]) if fb.modes else "none")

    # G. a stranger in DM — key instructions; the owner is not notified
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_stranger, user=stranger, message_id=17, text="привет!"), ctx)
    sent_new = fb.sent[n0:]
    ref = next((t for cid, t in sent_new if cid == 999), "")
    check("DM: the stranger gets the key instructions", "Mistral API key" in ref and "admin.mistral.ai" in ref and "/setkey" in ref and "/buy" in ref, repr(ref[:80]))
    check("owner is not notified about the stranger", all(cid != owner_id for cid, t in sent_new))

    # G2. a stranger sets their key — access opens immediately
    await bot_app.on_command(make_update(fb, chat=dm_stranger, user=stranger, message_id=28,
                                         text=f"/setkey {cfg.openai_api_key}"), ctx)
    check("/setkey from a stranger — accepted", "Key saved" in fb.sent[-1][1], repr(fb.sent[-1][1][:60]))
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_stranger, user=stranger, message_id=29, text="ну что, теперь работаем?"), ctx)
    ref = next((t for cid, t in fb.sent[n0:] if cid == 999), "")
    check("stranger with their own key — a live answer", bool(ref) and "please try again in a moment" not in ref, repr(ref[:70]))

    # H. commands
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=dm_owner, user=owner, message_id=18, text="/id"), ctx)
    check("/id answers with the ids", len(fb.sent) == n0 + 1 and str(dm_owner["id"]) in fb.sent[-1][1])
    await bot_app.on_command(make_update(fb, chat=dm_owner, user=owner, message_id=19, text="/status"), ctx)
    check("/status to the owner", len(fb.sent) == n0 + 2 and "Le Chonk" in fb.sent[-1][1])
    await bot_app.on_command(make_update(fb, chat=group, user=vasya, message_id=20, text="/status"), ctx)
    check("group: /status ignored (silence)", len(fb.sent) == n0 + 2)
    await bot_app.on_command(make_update(fb, chat=group, user=vasya, message_id=21, text="/setkey abc123456789012"), ctx)
    check("group: /setkey ignored, the message deleted",
          len(fb.sent) == n0 + 2 and (group["id"], 21) in fb.deleted)
    await bot_app.on_command(make_update(fb, chat=group, user=owner, message_id=91, text="/status"), ctx)
    check("group: an owner command works for the owner",
          len(fb.sent) == n0 + 3 and "Le Chonk" in fb.sent[-1][1])
    await bot_app.on_command(make_update(fb, chat=dm_owner, user=owner, message_id=22, text="/allowed"), ctx)
    check("/allowed to the owner", len(fb.sent) == n0 + 4 and "owner" in fb.sent[-1][1])
    await bot_app.on_command(make_update(fb, chat=dm_owner, user=owner, message_id=23, text="/allow @vasya_test"), ctx)
    check("/allow @username", len(fb.sent) == n0 + 5 and "invite list" in fb.sent[-1][1])
    await bot_app.on_command(make_update(fb, chat=dm_owner, user=owner, message_id=24, text="/deny @vasya_test"), ctx)
    check("/deny @username", len(fb.sent) == n0 + 6 and "invites" in fb.sent[-1][1])

    # H2. /help — the access block only shows to allowlisted users
    await bot_app.on_command(make_update(fb, chat=dm_owner, user=owner, message_id=25, text="/help"), ctx)
    check("/help to the owner: the access block + owner commands",
          "allowlist" in fb.sent[-1][1] and "Owner commands" in fb.sent[-1][1])
    check("/help lists /new", "/new" in fb.sent[-1][1])
    n_before = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=vasya, message_id=26, text="/help"), ctx)
    check("group: /help ignored (silence)", len(fb.sent) == n_before)
    await bot_app.on_command(make_update(fb, chat={"id": 555, "type": "private"}, user=vasya, message_id=92, text="/help"), ctx)
    check("/help to an allowed user in DM: the access block, no owner commands",
          "allowlist" in fb.sent[-1][1] and "Owner commands" not in fb.sent[-1][1])
    await bot_app.on_command(make_update(fb, chat=dm_stranger, user=stranger, message_id=27, text="/help"), ctx)
    check("/help to a non-allowed user: plain text about /setkey and /buy",
          "Mistral API key" in fb.sent[-1][1] and "/setkey" in fb.sent[-1][1] and "/buy" in fb.sent[-1][1]
          and "allowlist" not in fb.sent[-1][1])

    # H3. /new — the new-session marker (DMs only)
    _odm = dm_owner["id"]
    _old_txts = [(m.text or "") for m in bot_app.buffer_for(_odm)]
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=dm_owner, user=owner, message_id=93, text="/new"), ctx)
    check("/new in DM: context cleared, confirmation sent",
          len(_old_txts) > 0 and len(bot_app.buffer_for(_odm)) == 0
          and len(fb.sent) == n0 + 1 and fb.sent[-1][1] == bot_mod.NEW_SESSION_NOTE,
          f"was {len(_old_txts)}, now {len(bot_app.buffer_for(_odm))}")
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_owner, user=owner, message_id=94,
                                         text="проверка после /new: сколько будет два плюс два?"), ctx)
    _new_txts = [(m.text or "") for m in bot_app.buffer_for(_odm)]
    check("/new: only the message after the command entered the context",
          len(_new_txts) >= 2 and "два плюс два" in _new_txts[0]
          and not any(old and old in t for t in _new_txts for old in _old_txts),
          str([t[:35] for t in _new_txts]))
    _gbuf = len(bot_app.buffer_for(group["id"]))
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=vasya, message_id=95, text="/new"), ctx)
    await bot_app.on_command(make_update(fb, chat=group, user=owner, message_id=96, text="/new"), ctx)
    check("group: /new ignored (silence, the buffer untouched)",
          len(fb.sent) == n0 and len(bot_app.buffer_for(group["id"])) == _gbuf)

    # J. @username invite: activation at first contact
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_lena, user=lena, message_id=30, text="привет!"), ctx)
    ref = next((t for cid, t in fb.sent[n0:] if cid == 888), "")
    check("Лена before the invite — key instructions", "Mistral API key" in ref, repr(ref[:70]))
    bot_app.access.want_username("lena_test")
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_lena, user=lena, message_id=31, text="ну привет ещё раз"), ctx)
    ref = next((t for cid, t in fb.sent[n0:] if cid == 888), "")
    check("after the invite — activated, asks for a key", "setkey" in ref, repr(ref[:60]))
    check("id bound to @lena_test", bot_app.access.is_allowed(888) and not bot_app.access.is_wanted_username("lena_test"))
    check("owner notified about the activation", any(cid == owner_id and "activated" in t for cid, t in fb.sent[n0:]))

    # K. an invite «on my key» — works immediately, no /setkey
    bot_app.access.want_username("kate_test")
    bot_app.access.trust_name("kate_test")
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_kate, user=kate, message_id=40, text="привет, котик!"), ctx)
    ref = next((t for cid, t in fb.sent[n0:] if cid == 889), "")
    check("on my key: answers without /setkey", bool(ref) and "setkey" not in ref and "please try again in a moment" not in ref, repr(ref[:70]))
    check("trust moved to the id", bot_app.access.is_allowed(889) and bot_app.access.is_owner_key_user(889))
    check("owner notified about kate's activation", any(cid == owner_id and "activated" in t for cid, t in fb.sent[n0:]))

    # L. Stars packs (Telegram Stars)
    buyer = {"id": 777, "is_bot": False, "first_name": "Покупатель", "username": "buyer777"}
    dm_buyer = {"id": 777, "type": "private"}

    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=dm_buyer, user=buyer, message_id=60, text="/buy"), ctx)
    last = fb.sent[-1][1]
    check("/buy — the pack list", len(fb.sent) == n0 + 1 and "20" in last and "100" in last, repr(last[:60]))

    await bot_app.on_callback(make_callback_update(fb, user=buyer, data="buy:50", chat_id=777), ctx)
    inv = fb.invoices[-1] if fb.invoices else {}
    prices = inv.get("prices") or [("", 0)]
    check("button → an XTR invoice for 50⭐", bool(fb.invoices) and inv.get("currency") == "XTR"
          and prices[0][1] == 50 and inv.get("provider_token") == "", str(inv.get("prices")))

    await bot_app.on_precheckout(make_precheckout_update(fb, user=buyer, payload="buy:50:777", amount=50), ctx)
    check("pre_checkout confirmed", bool(fb.precheckout_answers) and fb.precheckout_answers[-1] is True)

    n0 = len(fb.sent)
    await bot_app.on_payment(make_payment_update(fb, user=buyer, chat_id=777, stars=50, payload="buy:50:777", charge="chg_t1"), ctx)
    check("payment: +50 credits", bot_app.credits.get(777) == 50)
    ref = next((t for cid, t in fb.sent[n0:] if cid == 777), "")
    check("payment: confirmation sent", "Payment received" in ref, repr(ref[:70]))

    await bot_app.on_payment(make_payment_update(fb, user=buyer, chat_id=777, stars=50, payload="buy:50:777", charge="chg_t1"), ctx)
    check("a repeated charge_id doesn't double-credit", bot_app.credits.get(777) == 50)

    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_buyer, user=buyer, message_id=64, text="привет! работаешь?"), ctx)
    ref = next((t for cid, t in fb.sent[n0:] if cid == 777), "")
    check("buyer without a key — a live answer", bool(ref) and "please try again in a moment" not in ref, repr(ref[:70]))
    check("a credit was charged (49 of 50)", bot_app.credits.get(777) == 49)

    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=dm_buyer, user=buyer, message_id=65, text="/balance"), ctx)
    check("/balance shows the remaining credits", "49" in fb.sent[-1][1], repr(fb.sent[-1][1][:50]))

    bot_app.credits.spend(777, 48)  # exactly 1 left
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_buyer, user=buyer, message_id=66, text="последний вопрос?"), ctx)
    refs = [t for cid, t in fb.sent[n0:] if cid == 777]
    check("the last credit charged down to 0", bot_app.credits.get(777) == 0)
    check("the last-request note", any("last request" in t for t in refs), repr(refs[-1][:70]) if refs else "none")

    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=dm_buyer, user=buyer, message_id=67, text="и ещё вопрос"), ctx)
    ref = next((t for cid, t in fb.sent[n0:] if cid == 777), "")
    check("out of credits — the offer again", "/buy" in ref and "/setkey" in ref, repr(ref[:80]))

    # N. chat sponsorship: replies to members without access, covered by the sponsor (/sponsor)
    # N0. sponsorship doesn't work in DMs — groups only
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=dm_owner, user=owner, message_id=70, text="/sponsor"), ctx)
    check("sponsorship: not allowed in DM",
          len(fb.sent) == n0 + 1 and "group" in fb.sent[-1][1].lower(), repr(fb.sent[-1][1][:70]))

    # N1. no personal key/balance — can't sponsor (Катя runs on the owner's key)
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=kate, message_id=71, text="/sponsor"), ctx)
    check("sponsorship: no personal key/balance — refused",
          len(fb.sent) == n0 + 1 and "/setkey" in fb.sent[-1][1] and "/buy" in fb.sent[-1][1],
          repr(fb.sent[-1][1][:70]))

    # N2. Вася (personal key) sponsors the chat
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=vasya, message_id=72, text="/sponsor"), ctx)
    ok = len(fb.sent) == n0 + 1 and "now sponsored" in fb.sent[-1][1]
    check("sponsorship: Вася took the chat (sponsor_id=555)",
          ok and str(bot_app.store.get(group["id"], "sponsor_id")) == "555", repr(fb.sent[-1][1][:70]))

    # N3. stranger Петя tags — answered on the sponsor's key instead of the canned offer
    petya = {"id": 666, "is_bot": False, "first_name": "Петя"}
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=petya, message_id=73,
                                         text="@lechonk_bot как жизнь?", entities=mention), ctx)
    ref = next((t for cid, t in fb.sent[n0:] if cid == group["id"]), "")
    check("sponsorship: the stranger answered at the sponsor's expense",
          bool(ref) and "No access" not in ref and "please try again in a moment" not in ref, repr(ref[:70]))

    # N4. someone else's /unsponsor is forbidden
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=lena, message_id=74, text="/unsponsor"), ctx)
    check("sponsorship: removal by a third party forbidden",
          len(fb.sent) == n0 + 1 and "Only the sponsor" in fb.sent[-1][1], repr(fb.sent[-1][1][:70]))

    # N5. a buyer with credits tries to sponsor a taken chat — refused
    bot_app.credits.add(777, 5, 5, "chg_n1")
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=buyer, message_id=75, text="/sponsor"), ctx)
    check("sponsorship: the chat already has a sponsor",
          len(fb.sent) == n0 + 1 and "already sponsored" in fb.sent[-1][1], repr(fb.sent[-1][1][:70]))

    # N6. Вася removes the sponsorship
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=vasya, message_id=76, text="/unsponsor"), ctx)
    check("sponsorship: removed by the sponsor",
          len(fb.sent) == n0 + 1 and "no longer sponsored" in fb.sent[-1][1]
          and not bot_app.store.get(group["id"], "sponsor_id"), repr(fb.sent[-1][1][:70]))

    # N7. the buyer sponsors the chat — paid from his credit balance
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=buyer, message_id=77, text="/sponsor"), ctx)
    check("sponsorship: the buyer took the chat (sponsor_id=777)",
          len(fb.sent) == n0 + 1 and "now sponsored" in fb.sent[-1][1]
          and str(bot_app.store.get(group["id"], "sponsor_id")) == "777", repr(fb.sent[-1][1][:70]))

    # N8. Маша tags: the sponsor is charged; balance 1 → 0 → a quiet DM note to the sponsor
    bot_app.credits.spend(777, 4)  # exactly 1 left
    masha = {"id": 667, "is_bot": False, "first_name": "Маша"}
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=masha, message_id=78,
                                         text="@lechonk_bot привет!", entities=mention), ctx)
    refs = [t for cid, t in fb.sent[n0:] if cid == group["id"]]
    check("sponsorship: answer charged to the sponsor (balance 0)",
          bool(refs) and "please try again in a moment" not in refs[-1] and bot_app.credits.get(777) == 0,
          repr(refs[-1][:70]) if refs else "none")
    check("sponsorship: no «last request» note in the chat",
          not any("That was your last request" in t for t in refs))
    check("sponsorship: a quiet DM note to the sponsor",
          any(cid == 777 and "run out" in t for cid, t in fb.sent[n0:]))

    # N9. after the sponsorship is removed, a stranger gets the canned offer again
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=buyer, message_id=79, text="/unsponsor"), ctx)
    ok = len(fb.sent) == n0 + 1 and not bot_app.store.get(group["id"], "sponsor_id")
    bot_app._offer_day.clear()
    bot_app._offer_last.clear()
    n0 = len(fb.sent)
    await bot_app.on_message(make_update(fb, chat=group, user=masha, message_id=80,
                                         text="@lechonk_bot ну и?", entities=mention), ctx)
    check("sponsorship: removed — the canned offer returns",
          ok and len(fb.sent) == n0 + 1 and fb.sent[-1][1] == bot_mod.NOACCESS_OFFER_GROUP,
          repr(fb.sent[-1][1][:70]))

    # N10. a group administrator can also remove the sponsorship
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=vasya, message_id=81, text="/sponsor"), ctx)
    check("sponsorship: Вася sponsored the chat again",
          len(fb.sent) == n0 + 1 and str(bot_app.store.get(group["id"], "sponsor_id")) == "555",
          repr(fb.sent[-1][1][:70]))
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=lena, message_id=82, text="/unsponsor"), ctx)
    check("sponsorship: a plain member can't remove it",
          len(fb.sent) == n0 + 1 and "chat administrator" in fb.sent[-1][1], repr(fb.sent[-1][1][:70]))
    fb.chat_member_status[(group["id"], 889)] = "administrator"
    n0 = len(fb.sent)
    await bot_app.on_command(make_update(fb, chat=group, user=kate, message_id=83, text="/unsponsor"), ctx)
    check("sponsorship: a group admin removed it (sponsor_id cleared)",
          len(fb.sent) == n0 + 1 and "no longer sponsored" in fb.sent[-1][1]
          and not bot_app.store.get(group["id"], "sponsor_id"), repr(fb.sent[-1][1][:70]))

    # P. concurrency: per-user locks (different users — parallel; same user — busy)
    _orig_call_llm = bot_mod.call_llm
    gate = asyncio.Event()
    in_flight = {"n": 0}

    async def _gated_call_llm(client, model, messages, max_tokens=1024, temperature=0.4, usage_out=None):
        in_flight["n"] += 1
        await gate.wait()
        return "OK (fake-LLM)"

    bot_mod.call_llm = _gated_call_llm
    try:
        t1 = asyncio.create_task(bot_app.on_message(make_update(
            fb, chat=group, user=vasya, message_id=201,
            text="@lechonk_bot вопрос раз", entities=mention), ctx))
        t2 = asyncio.create_task(bot_app.on_message(make_update(
            fb, chat=group, user=kate, message_id=202,
            text="@lechonk_bot вопрос два", entities=mention), ctx))
        for _ in range(60):
            if in_flight["n"] >= 2:
                break
            await asyncio.sleep(0.05)
        check("concurrency: two different users — both LLM calls in flight",
              in_flight["n"] == 2 and not t1.done() and not t2.done(),
              f"in_flight={in_flight['n']}")

        # P2. the same user while their request is in flight — busy line, no new LLM call
        n1 = len(fb.sent)
        await bot_app.on_message(make_update(fb, chat=group, user=vasya, message_id=203,
                                             text="@lechonk_bot и ещё вопрос", entities=mention), ctx)
        sent_new = [t for cid, t in fb.sent[n1:]]
        check("concurrency: re-tagging by the same user — busy, no new LLM call",
              len(sent_new) == 1 and sent_new[0] in bot_mod.BUSY_LINES and in_flight["n"] == 2,
              repr(sent_new[0][:60]) if sent_new else "none")

        gate.set()
        await asyncio.gather(t1, t2)
        answers = [t for cid, t in fb.sent[n1:] if cid == group["id"]]
        check("concurrency: both users got their answers",
              sum("OK (fake-LLM)" in t for t in answers) == 2, f"answers={len(answers)}")
        check("concurrency: locks are per (chat, user)",
              (group["id"], 555) in bot_app.locks and (group["id"], 889) in bot_app.locks
              and all(isinstance(k, tuple) and len(k) == 2 for k in bot_app.locks),
              str([k[0] for k in bot_app.locks][:3]))

        # P3. /new while a request is in flight: an answer started before the reset doesn't enter the context
        gate2 = asyncio.Event()
        in_flight2 = {"n": 0}

        async def _gated2(client, model, messages, max_tokens=1024, temperature=0.4, usage_out=None):
            in_flight2["n"] += 1
            await gate2.wait()
            return "OK (fake-LLM) 2"

        bot_mod.call_llm = _gated2
        t3 = asyncio.create_task(bot_app.on_message(make_update(
            fb, chat=dm_owner, user=owner, message_id=204, text="вопрос, начатый до /new"), ctx))
        for _ in range(60):
            if in_flight2["n"] >= 1:
                break
            await asyncio.sleep(0.05)
        check("session: the request from before /new is in flight", in_flight2["n"] == 1 and not t3.done())
        await bot_app.on_command(make_update(fb, chat=dm_owner, user=owner, message_id=205, text="/new"), ctx)
        gate2.set()
        await t3
        _late = [m.text or "" for m in bot_app.buffer_for(dm_owner["id"])]
        check("after /new, a late answer from the old session isn't appended",
              not any("fake-LLM" in t for t in _late) and _late == [],
              str([t[:30] for t in _late]))
    finally:
        bot_mod.call_llm = _orig_call_llm

    # I. the buffer contains the bot's answers (assistant)
    check("the bot's answers are in the buffer", any(m.is_bot for m in bot_app.buffer_for(group["id"])))
    print("   group buffer:", [(m.sender, (m.text or "")[:22]) for m in bot_app.buffer_for(group["id"])])

    # J. usage log: token counters only, no message texts
    _utxt = bot_mod.USAGE_LOG.read_text("utf-8") if bot_mod.USAGE_LOG.exists() else ""
    check("usage log: token counters recorded", '"prompt"' in _utxt and '"cached"' in _utxt)
    check("usage log: no message texts", "ты кто" not in _utxt and "привет" not in _utxt)


asyncio.run(handler_tests())

# ── summary ──────────────────────────────────────────────────────────────
print(f"\n== SUMMARY: {len(PASS)} ok, {len(FAIL)} fail ==")
if FAIL:
    print("Failed:", ", ".join(FAIL))
    sys.exit(1)
print("All checks passed 🎉")
