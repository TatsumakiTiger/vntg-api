import os
import requests
import psycopg2
import jwt as pyjwt
from datetime import datetime, timezone, timedelta
from flask import Flask, redirect, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://www.vntg.com.pl")
CORS(app, origins=[FRONTEND_URL], supports_credentials=True)

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
JWT_SECRET = os.environ.get("JWT_SECRET", "CHANGE-ME-set-this-in-railway")
REDIRECT_URI = os.environ.get(
    "DISCORD_REDIRECT_URI",
    "https://vntg-api-production.up.railway.app/api/callback",
)

DISCORD_API = "https://discord.com/api/v10"
SCOPES = "identify email guilds"

GUILD_ID = "1477669827547103365"
VERIFIED_ROLE_ID = "1491125641226227742"

SESSION_DAYS = 30


# ────────────────────────────────────────────
# JWT helpers
# ────────────────────────────────────────────
def create_session_token(discord_id):
    payload = {
        "sub": discord_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_session_token(token):
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("sub")
    except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError):
        return None


# ────────────────────────────────────────────
# Database helpers
# ────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            discord_id          TEXT PRIMARY KEY,
            username            TEXT,
            global_name         TEXT,
            avatar              TEXT,
            email               TEXT,
            vantage_nick        TEXT UNIQUE,
            custom_avatar       TEXT,
            onboarding_complete BOOLEAN DEFAULT false,
            created_at          TIMESTAMP DEFAULT now(),
            last_login          TIMESTAMP DEFAULT now()
        )
        """
    )
    # Migrate existing tables that lack new columns
    for col, typedef in [
        ("vantage_nick", "TEXT UNIQUE"),
        ("custom_avatar", "TEXT"),
        ("onboarding_complete", "BOOLEAN DEFAULT false"),
        ("subscribed_agent", "TEXT"),
        ("streak", "INTEGER DEFAULT 0"),
        ("last_seen_date", "DATE"),
        ("daily_log", "TEXT[] DEFAULT '{}'"),
        ("xp", "INTEGER DEFAULT 0"),
    ]:
        try:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
        except Exception:
            conn.rollback()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            video_id    TEXT PRIMARY KEY,
            player      TEXT NOT NULL,
            agent       TEXT NOT NULL,
            map         TEXT NOT NULL,
            channel     TEXT,
            created_at  TIMESTAMP DEFAULT now()
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def upsert_user(discord_id, username, global_name, avatar, email):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (discord_id, username, global_name, avatar, email, last_login)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (discord_id) DO UPDATE SET
            username    = EXCLUDED.username,
            global_name = EXCLUDED.global_name,
            avatar      = EXCLUDED.avatar,
            email       = EXCLUDED.email,
            last_login  = EXCLUDED.last_login
        """,
        (discord_id, username, global_name, avatar, email, datetime.now(timezone.utc)),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_user(discord_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT discord_id, username, global_name, avatar, email, created_at, last_login, vantage_nick, custom_avatar, onboarding_complete, subscribed_agent, streak, last_seen_date, daily_log, xp FROM users WHERE discord_id = %s",
        (discord_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "global_name": row[2],
        "avatar": row[3],
        "email": row[4],
        "created_at": row[5].isoformat() if row[5] else None,
        "last_login": row[6].isoformat() if row[6] else None,
        "vantage_nick": row[7],
        "custom_avatar": row[8],
        "onboarding_complete": bool(row[9]) if row[9] is not None else False,
        "subscribed_agent": row[10],
        "streak": row[11] or 0,
        "last_seen_date": row[12].isoformat() if row[12] else None,
        "daily_log": list(row[13]) if row[13] else [],
        "xp": row[14] or 0,
    }


def update_streak(discord_id):
    from datetime import date, timedelta
    today = date.today()
    today_str = today.isoformat()
    yesterday_str = (today - timedelta(days=1)).isoformat()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT streak, last_seen_date, daily_log, xp FROM users WHERE discord_id = %s",
        (discord_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close(); return

    current_streak, last_seen, daily_log, current_xp = row
    current_streak = current_streak or 0
    daily_log = list(daily_log) if daily_log else []
    current_xp = current_xp or 0

    if last_seen and last_seen.isoformat() == today_str:
        cur.close(); conn.close(); return

    new_streak = (current_streak + 1) if (last_seen and last_seen.isoformat() == yesterday_str) else 1

    if today_str not in daily_log:
        daily_log.append(today_str)

    # +10 XP per day, capped at level 5 max (700 total XP)
    XP_PER_DAY = 10
    XP_CAP = 700
    new_xp = min(current_xp + XP_PER_DAY, XP_CAP)

    cur.execute(
        "UPDATE users SET streak = %s, last_seen_date = %s, daily_log = %s, xp = %s WHERE discord_id = %s",
        (new_streak, today_str, daily_log, new_xp, discord_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_videos(player=None, agent=None, map_name=None, limit=20, offset=0):
    """Fetch videos with optional filters and pagination. Only returns complete entries."""
    conn = get_db()
    cur = conn.cursor()

    query = "SELECT video_id, player, agent, map, channel, created_at FROM videos WHERE player IS NOT NULL AND agent IS NOT NULL AND map IS NOT NULL"
    params = []

    if player:
        query += " AND LOWER(player) = LOWER(%s)"
        params.append(player)
    if agent:
        query += " AND LOWER(agent) = LOWER(%s)"
        params.append(agent)
    if map_name:
        query += " AND LOWER(map) = LOWER(%s)"
        params.append(map_name)

    query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [
        {
            "video_id": r[0],
            "player": r[1],
            "agent": r[2],
            "map": r[3],
            "channel": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


def count_videos(player=None, agent=None, map_name=None):
    """Count total videos matching filters."""
    conn = get_db()
    cur = conn.cursor()

    query = "SELECT COUNT(*) FROM videos WHERE player IS NOT NULL AND agent IS NOT NULL AND map IS NOT NULL"
    params = []

    if player:
        query += " AND LOWER(player) = LOWER(%s)"
        params.append(player)
    if agent:
        query += " AND LOWER(agent) = LOWER(%s)"
        params.append(agent)
    if map_name:
        query += " AND LOWER(map) = LOWER(%s)"
        params.append(map_name)

    cur.execute(query, params)
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    return total


def get_filter_options():
    """Return distinct agents/maps/players for filter dropdowns."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT agent FROM videos WHERE agent IS NOT NULL ORDER BY agent"
    )
    agents = [r[0] for r in cur.fetchall()]
    cur.execute(
        "SELECT DISTINCT map FROM videos WHERE map IS NOT NULL ORDER BY map"
    )
    maps = [r[0] for r in cur.fetchall()]
    cur.execute(
        "SELECT DISTINCT player FROM videos WHERE player IS NOT NULL ORDER BY player"
    )
    players = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"agents": agents, "maps": maps, "players": players}


# ────────────────────────────────────────────
# Discord helpers
# ────────────────────────────────────────────
def grant_verified_role(user_id):
    url = f"{DISCORD_API}/guilds/{GUILD_ID}/members/{user_id}/roles/{VERIFIED_ROLE_ID}"
    res = requests.put(
        url,
        headers={
            "Authorization": f"Bot {BOT_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    return res.status_code in (200, 204)


def member_has_role(user_id):
    """Return True if the user is in the guild AND already has the verified role."""
    url = f"{DISCORD_API}/guilds/{GUILD_ID}/members/{user_id}"
    res = requests.get(url, headers={"Authorization": f"Bot {BOT_TOKEN}"})
    if res.status_code != 200:
        return False
    roles = res.json().get("roles", [])
    return VERIFIED_ROLE_ID in roles


def fetch_discord_user(access_token):
    res = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if res.status_code != 200:
        return None
    return res.json()


# ────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/login")
def login():
    params = (
        f"client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPES.replace(' ', '%20')}"
    )
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")


@app.route("/api/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect(f"{FRONTEND_URL}?error=no_code")

    token_res = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if token_res.status_code != 200:
        return redirect(f"{FRONTEND_URL}?error=token_failed")

    access_token = token_res.json().get("access_token")

    user = fetch_discord_user(access_token)
    if not user:
        return redirect(f"{FRONTEND_URL}?error=user_fetch_failed")

    avatar_url = None
    if user.get("avatar"):
        avatar_url = f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png"

    upsert_user(
        discord_id=user["id"],
        username=user["username"],
        global_name=user.get("global_name"),
        avatar=avatar_url,
        email=user.get("email"),
    )

    grant_verified_role(user["id"])

    session_token = create_session_token(user["id"])
    return redirect(f"{FRONTEND_URL}/?token={session_token}")


@app.route("/api/me")
def me():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "unauthorized"}), 401

    token = auth.split(" ", 1)[1]

    discord_id = decode_session_token(token)
    if not discord_id:
        return jsonify({"error": "session_expired"}), 401

    update_streak(discord_id)

    db_user = get_user(discord_id)
    if not db_user:
        return jsonify({"error": "user_not_found"}), 404

    # Safety net: if user joined the guild after logging in on site,
    # make sure they have the verified role. Cheap no-op if already granted.
    if BOT_TOKEN and not member_has_role(discord_id):
        grant_verified_role(discord_id)

    return jsonify(db_user)


@app.route("/api/videos")
def videos():
    """Return VODs for ProView. Supports ?player=X&agent=Y&map=Z&limit=N&offset=N."""
    player = request.args.get("player")
    agent = request.args.get("agent")
    map_name = request.args.get("map")

    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        limit, offset = 20, 0

    results = get_videos(
        player=player, agent=agent, map_name=map_name, limit=limit, offset=offset
    )
    return jsonify(results)


@app.route("/api/videos/filters")
def videos_filters():
    """Return distinct agents/maps/players for filter dropdowns."""
    return jsonify(get_filter_options())


@app.route("/api/videos/count")
def videos_count():
    """Return total number of videos matching filters."""
    player = request.args.get("player")
    agent = request.args.get("agent")
    map_name = request.args.get("map")
    return jsonify({"total": count_videos(player=player, agent=agent, map_name=map_name)})


# ────────────────────────────────────────────
# Onboarding
# ────────────────────────────────────────────
def _get_authed_user():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, (jsonify({"error": "unauthorized"}), 401)
    discord_id = decode_session_token(auth.split(" ", 1)[1])
    if not discord_id:
        return None, (jsonify({"error": "session_expired"}), 401)
    return discord_id, None


import re

NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


@app.route("/api/check-nick/<nick>")
def check_nick(nick):
    if not NICK_RE.match(nick):
        return jsonify({"available": False, "reason": "Invalid format. 3-20 chars, letters/numbers/underscore only."})
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE LOWER(vantage_nick) = LOWER(%s)", (nick,))
    taken = cur.fetchone() is not None
    cur.close()
    conn.close()
    if taken:
        return jsonify({"available": False, "reason": "This nick is already taken."})
    return jsonify({"available": True})


@app.route("/api/onboarding/nick", methods=["POST"])
def set_nick():
    discord_id, err = _get_authed_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    nick = (data.get("nick") or "").strip()
    if not NICK_RE.match(nick):
        return jsonify({"error": "Invalid nick. 3-20 chars, letters/numbers/underscore only."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE LOWER(vantage_nick) = LOWER(%s) AND discord_id != %s", (nick, discord_id))
    if cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"error": "Nick already taken."}), 409
    cur.execute("UPDATE users SET vantage_nick = %s WHERE discord_id = %s", (nick, discord_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "nick": nick})


@app.route("/api/onboarding/avatar", methods=["POST"])
def set_avatar():
    discord_id, err = _get_authed_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    avatar_data = data.get("avatar")
    if not avatar_data:
        return jsonify({"error": "No avatar provided."}), 400
    if len(avatar_data) > 500_000:
        return jsonify({"error": "Avatar too large (max ~350KB)."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET custom_avatar = %s WHERE discord_id = %s", (avatar_data, discord_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/onboarding/complete", methods=["POST"])
def complete_onboarding():
    discord_id, err = _get_authed_user()
    if err:
        return err
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT vantage_nick FROM users WHERE discord_id = %s", (discord_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        cur.close()
        conn.close()
        return jsonify({"error": "Set a nick first."}), 400
    cur.execute("UPDATE users SET onboarding_complete = true WHERE discord_id = %s", (discord_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


# ────────────────────────────────────────────
# Subscription
# ────────────────────────────────────────────
VALID_AGENTS = [
    "Jett", "Reyna", "Raze", "Phoenix", "Neon", "Yoru", "Iso",
    "Sage", "Skye", "Killjoy", "Cypher", "Chamber", "Deadlock",
    "Gekko", "Fade", "Sova", "Breach", "KAYO", "Tejo",
    "Omen", "Brimstone", "Viper", "Astra", "Harbor", "Clove", "Miks",
    "Vyse", "Waylay", "Veto",
]


@app.route("/api/me/subscription", methods=["GET"])
def get_subscription():
    discord_id, err = _get_authed_user()
    if err:
        return err
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT subscribed_agent FROM users WHERE discord_id = %s", (discord_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify({"subscribed_agent": row[0] if row else None})


@app.route("/api/me/subscription", methods=["POST"])
def set_subscription():
    discord_id, err = _get_authed_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    agent = (data.get("agent") or "").strip()
    if agent not in VALID_AGENTS:
        return jsonify({"error": f"Invalid agent. Choose from: {', '.join(VALID_AGENTS)}"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET subscribed_agent = %s WHERE discord_id = %s", (agent, discord_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "subscribed_agent": agent})


@app.route("/api/me/subscription", methods=["DELETE"])
def delete_subscription():
    discord_id, err = _get_authed_user()
    if err:
        return err
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET subscribed_agent = NULL WHERE discord_id = %s", (discord_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "subscribed_agent": None})


# ────────────────────────────────────────────
# Startup
# ────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)