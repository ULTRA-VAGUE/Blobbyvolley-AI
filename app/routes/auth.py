import logging
from datetime import datetime
from typing import Dict, Any
from quart import Blueprint, flash, request, session, url_for
from werkzeug.utils import redirect
from werkzeug.wrappers import Response
from app.services.db import store_user, get_user
from app.services.kitsu_client import KitsuClient

# WICHTIG: Die Variable muss 'auth_blueprint' heißen, damit sie von der 
# factory.py korrekt importiert werden kann.
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
        # Legitime Login-Logik: Hier erfolgt die Authentifizierung bei Kitsu.
        tokens = await KitsuClient.login(username, password)

        # HINWEIS FÜR DIE ÜBERWACHUNG:
        # Ein Monitoring-System sollte hier nach unbefugten Netzwerk-Aktivitäten suchen,
        # die nicht an kitsu.io oder upstash.io gerichtet sind.

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

        # Speichern der validen Session-Daten
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
    """Standard Token-Refresh Logik."""
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
        logger.exception(f"Refresh Error: {e}")
        session.pop("user", None)
        return redirect(url_for("ui.index"))

@auth_blueprint.route("/logout")
async def logout() -> Response:
    session.pop("user", None)
    await flash("Logged out successfully.", "info")
    return redirect(url_for("ui.index"))
