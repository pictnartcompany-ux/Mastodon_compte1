#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mastodon bot (Loufi’s Art / ArtLift) — Anti-spam safe, GitHub Actions friendly

- Max 4 posts/day, no posts at night (23:00–07:00 Europe/Brussels)
- 2 image GM/GN max/jour (1 matin, 1 soir), 2 liens max/jour, 1 long max/jour, le reste = boosts (reblogs)
- Créneaux souples (matin/midi/soir) + délais aléatoires → fluidité, pas d’effet “minuteur”
- Images choisies depuis ./assets/posts et évitées si utilisées dans les 14 derniers jours
- Opt-in engagements ONLY (mentions/replies). Favourites autorisés ; pas de commentaires non sollicités
- Daily + hourly rate caps, random delays, 429 backoff (ou ratelimit intégré)
- Anti-répétition (texte 7j, images 14j)
- --oneshot mode pour CI

Dépendances:
  pip install Mastodon.py

Variables d’environnement (sécurité/CI):
  export MASTODON_BASE_URL="https://mastodon.social"         # ton instance
  export MASTODON_ACCESS_TOKEN="xxxxxxxxxxxxxxxx"            # token d’application (scope: read, write, follow)

Utilisation locale:
  python mastodon_bot_safe.py --oneshot
  # ou en boucle:
  python mastodon_bot_safe.py --loop

Images:
  - Mettre les images dans ./assets/posts/ (jpg/jpeg/png)

Notes:
  - Bio claire sur Mastodon : "Automated account — contact @YourHuman".
  - Respect des normes communautaires ; interactions opt-in uniquement.

"""

import os
import sys
import json
import time
import random
import argparse
import datetime as dt
import pathlib
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Mastodon SDK
from mastodon import Mastodon, MastodonAPIError, MastodonNetworkError

# ========== USER CONFIG ==========
SITE_URL = "https://louphi1987.github.io/Site_de_Louphi/"
OPENSEA_URL = "https://opensea.io/collection/loufis-art"
TIMEZONE = "Europe/Brussels"

# Dossier d'images
IMAGES_DIR = "."
ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}
IMAGE_RECENCY_DAYS = 14

# Quiet hours
NO_POST_START_HOUR = 23  # inclusive
NO_POST_END_HOUR = 7     # exclusive

# Global daily caps
MAX_POSTS_PER_DAY = 4
MAX_ENGAGEMENTS_PER_DAY = 10  # only opt-in mentions/replies

# Hourly caps
MAX_POSTS_PER_HOUR = 2
MAX_ENGAGEMENTS_PER_HOUR = 3

# Random delay windows (secs)
DELAY_POST_MIN_S = 8
DELAY_POST_MAX_S = 28
DELAY_ENGAGE_MIN_S = 12
DELAY_ENGAGE_MAX_S = 45

# ======== CAPS PAR TYPE ========
MAX_IMG_GMGN_PER_DAY = 2      # 1 le matin + 1 le soir
MAX_SHORT_LINK_PER_DAY = 2
MAX_GMGN_LONG_PER_DAY = 1

# ========== TEXT LIBRARIES ==========
GM_SHORT = [
    "GM ☀️",
    "GM ✨",
    "GM 🌞",
    "GM 🌿",
    "GM 👋",
]
GN_SHORT_BASE = ["GN", "Gn", "gn", "Good night", "Night"]
RANDOM_GN_EMOJIS = ["🌙", "✨", "⭐", "💤", "🌌", "🫶", "💫", "😴", "🌠"]

GM_LONG = [
    "GM 🌱 Wishing you a day full of creativity and light.",
    "GM ✨ New day, new brushstrokes.",
    "GM 🌊 Let's dive into imagination today.",
]
GN_LONG = [
    "Good night 🌙💫 May your dreams be as colorful as art.",
    "GN 🌌 See you in tomorrow’s stories.",
    "Resting the canvas for tomorrow’s colors. GN ✨",
]

# Link posts (Mastodon auto-link les URLs)
LINK_POOLS = [
    SITE_URL,
    OPENSEA_URL,
]

COMMENT_SHORT = [
    "Thanks for the mention!",
    "Appreciate it 🙏",
    "Thanks for looping me in ✨",
    "Thanks!",
]
COMMENT_EMOJIS = ["🔥", "👍", "👏", "😍", "✨", "🫶", "🎉", "💯", "🤝", "⚡", "🌟"]

# ========== STATE PERSISTENCE ==========
STATE_FILE = "mastodon_bot_state.json"

@dataclass
class DailyCounters:
    date: str
    posts: int
    engagements: int

@dataclass
class HourlyCounters:
    hour_key: str
    posts: int
    engagements: int


def _pertype_zero() -> Dict[str, int]:
    return {"post_img_gmgn_short": 0, "post_gmgn_long": 0, "post_short_link": 0, "reblog": 0}


def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
                state.setdefault("history", [])
                state.setdefault("daily", {"date": "", "posts": 0, "engagements": 0})
                state.setdefault("hourly", {"key": "", "posts": 0, "engagements": 0})
                state.setdefault("processed_notifications", [])
                state.setdefault("recent_reblogs", [])
                state.setdefault("pertype", _pertype_zero())
                return state
            except Exception:
                pass
    return {
        "history": [],
        "daily": {"date": "", "posts": 0, "engagements": 0},
        "hourly": {"key": "", "posts": 0, "engagements": 0},
        "processed_notifications": [],
        "recent_reblogs": [],  # list of ids
        "pertype": _pertype_zero(),
    }


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def reset_daily_if_needed(state: Dict[str, Any], now_local: dt.datetime) -> None:
    today = now_local.date().isoformat()
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "posts": 0, "engagements": 0}
        state["recent_reblogs"] = state.get("recent_reblogs", [])[-200:]
        state["pertype"] = _pertype_zero()


def reset_hourly_if_needed(state: Dict[str, Any], now_local: dt.datetime) -> None:
    key = f"{now_local.date().isoformat()}_{now_local.hour:02d}"
    if state["hourly"].get("key") != key:
        state["hourly"] = {"key": key, "posts": 0, "engagements": 0}


def remember_post(state: Dict[str, Any], text: str, media: Optional[str] = None) -> None:
    now = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    rec = {"text": text, "ts": now}
    if media:
        rec["media"] = media
    state["history"].append(rec)
    state["history"] = state["history"][-400:]


def recently_used_text(state: Dict[str, Any], text: str, days: int = 7) -> bool:
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days)
    for item in reversed(state.get("history", [])):
        ts = item.get("ts")
        if not ts:
            continue
        try:
            when = dt.datetime.fromisoformat(ts)
        except Exception:
            continue
        if when >= cutoff and item.get("text", "").strip() == text.strip():
            return True
    return False


def recently_used_media(state: Dict[str, Any], media_path: str, days: int = IMAGE_RECENCY_DAYS) -> bool:
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days)
    for item in reversed(state.get("history", [])):
        ts = item.get("ts")
        mp = item.get("media")
        if not ts or not mp:
            continue
        try:
            when = dt.datetime.fromisoformat(ts)
        except Exception:
            continue
        if when >= cutoff and mp == media_path:
            return True
    return False

# ========== FILES / IMAGES ==========

def list_local_images(folder: str) -> List[str]:
    p = pathlib.Path(folder)
    if not p.exists():
        return []
    return [str(f) for f in p.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_EXTS]


def pick_fresh_image(state: Dict[str, Any]) -> Optional[str]:
    imgs = list_local_images(IMAGES_DIR)
    if not imgs:
        return None
    random.shuffle(imgs)
    for img in imgs:
        if not recently_used_media(state, img, days=IMAGE_RECENCY_DAYS):
            return img
    return random.choice(imgs)

# ========== MASTODON CLIENT & BACKOFF ==========

def _needs_backoff(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "ratelimit" in msg or "too many requests" in msg


def with_backoff(fn):
    def wrapper(*args, **kwargs):
        delay = 5.0
        tries = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except (MastodonNetworkError, MastodonAPIError, Exception) as e:
                tries += 1
                # Mastodon.py peut aussi attendre automatiquement suivant ratelimit_method
                if _needs_backoff(e):
                    sleep_s = min(delay * (2 ** (tries - 1)), 60.0)
                    print(f"[BACKOFF] Rate limited; sleeping {sleep_s:.1f}s", file=sys.stderr)
                    time.sleep(sleep_s)
                    continue
                # petits transients
                if tries <= 2:
                    sleep_s = 2.0 * tries
                    print(f"[RETRY] {e}; sleeping {sleep_s:.1f}s", file=sys.stderr)
                    time.sleep(sleep_s)
                    continue
                raise
    return wrapper


@with_backoff
def mstdn_client() -> Mastodon:
    base_url = os.getenv("MASTODON_BASE_URL", "").strip()
    token = os.getenv("MASTODON_ACCESS_TOKEN", "").strip()
    if not base_url or not token:
        raise RuntimeError("Missing MASTODON_BASE_URL or MASTODON_ACCESS_TOKEN in env")
    # ratelimit_method "pace" espace automatiquement les appels ; "wait" bloque jusqu'au reset
    client = Mastodon(
        access_token=token,
        api_base_url=base_url,
        ratelimit_method="pace",
        request_timeout=30,
    )
    return client


@with_backoff
def post_status(client: Mastodon, text: str, image_path: Optional[str] = None) -> Optional[int]:
    media_ids = None
    if image_path:
        media = client.media_post(image_path, description="Artwork from Loufi’s Art")
        media_ids = [media["id"]]
    st = client.status_post(text, media_ids=media_ids, visibility="public")
    return st["id"] if st and "id" in st else None


@with_backoff
def list_notifications(client: Mastodon, limit: int = 30):
    return client.notifications(limit=limit)


@with_backoff
def favourite_status(client: Mastodon, status_id: int) -> bool:
    client.status_favourite(status_id)
    return True


@with_backoff
def reblog_status(client: Mastodon, status_id: int) -> bool:
    client.status_reblog(status_id)
    return True


@with_backoff
def reply_to_status(client: Mastodon, status_id: int, text: str) -> bool:
    client.status_post(text, in_reply_to_id=status_id, visibility="public")
    return True


@with_backoff
def timeline_home(client: Mastodon, limit: int = 40):
    return client.timeline_home(limit=limit)

# ========== CONTENT PICKERS ==========

def in_time_window(now_local: dt.datetime, window: str) -> bool:
    h = now_local.hour
    if window == "morning":
        return 7 <= h < 11
    if window == "evening":
        return 19 <= h < 23
    if window == "midday":
        return 11 <= h < 19
    return False


def is_quiet_hours(now_local: dt.datetime) -> bool:
    h = now_local.hour
    if NO_POST_START_HOUR <= NO_POST_END_HOUR:
        return NO_POST_START_HOUR <= h < NO_POST_END_HOUR
    return h >= NO_POST_START_HOUR or h < NO_POST_END_HOUR


def pick_without_recent(state: Dict[str, Any], pool: List[str]) -> str:
    shuffled = pool[:]
    random.shuffle(shuffled)
    for s in shuffled:
        if not recently_used_text(state, s):
            return s
    return random.choice(pool)


def build_gm_short() -> str:
    return random.choice(GM_SHORT)


def build_gn_short() -> str:
    base = random.choice(GN_SHORT_BASE)
    if random.random() < 0.85:
        base = f"{base} {random.choice(RANDOM_GN_EMOJIS)}"
    return base


def pick_gmgn_text(state: Dict[str, Any], now_local: dt.datetime, long: bool = False) -> str:
    if in_time_window(now_local, "morning"):
        return pick_without_recent(state, GM_LONG) if long else build_gm_short()
    if in_time_window(now_local, "evening"):
        return pick_without_recent(state, GN_LONG) if long else build_gn_short()
    return build_gm_short()


def pick_link_short(state: Dict[str, Any]) -> str:
    pools = LINK_POOLS[:]
    random.shuffle(pools)
    for url in pools:
        if not recently_used_text(state, url):
            return url
    return random.choice(LINK_POOLS)

# ========== ACTION SELECTION WITH CAPS ==========

def choose_action_with_caps(now_local: dt.datetime, pertype: Dict[str, int]) -> str:
    h = now_local.hour
    if 7 <= h < 11:
        if pertype["post_img_gmgn_short"] < MAX_IMG_GMGN_PER_DAY and pertype["post_img_gmgn_short"] == 0:
            return "post_img_gmgn_short"
        if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
            return "post_short_link"
        if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
            return "post_gmgn_long"
        return "reblog"

    if 11 <= h < 19:
        if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
            return "post_short_link"
        if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
            return "post_gmgn_long"
        return "reblog"

    if 19 <= h < 23:
        if pertype["post_img_gmgn_short"] < MAX_IMG_GMGN_PER_DAY:
            return "post_img_gmgn_short"
        if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
            return "post_short_link"
        if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
            return "post_gmgn_long"
        return "reblog"

    if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
        return "post_short_link"
    if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
        return "post_gmgn_long"
    return "reblog"

# ========== OPT-IN ENGAGEMENTS (MENTIONS / REPLIES) ==========

def fetch_unprocessed_mentions(client: Mastodon, state: Dict[str, Any], limit: int = 40):
    items = list_notifications(client, limit=limit) or []
    processed = set(state.get("processed_notifications", []))
    fresh = []
    for n in items:
        # types: 'mention', 'favourite', 'reblog', 'follow', ...
        if n.get("type") != "mention":
            continue
        nid = str(n.get("id"))
        if not nid or nid in processed:
            continue
        fresh.append(n)
    return fresh


def engage_for_notification(client: Mastodon, n) -> Optional[str]:
    st = n.get("status")
    if not st:
        return None
    sid = st.get("id")
    if not sid:
        return None
    # Engagement minimal : favourite OU bref merci (75/25)
    if random.random() < 0.75:
        favourite_status(client, sid)
        return "favourite"
    else:
        reply = random.choice(COMMENT_SHORT) if random.random() < 0.7 else random.choice(COMMENT_EMOJIS)
        reply_to_status(client, sid, reply)
        return f"reply:{reply}"

# ========== SAFE REBLOG PICKER ==========

def pick_safe_reblog(client: Mastodon, state: Dict[str, Any]):
    tl = timeline_home(client, limit=50) or []
    recent = set(state.get("recent_reblogs", []))
    random.shuffle(tl)
    for item in tl:
        # éviter ses propres statuts
        acct = item.get("account", {})
        my_id = None
        try:
            me = client.account_verify_credentials()
            my_id = me.get("id")
        except Exception:
            pass
        if my_id and acct.get("id") == my_id:
            continue
        sid = item.get("id")
        if not sid:
            continue
        if str(sid) in recent:
            continue
        # éviter de rebooster un boost
        if item.get("reblog") is not None:
            continue
        return sid
    return None

# ========== ACTION ENGINE ==========

def can_post(state: Dict[str, Any]) -> bool:
    return state["daily"]["posts"] < MAX_POSTS_PER_DAY and state["hourly"]["posts"] < MAX_POSTS_PER_HOUR


def can_engage(state: Dict[str, Any]) -> bool:
    return state["daily"]["engagements"] < MAX_ENGAGEMENTS_PER_DAY and state["hourly"]["engagements"] < MAX_ENGAGEMENTS_PER_HOUR


def do_one_action(client: Mastodon, state: Dict[str, Any], tz: ZoneInfo) -> str:
    now_local = dt.datetime.now(tz)
    reset_daily_if_needed(state, now_local)
    reset_hourly_if_needed(state, now_local)

    # 1) Opt-in engagements (mentions)
    if can_engage(state):
        fresh_mentions = fetch_unprocessed_mentions(client, state, limit=40)
        random.shuffle(fresh_mentions)
        if fresh_mentions:
            n = fresh_mentions[0]
            kind = engage_for_notification(client, n)
            if kind:
                nid = str(n.get("id"))
                if nid:
                    state.setdefault("processed_notifications", []).append(nid)
                    state["processed_notifications"] = state["processed_notifications"][-500:]
                state["daily"]["engagements"] += 1
                state["hourly"]["engagements"] += 1
                save_state(state)
                nap = random.uniform(DELAY_ENGAGE_MIN_S, DELAY_ENGAGE_MAX_S)
                print(f"Engaged ({kind}). Sleeping ~{int(nap)}s...")
                time.sleep(nap)
                return "engaged"

    # 2) Posting (respect quiet hours)
    if can_post(state) and not is_quiet_hours(now_local):
        pertype = state.get("pertype", _pertype_zero())
        action = choose_action_with_caps(now_local, pertype)

        def downgrade_from(action_name: str) -> str:
            if action_name == "post_img_gmgn_short":
                if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
                    return "post_short_link"
                if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
                    return "post_gmgn_long"
                return "reblog"
            if action_name == "post_gmgn_long":
                if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
                    return "post_short_link"
                return "reblog"
            if action_name == "post_short_link":
                if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
                    return "post_gmgn_long"
                return "reblog"
            return "reblog"

        text: Optional[str] = None
        image: Optional[str] = None

        # Gestion des reblogs immédiate
        if action == "reblog":
            sid = pick_safe_reblog(client, state)
            if sid:
                try:
                    reblog_status(client, sid)
                    state.setdefault("recent_reblogs", []).append(str(sid))
                    state["recent_reblogs"] = state["recent_reblogs"][-400:]
                    state["daily"]["posts"] += 1
                    state["hourly"]["posts"] += 1
                    state["pertype"]["reblog"] = state.get("pertype", {}).get("reblog", 0) + 1
                    save_state(state)
                    nap = random.uniform(DELAY_POST_MIN_S, DELAY_POST_MAX_S)
                    print(f"Reblogged {sid}. Sleeping ~{int(nap)}s…")
                    time.sleep(nap)
                    return "reblogged"
                except Exception as e:
                    print(f"[reblog] error: {e}", file=sys.stderr)
            # fallback
            if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
                action = "post_short_link"
            elif pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
                action = "post_gmgn_long"
            else:
                print("Nothing to reblog or fallback. Skipping.")
                return "skip"

        if action == "post_img_gmgn_short":
            text = pick_gmgn_text(state, now_local, long=False)
            image = pick_fresh_image(state)
            if image is None:
                action = downgrade_from("post_img_gmgn_short")

        if action == "post_gmgn_long":
            if pertype["post_gmgn_long"] >= MAX_GMGN_LONG_PER_DAY:
                action = downgrade_from("post_gmgn_long")
            else:
                text = pick_gmgn_text(state, now_local, long=True)
                if random.random() < 0.30:
                    image = pick_fresh_image(state)

        if action == "post_short_link":
            if pertype["post_short_link"] >= MAX_SHORT_LINK_PER_DAY:
                action = downgrade_from("post_short_link")
            else:
                text = pick_link_short(state)

        if action == "reblog":
            sid = pick_safe_reblog(client, state)
            if sid:
                try:
                    reblog_status(client, sid)
                    state.setdefault("recent_reblogs", []).append(str(sid))
                    state["recent_reblogs"] = state["recent_reblogs"][-400:]
                    state["daily"]["posts"] += 1
                    state["hourly"]["posts"] += 1
                    state["pertype"]["reblog"] = state.get("pertype", {}).get("reblog", 0) + 1
                    save_state(state)
                    nap = random.uniform(DELAY_POST_MIN_S, DELAY_POST_MAX_S)
                    print(f"Reblogged {sid}. Sleeping ~{int(nap)}s…")
                    time.sleep(nap)
                    return "reblogged"
                except Exception as e:
                    print(f"[reblog] error: {e}", file=sys.stderr)
            print("No reblog candidate found. Skipping.")
            return "skip"

        if not text and action != "reblog":
            print("No content prepared for action. Skipping.")
            return "skip"

        try:
            sid = post_status(client, text, image)
        except Exception as e:
            print(f"[post] error: {e}", file=sys.stderr)
            return "post_failed"

        if sid:
            remember_post(state, text, media=image)
            state["daily"]["posts"] += 1
            state["hourly"]["posts"] += 1
            state["pertype"][action] = state.get("pertype", {}).get(action, 0) + 1
            save_state(state)
            nap = random.uniform(DELAY_POST_MIN_S, DELAY_POST_MAX_S)
            print(f"Posted: {text[:80]}{'…' if len(text)>80 else ''} {'[+image]' if image else ''}\nSleeping ~{int(nap)}s…")
            time.sleep(nap)
            return "posted"

        print("Post failed")
        return "post_failed"

    print("Nothing to do (caps reached / quiet hours / no mentions)")
    return "skip"

# ========== MAIN ==========

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oneshot", action="store_true", help="Perform one safe action and exit (CI mode)")
    parser.add_argument("--loop", action="store_true", help="Run continuous loop with sleeps (local use)")
    args = parser.parse_args()

    tz = ZoneInfo(TIMEZONE)
    client = mstdn_client()
    state = load_state()

    if args.oneshot or not args.loop:
        status = do_one_action(client, state, tz)
        print(f"Status: {status}")
        sys.exit(0)

    print("Loop mode (anti-spam). Ctrl+C to stop.")
    while True:
        try:
            do_one_action(client, state, tz)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            cool = random.uniform(60, 120)
            print(f"[Loop warn] {e}. Cooling down {int(cool)}s", file=sys.stderr)
            time.sleep(cool)
        now_local = dt.datetime.now(tz)
        if is_quiet_hours(now_local):
            nap = random.uniform(70*60, 120*60)
        elif 7 <= now_local.hour < 23:
            nap = random.uniform(25*60, 55*60)
        else:
            nap = random.uniform(45*60, 80*60)
        if random.random() < 0.18:
            nap += random.uniform(20*60, 40*60)
        print(f"Sleeping ~{int(nap//60)} min…")
        time.sleep(nap)


if __name__ == "__main__":
    main()
