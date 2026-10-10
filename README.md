# 🐈‍⬛ Le Chonk — a Telegram AI assistant bot

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21.6%2B-26A5E4?logo=telegram&logoColor=white)
![OpenAI-compatible API](https://img.shields.io/badge/LLM-OpenAI--compatible-412991?logo=openai&logoColor=white)
![Telegram Stars](https://img.shields.io/badge/payments-Telegram%20Stars-FFD700?logo=telegram&logoColor=white)
![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-green)

<p align="center">
  <img src="assets/lechonk_hero.jpg" width="220" alt="Le Chonk — a chonky cat in a beret">
</p>

Le Chonk is a chat assistant bot for Telegram built on an OpenAI-compatible LLM API (Mistral by default). It lives in group chats and direct messages, keeps the recent conversation in mind, can look at pictures, answers in the user's language — and, if you want, pays for itself with Telegram Stars.

🤖 **Try it in Telegram:** [@le_chonk_bot](https://t.me/le_chonk_bot)

## ✨ Features at a glance

- 🗣️ **Smart addressing** — in groups it answers when @mentioned, replied to, or called by a wake word; in DMs it answers everything (access permitting).
- 🧠 **Cache-friendly memory** — the context window grows 1×→2× and trims back, so Mistral's prefix cache misses once per cycle instead of once per message.
- 🆕 **Fresh sessions on demand** — `/new` in DM drops the context; only messages sent after it are used.
- 🖼️ **Vision** — photos and image files are read by the model; `MAX_IMAGES` (default 4) per request.
- 💌 **Reply awareness** — tag the bot via a reply, and the quoted message is pulled into the request.
- 🔑 **Bring your own key** — users can set a personal Mistral key with `/setkey`; the key message gets deleted.
- ⭐ **Telegram Stars packs** — 20/50/100 prepaid requests (configurable), with balances, billing and refunds.
- 🤝 **Chat sponsorship** — one member can cover replies for everyone else in a group.
- 👑 **Owner toolkit** — per-chat prompts, context size, model overrides, access management, status.
- 🔒 **Private by design** — message texts never touch the disk; keys live in a `chmod 600` file and never appear in logs.
- 🔐 **Encryption at rest** — personal API keys are stored AES-256-GCM encrypted (`ENCRYPTION_KEY` in `.env`); the on-disk file holds ciphertext only.
- 📊 **Cache analytics** — `data/usage.log` records prompt / cached / completion token counters for every call.

## 💬 How conversations work

### In groups

Le Chonk reacts to three things: a real @mention of its username, a reply to one of its own messages, or a wake word from `WAKE_WORDS` (case-insensitive, matched as a word prefix — so «Лечонк», «Лечонку» and «lechonk» all trigger). Anything else is quietly buffered as context and never answered.

Until it is addressed, the bot just listens: every message it can see (texts, photo captions, photos) goes into that chat's in-memory ring buffer. History from before it joined is invisible — a Telegram limitation, not a bug — and the buffer dies with the process: restarts begin with a clean slate.

### In DMs

Direct messages need no wake word or @mention — writing to the bot is enough. Access rules still apply: allowlisted users chat right away, while strangers get a short note with the two ways in — their own Mistral key (`/setkey`) or a prepaid pack (`/buy`).

### Context window

Each chat keeps a sliding window of recent messages: it grows from N up to 2×N, then trims back to the last N. This grow→trim cycle produces one cache miss per cycle instead of one per message, which keeps Mistral's prefix cache warm. N defaults to 25 and is tunable per chat (10–50) with `/setcontext`. In DM, `/new` resets the window on demand: the context is dropped and only messages sent after the command are included.

The current time is appended as the last system note of every request, and the per-chat service block (“which chat, which model, what you can see”) lives in the system prompt — so fast-changing data (the clock) never invalidates the cached prefix.

Tag the bot via a reply and the replied-to text is quoted into the request (`↪ В ответ на сообщение от <Name>: «…»`), even if it has already slid out of the buffer. Replies to the bot's own messages don't get quoted — those are already in its visible history.

### Messages that aren't text

Stickers, voice notes, video circles, videos, GIFs, audio and files aren't interpreted, but they aren't invisible either: the bot records short placeholders (`[стикер]`, `[голосовое сообщение]`, `[файл: report.pdf]`, `[фото]`) so the model still knows what happened in the chat.

### Delivery & reliability

Answers longer than ~4000 characters are split at line or word boundaries, and incoming messages are clipped to 1,500 characters before buffering, so one wall of text can't flood the window. While the model thinks, the “typing…” status stays alive; one request runs per chat at a time — a second trigger gets a polite “one moment” line instead of a race. Failures produce a friendly retry note and are never billed.

## 🖼️ Images and vision

Photos and image documents are downloaded (up to 8 MB), downscaled to ≤1024 px, re-encoded as JPEG and sent to the model as `image_url` data URIs. The last `MAX_IMAGES` images in the buffer go in as real images; older ones collapse into `[фото]` markers so the text context stays intact.

Set `VISION_MODEL` to route image requests to a dedicated vision model while text requests stay on `MODEL`. If a provider rejects the OpenAI-style `{ "url": … }` image shape, the client retries once with the bare-string form — a picky endpoint never kills the whole request.

## 🔐 Access & billing

### Who gets in

Access is checked before any LLM call, so denied requests cost nothing — no tokens are burned on strangers. The owner can list, invite, and revoke users at runtime; none of it needs a restart. There are four ways in:

- **Owner** (`OWNER_ID`) — always allowed, runs on the key from `.env`.
- **Allowlist** — IDs from `ALLOWED_USERS`, managed at runtime with `/allow` and `/deny`; supports invites by @username that bind to a user ID on first contact.
- **Own key** — anyone can send `/setkey <key>` in DM (keys are created at admin.mistral.ai). Stored with `chmod 600`; the bot tries to delete the message with the key.
- **Stars credits** — strangers can buy a prepaid pack with `/buy` and run on the owner's key until the pack is used up.

With `REQUIRE_USER_KEYS=true` (default), allowlisted users must also bring a personal key. `/allow <id> mykey` marks someone as trusted to run on the owner's key instead, and `REQUIRE_USER_KEYS=false` frees the whole allowlist from the key requirement.

When choosing whose key to use, priority is: personal key → sponsor's key or credits → owner's key. A credit is spent only for answers served on the owner's key — never for the owner, trusted `mykey` users, or allowlisted users when `REQUIRE_USER_KEYS=false` — and it's charged per delivered answer, not per attempt.

### Strangers

In a group, a stranger who addresses the bot gets a short canned reply — no LLM involved, no access-prompt spam — pointing to `/setkey` or `/buy`. Throttling is built in: at most once per user per chat every `NOACCESS_OFFER_COOLDOWN_HOURS` (default 12), and no more than `NOACCESS_OFFER_CHAT_DAILY_CAP` (default 3) per chat per day. A cap of `0` disables the offers entirely.

## ⭐ Telegram Stars: prepaid request packs

Le Chonk sells prepaid request packs for Telegram Stars: `20⭐ = 20 requests`, `50⭐ = 50`, `100⭐ = 100` (the lineup comes from `STAR_PACKAGES`). In DM, `/buy` offers a button per pack; the invoice is in currency XTR, needs no payment provider, and is single-chat — a forwarded invoice can't be paid by someone else.

`successful_payment` credits the buyer's balance in `data/credits.json`. One request equals one answer; failed or undelivered replies are never debited, and duplicate payment events are ignored via `telegram_payment_charge_id`, so each pack is credited exactly once.

When a pack hits zero, the bot adds one heads-up line (“that was your last request…”) and then stays quiet about money until asked. `/balance` shows what's left; `/terms`, `/support` and `/paysupport` cover the fine print and the human contact.

### Refunds

Buyers ask via `/paysupport`; the seller issues a full refund (`refundStarPayment` — Stars has no partial refunds and no bank chargebacks). Per `/terms`, the unused remainder is refundable, and refunding a pack revokes its credits from the balance (floored at zero). Tooling: `scripts/refund_star_payment.py <user_id> <charge_id>`; it writes `data/refunds.log` and marks the purchase in `credits.json`.

### Operator notes

Earned Stars are paid out via Fragment: currently a 1,000⭐ minimum and an up-to-21-day hold per Star, paid out in Gram (formerly Toncoin). Alternatively, Stars can be spent on Telegram Ads. Payout rules and fees change — check Fragment's live terms before relying on any number here.

## 🤝 Sponsorship

`/sponsor` in a group lets one member take the chat under their wing: the bot then serves members who have no access of their own — on the sponsor's personal key, or (without one) on the owner's key with 1 credit per delivered answer debited from the sponsor's Stars balance.

The slot holds one sponsor at a time and persists until removed, even while the sponsor is temporarily out of funds (others fall back to the usual no-access offer meanwhile). There are no rate limits; the sponsor covers as many replies as their key or balance allows.

To sponsor, you need a personal key or credits; the bot owner can also take or reassign the slot. `/unsponsor` removes it — allowed for the sponsor, any administrator of that chat, or the bot owner. When a sponsor's balance runs dry, the sponsor gets one quiet DM note; the chat itself sees nothing.

## 🎛 Commands

### For everyone (DM)

| Command | What it does |
|---|---|
| `/start`, `/help` | Command list; the access block adapts to who's asking |
| `/id` | Show this chat's ID and yours |
| `/new` | Start a new session — forget earlier messages |
| `/setkey <key>` | Save your own Mistral API key (DM only) |
| `/delkey` | Delete your saved key |
| `/buy` | Request packs for Telegram Stars |
| `/balance` | Your remaining requests |
| `/terms`, `/support`, `/paysupport` | Terms of use, support, payment issues |

### In groups

| Command | What it does |
|---|---|
| `/sponsor` | Take this chat under your wing |
| `/unsponsor` | Remove the sponsorship (sponsor, chat admin, or bot owner) |

Every other command is silently ignored in groups — no “unknown command” noise — and a stray `/setkey <key>` message gets deleted if the bot has the rights. The owner can still tune things with admin commands in groups, and unknown commands in DMs get a gentle “Try /help”.

### Owner (DM)

| Command | What it does |
|---|---|
| `/status` | Live snapshot: buffer, window, models, prompt source, sponsor, access stats |
| `/reset` | Clear this chat's memory |
| `/setprompt <text>` | Custom system prompt for this chat |
| `/clearprompt` | Restore the default prompt |
| `/setcontext <10–50>` | Context window size (grows to 2×, then trims) |
| `/model` | Show main / vision / per-chat models |
| `/setmodel <id \| clear>` | Change the model for this chat (or revert) |
| `/allowed` | Access list with marks: personal key, on owner's key, invited |
| `/allow <id \| @nick> [mykey]` | Allow a user; `mykey` = trusted to run on the owner's key |
| `/deny <id \| @nick>` | Revoke access (by ID also drops the key and trust) |

The Telegram command menu mirrors all of this: groups see only the sponsor pair, DMs see the public set, and the owner's own DM additionally shows the admin set. Invited users send the owner a tiny 🎉 note when their access activates; strangers never bother the owner.

## 🧠 Under the hood

### Model layer

Provider-agnostic: point `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL` at Mistral (default), OpenAI, OpenRouter, or anything else that speaks the OpenAI chat-completions dialect. Mistral's list-style message content is handled: `thinking` parts are dropped, only the text is returned to the chat.

All non-LLM bot messages — help, prompts, command replies, payment texts, menus — are in English; the model itself always answers in the user's language. Markdown from the model becomes Telegram HTML (`**bold**`, `*italic*`, `~~strike~~`, `code`, code blocks, links, `# headings`); if Telegram rejects the markup, the chunk is re-sent as plain text.

### The default persona

The built-in system prompt tells Le Chonk to: answer in the user's language, name the model it runs on when asked (the current model is injected into every request), use the chat context without inventing facts, and keep the tone flexible — friendly, serious, encouraging, or argumentative, depending on the room. Teasing is gated: it only unlocks when both the conversation and the person tagging are toxic. A block of owner-approved permissions covers humor and consultations.

### Strings & storage

- Buffers: **RAM only** — message texts are never written to disk.
- `data/access.json` — allowlist, invites, personal API keys (`chmod 600`; AES-256-GCM encrypted when `ENCRYPTION_KEY` is set).
- `data/credits.json` — Stars balances and purchase history (`chmod 600`).
- `data/chat_settings.json` — per-chat prompt, context size, model, sponsor.
- `data/usage.log` — per-request token counters: prompt / cached / completion, no content.
- `data/refunds.log` — refund audit trail.
- `data/images/` — working copies only: deleted when they leave the window and at startup.

Keys never appear in logs, and nothing ever echoes them back into a chat. The only place a key can surface is the user's own `/setkey` message — which the bot immediately tries to delete (and tells the user to clean up if it can't). Personal keys belong to their owners: `/delkey` removes one, `/deny` by ID removes it along with access.

## 📁 Project layout

```
lechonk-bot/
├── bot.py                       # entry point: handlers, buffers, commands, payments
├── llm.py                       # OpenAI-compatible client + request assembly
├── settings.py                  # .env config, per-chat settings, access & credits stores
├── tg_format.py                 # Markdown → Telegram HTML
├── scripts/
│   ├── smoke_test.py            # offline checks + live API tests + synthetic update run
│   ├── refund_star_payment.py   # full Stars refund + credit revocation
│   ├── gen_avatar.py            # avatar generation
│   └── set_avatar.py            # install the avatar via the Bot API
├── deploy/lechonk.service       # systemd user unit
├── LICENSE                      # AGPL-3.0
├── assets/                      # images
│   ├── lechonk_avatar.png       # the cat (avatar)
│   └── lechonk_hero.jpg         # README hero photo
├── data/                        # runtime state (git-ignored)
├── .env.example                 # configuration template
└── README.md                   # this doc
```

## 🚀 Getting started

### 1. Prerequisites

You need Python 3.10+ and a bot token from [@BotFather](https://t.me/BotFather). On the API side, grab a key from Mistral (default) or any other OpenAI-compatible provider — and Telegram Stars for paid packs need no provider account at all.

### 2. Install

```bash
git clone https://github.com/iiw/le-chonk-bot-tg.git
cd le-chonk-bot-tg
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env        # then fill in the required values
```

That installs four packages: `python-telegram-bot`, `openai`, `python-dotenv` and `Pillow`. No webhooks, no database, no broker — plain long polling in, answers out. A regular `python3 -m venv` + `pip install -r requirements.txt` works too if you prefer it over `uv`.

### 3. BotFather setup

1. `/newbot` — create the bot (name it **Le Chonk**) and put the token into `TELEGRAM_BOT_TOKEN`.
2. `/setprivacy` → pick your bot → **Disable**. Otherwise it only sees mentions and commands in groups instead of the full chat context (making it a group admin also works; the bot logs a warning at startup if privacy mode blocks it).
3. Avatar: upload `assets/lechonk_avatar.png` via `/setuserpic`, or run `scripts/set_avatar.py` — it checks whether your Bot API version supports `setMyProfilePhoto`.
4. The command menu is installed by the bot itself on startup, scoped per chat type.

### 4. Run

```bash
.venv/bin/python bot.py
```

For a service that survives reboots:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/lechonk.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lechonk
loginctl enable-linger "$USER"     # keep it running after logout
journalctl --user -u lechonk -f    # live logs
```

Adjust `WorkingDirectory` and `ExecStart` in the unit if your checkout lives elsewhere. `enable-linger` is what lets the service outlive your SSH session, and `journalctl --user -u lechonk -f` is the first place to look when something smells off.

## ⚙️ Configuration

Everything is configured through `.env` — copy `.env.example` to start with. Three values are required (bot token, API key, owner ID); the rest has sensible defaults, and some settings are adjustable per chat at runtime.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather. **Required** |
| `OPENAI_API_KEY` | — | Owner's API key. **Required** |
| `OPENAI_BASE_URL` | — | API base URL, e.g. `https://api.mistral.ai/v1`, `https://openrouter.ai/api/v1` |
| `MODEL` | `mistral-large-4` | Model ID |
| `VISION_MODEL` | — (uses `MODEL`) | Model for requests that include images |
| `OWNER_ID` | — | Your Telegram user ID (send `/id`) |
| `ALLOWED_USERS` | — | Comma-separated IDs; initial allowlist (editable in chat later) |
| `ALLOWED_USERNAMES` | — | Invites by @nick; access activates on first contact |
| `REQUIRE_USER_KEYS` | `true` | Require personal keys from allowlisted non-owners |
| `ENCRYPTION_KEY` | — | AES-256 key (64 hex chars) encrypting `data/access.json` at rest; empty = plaintext |
| `CONTEXT_MESSAGES` | `25` | Base context window; grows up to 2×, then trims |
| `MAX_IMAGES` | `4` | Images per request (`0` = no vision) |
| `WAKE_WORDS` | — | Wake words without @, e.g. `лечонк, lechonk` |
| `MAX_TOKENS` | `3072` | Generation cap per answer |
| `TEMPERATURE` | `0.4` | Sampling temperature |
| `SYSTEM_PROMPT` | built-in | Global system prompt override |
| `STAR_PACKAGES` | `20,50,100` | Stars packs, stars = requests |
| `SUPPORT_CONTACT` | — («bot owner») | Contact for `/support` and `/paysupport` |
| `NOACCESS_OFFER_COOLDOWN_HOURS` | `12` | Canned no-access reply cooldown per (user, chat) |
| `NOACCESS_OFFER_CHAT_DAILY_CAP` | `3` | Canned replies per chat per day (`0` = off) |

## 🧪 Testing & tools

`scripts/smoke_test.py` is the safety net: config sanity, addressing logic, the access store, context assembly, live API calls (text + image) and a dry run of synthetic Telegram updates through the real handlers with a fake bot object. It does hit the real API, so it needs working keys.

`scripts/refund_star_payment.py <user_id> <charge_id>` performs a full `refundStarPayment`, revokes that pack's credits (within the remaining balance) and appends to `data/refunds.log`. ⚠️ Restart the service right after — the running process holds balances in memory and would overwrite the file.

Avatars: `scripts/gen_avatar.py` re-draws the cat from code, and `scripts/set_avatar.py` installs the result as the bot's profile photo — it politely checks first whether your Bot API version supports `setMyProfilePhoto`, and you can always upload the PNG by hand via @BotFather.

## 🩺 Troubleshooting

- **Silent in groups** — privacy mode is on and the bot isn't an admin; or it wasn't addressed; or the user has no access (addressing it then returns the canned reply rather than silence).
- **“I need access first”** — no allowlist entry, no key, no credits: DM `/setkey` or `/buy`.
- **`401 Unauthorized`** — check `OPENAI_API_KEY` and `OPENAI_BASE_URL`; Mistral keys typically start with `mstrl_`.
- **Image errors** — the provider or model lacks vision: set `VISION_MODEL` or `MAX_IMAGES=0`.
- **`max_tokens` rejected** — some OpenAI models expect `max_completion_tokens`; Mistral and OpenRouter accept `max_tokens`.
- **No history from before the bot joined** — a Telegram limitation, by design.
- **Stale balances after a refund** — restart the service (see Testing & tools).

## 🐾 Notes

### A pet project, built for fun

Le Chonk is a pet project — built for fun, out of curiosity, and for the joy of having a chonky cat living in the group chat. It isn't a product: there's no roadmap, no support SLA and no team — just a hobby bot whose code is published in case someone else wants to read it, learn from it, or run their own cat. Issues and pull requests are welcome, but replies may take a while; this is a hobby, not a job.

Le Chonk isn't affiliated with Telegram or Mistral AI. The name winks at Mistral's assistant “Le Chat”: same family, better fed. Built with python-telegram-bot, a lot of patience for edge cases, and exactly one cat. 🐾

### License

Licensed under the [GNU AGPL-3.0](LICENSE). In short: use, study, modify and self-host it freely — but if you run a modified version as a network service, you must offer its source to the people using it. See [LICENSE](LICENSE) for the full text.
