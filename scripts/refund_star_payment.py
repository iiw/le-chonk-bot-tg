#!/usr/bin/env python3
"""Refund a Telegram Stars payment for Le Chonk.

Usage:
    .venv/bin/python scripts/refund_star_payment.py <user_id> <charge_id>

Does: prints the bot's star balance before/after, calls refundStarPayment
(full amount — Stars has no partial refunds), REVOKES the pack's credits from
the user's balance (within the remaining balance, floored at 0), appends to
data/refunds.log and marks the purchase in data/credits.json.

IMPORTANT: restart the service after running (systemctl --user restart lechonk) —
the running process keeps balances in memory and would otherwise overwrite the file.
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import load_config, CREDITS_FILE  # noqa: E402

REFUNDS_LOG = Path(CREDITS_FILE).parent / "refunds.log"

if len(sys.argv) < 3:
    raise SystemExit("usage: refund_star_payment.py <user_id> <charge_id>")

USER_ID = int(sys.argv[1])
CHARGE = sys.argv[2]
cfg = load_config()
state = {"ok": False}


async def main() -> None:
    from telegram import Bot

    bot = Bot(cfg.telegram_token)
    try:
        try:
            bal = await bot.get_my_star_balance()
            print("star balance before:", getattr(bal, "amount", bal))
        except Exception as e:
            print("star balance before: n/a —", type(e).__name__)
        try:
            ok = await bot.refund_star_payment(user_id=USER_ID, telegram_payment_charge_id=CHARGE)
            state["ok"] = bool(ok)
            print("refundStarPayment →", ok)
        except AttributeError:
            import urllib.request

            url = f"https://api.telegram.org/bot{cfg.telegram_token}/refundStarPayment"
            req = urllib.request.Request(
                url,
                data=json.dumps({"user_id": USER_ID, "telegram_payment_charge_id": CHARGE}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read().decode())
            state["ok"] = bool(resp.get("ok"))
            print("raw refundStarPayment →", json.dumps(resp)[:300])
        try:
            bal = await bot.get_my_star_balance()
            print("star balance after:", getattr(bal, "amount", bal))
        except Exception as e:
            print("star balance after: n/a —", type(e).__name__)
    finally:
        await bot.shutdown()


asyncio.run(main())

if not state["ok"]:
    print("REFUND FAILED — leaving the credits untouched")
    raise SystemExit(1)


def _purchase() -> dict | None:
    try:
        d = json.loads(Path(CREDITS_FILE).read_text("utf-8"))
        for p in d.get("users", {}).get(str(USER_ID), {}).get("purchases", []):
            if p.get("charge_id") == CHARGE:
                return p
    except Exception:
        pass
    return None


p = _purchase()
stars = p.get("stars") if p else "?"
REFUNDS_LOG.parent.mkdir(parents=True, exist_ok=True)
with REFUNDS_LOG.open("a", encoding="utf-8") as f:
    f.write(f"{datetime.now().isoformat(timespec='seconds')} refund {stars}⭐ user={USER_ID} charge={CHARGE}\n")
print(f"refunds.log: recorded (↩ {stars}⭐)")

# mark the refund + revoke the pack's credits (within the remaining balance, floored at 0)
try:
    path = Path(CREDITS_FILE)
    d = json.loads(path.read_text("utf-8"))
    u = d.get("users", {}).get(str(USER_ID), {})
    rec = next((x for x in u.get("purchases", []) if x.get("charge_id") == CHARGE), None)
    if rec is None:
        print("credits.json: purchase not found — nothing to mark")
    else:
        if not rec.get("refunded_at"):
            rec["refunded_at"] = datetime.now().isoformat(timespec="seconds")
            print("credits.json: refunded_at set")
        cur = int(u.get("credits", 0))
        revoke = min(int(rec.get("credits") or 0), cur) if not rec.get("revoked_credits") else 0
        if revoke > 0:
            u["credits"] = cur - revoke
            rec["revoked_credits"] = revoke
            with REFUNDS_LOG.open("a", encoding="utf-8") as f:
                f.write(
                    f"{datetime.now().isoformat(timespec='seconds')} revoked {revoke} credits "
                    f"user={USER_ID} charge={CHARGE} (balance {cur} → {u['credits']})\n"
                )
            print(f"credits.json: revoked {revoke} credits, balance {cur} → {u['credits']}")
        elif rec.get("revoked_credits"):
            print("credits.json: credits were already revoked earlier")
        else:
            print("credits.json: nothing to revoke (balance 0)")
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
except Exception as e:
    print("credits.json: mark/revoke failed —", type(e).__name__)
