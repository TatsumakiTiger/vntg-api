import os
import requests
from flask import Flask, redirect, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, supports_credentials=True)

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://www.vntg.com.pl")
REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "https://vntg-api-production.up.railway.app/api/callback")

DISCORD_API = "https://discord.com/api/v10"
SCOPES = "identify email guilds"


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
    return redirect(f"{FRONTEND_URL}/dashboard?token={access_token}")


@app.route("/api/me")
def me():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "unauthorized"}), 401

    token = auth.split(" ", 1)[1]

    user_res = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {token}"},
    )

    if user_res.status_code != 200:
        return jsonify({"error": "discord_error"}), 401

    user = user_res.json()
    return jsonify({
        "id": user["id"],
        "username": user["username"],
        "global_name": user.get("global_name"),
        "avatar": f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png" if user.get("avatar") else None,
        "email": user.get("email"),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)