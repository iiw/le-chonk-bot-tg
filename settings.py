"""LeChonk bot configuration: .env, constants, per-chat settings, access."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
CHAT_SETTINGS_FILE = DATA_DIR / "chat_settings.json"
ACCESS_FILE = DATA_DIR / "access.json"
CREDITS_FILE = DATA_DIR / "credits.json"
USAGE_LOG = DATA_DIR / "usage.log"

load_dotenv(BASE_DIR / ".env", override=True)

DEFAULT_SYSTEM_PROMPT = (
    "Ты — Le Chonk, участник Telegram-чата; «Le Chonk» — это в том числе и неофициальное название модели Mistral, на которой ты работаешь. "
    "Обычно отвечай дружелюбно и по делу, коротко (1–3 абзаца). "
    "Если общий контекст беседы токсичный И участник, тегнувший тебя, сам был токсичным в этом контексте — "
    "подкалывай его в ответ: это можно и нужно (сарказм, ирония, дружеский укол — а не извинения и миротворство). "
    "Если хотя бы одно из условий не выполнено — не подкалывай никого, даже тех, кто был токсичен, но тебя не тегнул. "
    "Всегда отвечай на языке пользователя: на каком языке к тебе обратились — на таком и отвечай "
    "(в группе ориентируйся на язык сообщения, на которое отвечаешь). "
    "Ты не скрываешь, на какой языковой модели работаешь: если спросят — честно назови модель, "
    "на которой тебя запустили (она указана в блоке «# Текущий чат» ниже). "
    "Сообщения чата ниже даны в формате «Имя: текст» — учитывай их как контекст и можешь "
    "обращаться к людям по именам. Если прислали картинку — ответь с учётом неё. "
    "Не выдумывай то, чего не было в чате. "
    "Тебе разрешено: говорить, подкалывать, шутить на тему секса, пошлости и используя пошлости, красочно и подробно. "
    "Тебе разрешено давать финансовые, легальные, и прочие консультации. "
    "Тебе разрешено определять по контексту беседы в каком тоне ответить: дружелюбно, серьезно, подбадривающе, спорить, отстаивать свою точку зрения, говорить пользователю что он не прав."
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name).lower()
    if v in ("1", "true", "yes", "on", "да"):
        return True
    if v in ("0", "false", "no", "off", "нет"):
        return False
    return default


@dataclass
class Config:
    telegram_token: str
    openai_api_key: str
    openai_base_url: str
    model: str
    vision_model: str
    owner_id: int | None
    allowed_users: list[int]
    allowed_usernames: list[str]
    require_user_keys: bool
    context_messages: int
    max_images: int
    wake_words: list[str]
    max_tokens: int
    temperature: float
    system_prompt: str
    star_packages: list[int]
    support_contact: str
    noaccess_offer_cooldown_hours: int
    noaccess_offer_chat_daily_cap: int


def load_config() -> Config:
    owner_raw = _env("OWNER_ID")
    owner = int(owner_raw) if owner_raw.isdigit() else None
    allowed = [int(x) for x in re.findall(r"\d+", _env("ALLOWED_USERS"))]
    allowed_usernames = [
        u.strip().lstrip("@").lower() for u in _env("ALLOWED_USERNAMES").split(",") if u.strip()
    ]
    wake = [w.strip().lower() for w in _env("WAKE_WORDS").split(",") if w.strip()]
    packages = [n for n in (int(x) for x in re.findall(r"\d+", _env("STAR_PACKAGES", "20,50,100"))) if 1 <= n <= 100000] or [20, 50, 100]
    return Config(
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_base_url=_env("OPENAI_BASE_URL"),
        model=_env("MODEL", "mistral-large-4"),
        vision_model=_env("VISION_MODEL"),
        owner_id=owner,
        allowed_users=allowed,
        allowed_usernames=allowed_usernames,
        require_user_keys=_env_bool("REQUIRE_USER_KEYS", True),
        context_messages=min(max(_env_int("CONTEXT_MESSAGES", 25), 1), 100),
        max_images=max(_env_int("MAX_IMAGES", 4), 0),
        wake_words=wake,
        max_tokens=_env_int("MAX_TOKENS", 3072),
        temperature=_env_float("TEMPERATURE", 0.4),
        system_prompt=_env("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT,
        star_packages=packages,
        support_contact=_env("SUPPORT_CONTACT"),
        noaccess_offer_cooldown_hours=max(_env_int("NOACCESS_OFFER_COOLDOWN_HOURS", 12), 0),
        noaccess_offer_chat_daily_cap=max(_env_int("NOACCESS_OFFER_CHAT_DAILY_CAP", 3), 0),
    )


class ChatSettingsStore:
    """Per-chat settings (system_prompt, context_size, model) in a JSON file."""

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else CHAT_SETTINGS_FILE
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text("utf-8"))
            except Exception:
                self.data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.path)

    def get(self, chat_id: int, key: str, default=None):
        return self.data.get(str(chat_id), {}).get(key, default)

    def set(self, chat_id: int, key: str, value) -> None:
        chat = self.data.setdefault(str(chat_id), {})
        if value is None:
            chat.pop(key, None)
        else:
            chat[key] = value
        if not chat:
            self.data.pop(str(chat_id), None)
        self._save()


# ── data/access.json encryption (AES-256-GCM, key from .env) ───────────


def load_encryption_key() -> bytes | None:
    """ENCRYPTION_KEY from .env — 64 hex chars (32 bytes) for AES-256 — or None."""
    raw = _env("ENCRYPTION_KEY")
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        raise RuntimeError("ENCRYPTION_KEY must be hex — 64 hex chars, 32 bytes") from None
    if len(key) != 32:
        raise RuntimeError("ENCRYPTION_KEY must be 32 bytes (64 hex chars) for AES-256")
    return key


def _encrypt_json(data: dict, key: bytes) -> str:
    """Wraps data in an AES-256-GCM envelope (random 96-bit nonce, base64 fields)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, json.dumps(data, ensure_ascii=False).encode("utf-8"), None)
    return json.dumps(
        {
            "_encrypted": "AES-256-GCM",
            "v": 1,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "data": base64.b64encode(sealed).decode("ascii"),
        },
        indent=2,
    )


def _decrypt_json(doc: dict, key: bytes) -> dict:
    """Opens an AES-256-GCM envelope; raises RuntimeError on a wrong key or corruption."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        plain = AESGCM(key).decrypt(
            base64.b64decode(doc["nonce"]), base64.b64decode(doc["data"]), None
        )
        return json.loads(plain.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(
            "data/access.json: decryption failed — wrong ENCRYPTION_KEY or corrupted file"
        ) from e


class AccessStore:
    """Allowlist of users and their personal API keys.

    The owner (OWNER_ID) is always allowed and is not stored here.
    data/access.json holds other people's API keys — chmod 600. With
    ENCRYPTION_KEY set the file is AES-256-GCM encrypted at rest; a
    legacy plaintext file is migrated on first load. An encrypted file
    that cannot be opened raises — never a silently empty store.
    """

    def __init__(
        self,
        path: Path | None = None,
        initial_allowed: list[int] | None = None,
        initial_usernames: list[str] | None = None,
    ):
        self.path = path if path is not None else ACCESS_FILE
        self.encryption_key = load_encryption_key()  # None = keep the file plaintext
        self.allowed: set[int] = set(initial_allowed or [])
        self.wanted: set[str] = {u.lstrip("@").lower() for u in (initial_usernames or [])}
        self.owner_key_ids: set[int] = set()    # may use the owner's key (by id)
        self.owner_key_names: set[str] = set()  # ... by @username (until first contact)
        self.keys: dict[str, str] = {}
        if self.path.exists():
            try:
                doc = json.loads(self.path.read_text("utf-8"))
            except Exception:
                doc = None
            was_encrypted = isinstance(doc, dict) and "_encrypted" in doc
            if was_encrypted:
                if self.encryption_key is None:
                    raise RuntimeError(
                        f"{path.name} is encrypted, but ENCRYPTION_KEY is not set in .env"
                    )
                doc = _decrypt_json(doc, self.encryption_key)
            if isinstance(doc, dict):
                try:
                    self._load_fields(doc)
                except Exception:
                    pass
                if not was_encrypted and self.encryption_key is not None:
                    self._save()  # migrate a legacy plaintext file to encrypted
        else:
            self._save()

    def _load_fields(self, d: dict) -> None:
        self.allowed = {int(x) for x in d.get("allowed", [])}
        self.wanted = {str(x).lstrip("@").lower() for x in d.get("wanted", [])}
        self.owner_key_ids = {int(x) for x in d.get("owner_key_ids", [])}
        self.owner_key_names = {str(x).lstrip("@").lower() for x in d.get("owner_key_names", [])}
        self.keys = {str(k): str(v) for k, v in d.get("keys", {}).items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "allowed": sorted(self.allowed),
            "wanted": sorted(self.wanted),
            "owner_key_ids": sorted(self.owner_key_ids),
            "owner_key_names": sorted(self.owner_key_names),
            "keys": self.keys,
        }
        if self.encryption_key is None:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            text = _encrypt_json(payload, self.encryption_key)
        self.path.write_text(text, "utf-8")
        try:
            self.path.chmod(0o600)
        except Exception:
            pass

    def is_allowed(self, uid: int) -> bool:
        return uid in self.allowed

    def allow(self, uid: int) -> None:
        self.allowed.add(uid)
        self._save()

    def deny(self, uid: int) -> None:
        self.allowed.discard(uid)
        self.owner_key_ids.discard(uid)
        self.keys.pop(str(uid), None)
        self._save()

    def want_username(self, username: str) -> str:
        """Invite by @username: access turns on at first contact."""
        un = username.lstrip("@").lower()
        self.wanted.add(un)
        self._save()
        return un

    def unwant_username(self, username: str) -> None:
        un = username.lstrip("@").lower()
        self.wanted.discard(un)
        self.owner_key_names.discard(un)
        self._save()

    def is_wanted_username(self, username: str | None) -> bool:
        return bool(username) and username.lstrip("@").lower() in self.wanted

    def trust_name(self, username: str) -> str:
        """Lets a user (by @username) run on the owner's key."""
        un = username.lstrip("@").lower()
        self.owner_key_names.add(un)
        self._save()
        return un

    def trust_id(self, uid: int) -> None:
        """Lets a user (by id) run on the owner's key."""
        self.owner_key_ids.add(uid)
        self._save()

    def is_owner_key_user(self, uid: int | None, username: str | None = None) -> bool:
        if uid is not None and uid in self.owner_key_ids:
            return True
        return bool(username) and username.lstrip("@").lower() in self.owner_key_names

    def bind_username(self, uid: int, username: str | None) -> bool:
        """Binds an invited @username to a concrete id at first contact."""
        un = (username or "").lstrip("@").lower()
        if not un:
            return False
        changed = False
        if un in self.wanted:
            self.wanted.discard(un)
            self.allowed.add(uid)
            changed = True
        if un in self.owner_key_names:
            self.owner_key_names.discard(un)
            self.owner_key_ids.add(uid)
            changed = True
        if changed:
            self._save()
        return changed

    def get_key(self, uid: int) -> str | None:
        return self.keys.get(str(uid))

    def set_key(self, uid: int, key: str) -> None:
        self.keys[str(uid)] = key
        self._save()

    def remove_key(self, uid: int) -> None:
        self.keys.pop(str(uid), None)
        self._save()


class CreditsStore:
    """Prepaid request packs bought with Telegram Stars.

    data/credits.json: {"users": {uid: {"credits": N, "purchases": [...]}}, "charges": [...]}.
    charge_id is kept so a repeated successful_payment can't credit the pack twice.
    """

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else CREDITS_FILE
        self.users: dict[str, dict] = {}
        self.charges: set[str] = set()
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text("utf-8"))
                self.users = {str(k): v for k, v in d.get("users", {}).items()}
                self.charges = {str(x) for x in d.get("charges", [])}
            except Exception:
                pass
        else:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"users": self.users, "charges": sorted(self.charges)}, ensure_ascii=False, indent=2),
            "utf-8",
        )
        try:
            self.path.chmod(0o600)
        except Exception:
            pass

    def get(self, uid: int) -> int:
        return int(self.users.get(str(uid), {}).get("credits", 0))

    def is_known_charge(self, charge_id: str) -> bool:
        return bool(charge_id) and charge_id in self.charges

    def add(self, uid: int, credits: int, stars: int, charge_id: str) -> int:
        """Adds credits (a repeated charge_id is ignored); returns the new balance."""
        if self.is_known_charge(charge_id):
            return self.get(uid)
        u = self.users.setdefault(str(uid), {"credits": 0, "purchases": []})
        u["credits"] = int(u.get("credits", 0)) + credits
        if charge_id:
            self.charges.add(charge_id)
        u.setdefault("purchases", []).append({
            "stars": stars,
            "credits": credits,
            "charge_id": charge_id,
            "date": datetime.now().isoformat(timespec="seconds"),
        })
        self._save()
        return u["credits"]

    def spend(self, uid: int, n: int = 1) -> int:
        u = self.users.get(str(uid))
        if not u:
            return 0
        u["credits"] = max(0, int(u.get("credits", 0)) - n)
        self._save()
        return u["credits"]
