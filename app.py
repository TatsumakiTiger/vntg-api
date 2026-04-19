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
            discord_id  TEXT PRIMARY KEY,
            username    TEXT,
            global_name TEXT,
            avatar      TEXT,
            email       TEXT,
            created_at  TIMESTAMP DEFAULT now(),
            last_login  TIMESTAMP DEFAULT now()
        )
        """
    )
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
        "SELECT discord_id, username, global_name, avatar, email, created_at, last_login FROM users WHERE discord_id = %s",
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
    }


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
    return redirect(f"{FRONTEND_URL}/dashboard?token={session_token}")


@app.route("/api/me")
def me():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "unauthorized"}), 401

    token = auth.split(" ", 1)[1]

    discord_id = decode_session_token(token)
    if not discord_id:
        return jsonify({"error": "session_expired"}), 401

    db_user = get_user(discord_id)
    if not db_user:
        return jsonify({"error": "user_not_found"}), 404

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


# ────────────────────────────────────────────
# Startup
# ────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)