import os
import requests
import psycopg2
from datetime import datetime, timezone
from flask import Flask, redirect, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://www.vntg.com.pl")
CORS(app, origins=[FRONTEND_URL], supports_credentials=True)

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
REDIRECT_URI = os.environ.get(
    "DISCORD_REDIRECT_URI",
    "https://vntg-api-production.up.railway.app/api/callback",
)

DISCORD_API = "https://discord.com/api/v10"
SCOPES = "identify email guilds"

GUILD_ID = "1477669827547103365"
VERIFIED_ROLE_ID = "1491125641226227742"


# ────────────────────────────────────────────
# Database helpers
# ────────────────────────────────────────────
def get_db():
    """Return a new database connection."""
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Create the users table if it doesn't exist."""
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
    conn.commit()
    cur.close()
    conn.close()


def upsert_user(discord_id, username, global_name, avatar, email):
    """Insert a new user or update an existing one."""
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
    """Fetch a user row by discord_id."""
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


# ────────────────────────────────────────────
# Discord helpers
# ────────────────────────────────────────────
def grant_verified_role(user_id):
    """Add the Verified role to a guild member via Bot API."""
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
    """Get the current user from Discord using their OAuth2 token."""
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

    # Exchange code for access token
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

    # Fetch user info from Discord
    user = fetch_discord_user(access_token)
    if not user:
        return redirect(f"{FRONTEND_URL}?error=user_fetch_failed")

    # Build avatar URL
    avatar_url = None
    if user.get("avatar"):
        avatar_url = f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png"

    # Save / update user in database
    upsert_user(
        discord_id=user["id"],
        username=user["username"],
        global_name=user.get("global_name"),
        avatar=avatar_url,
        email=user.get("email"),
    )

    # Grant Verified role on Discord server
    grant_verified_role(user["id"])

    # Redirect to frontend dashboard WITH token
    return redirect(f"{FRONTEND_URL}/dashboard?token={access_token}")


@app.route("/api/me")
def me():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "unauthorized"}), 401

    token = auth.split(" ", 1)[1]

    # First get discord_id from Discord API (we need it to look up our DB)
    discord_user = fetch_discord_user(token)
    if not discord_user:
        return jsonify({"error": "discord_error"}), 401

    # Then fetch full user data from our database
    db_user = get_user(discord_user["id"])
    if not db_user:
        return jsonify({"error": "user_not_found"}), 404

    return jsonify(db_user)


# ────────────────────────────────────────────
# Startup
# ────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)