import logging
from quart import Blueprint
from app.services.db import get_valid_user, update_user_progress
from app.services.kitsu_client import KitsuClient
from .utils import respond_with

content_sync_bp = Blueprint("content_sync", __name__)
logger = logging.getLogger(__name__)

@content_sync_bp.route("/<auth_id>/subtitles/<catalog_type>/<stremio_id>.json")
async def sync_progress(auth_id: str, catalog_type: str, stremio_id: str):
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:04.000\nKitsu: Sync sent"
    res = {"subtitles": [{"id": "kitsu-sync", "url": f"data:text/vtt;charset=utf-8,{vtt}", "lang": "Kitsu Sync"}]}
    
    user, error = await get_valid_user(auth_id)
    if error or not user: return await respond_with(res, stremio_response=True)

    parts = stremio_id.split(":")
    # Falls IMDb ID (tt...), müssen wir sie erst auflösen
    anime_id = None
    episode = 1
    
    if stremio_id.startswith("tt"):
        imdb_id = parts[0]
        episode = int(parts[2]) if len(parts) >= 3 else 1
        # Kitsu ID über Mapping suchen
        mapping = await KitsuClient.get_anime_by_external_id(imdb_id, user["access_token"])
        if mapping.get("data"):
            anime_id = mapping["data"][0]["relationships"]["item"]["data"]["id"]
    elif stremio_id.startswith("kitsu"):
        anime_id = parts[1]
        episode = int(parts[3]) if len(parts) >= 4 else int(parts[2]) if len(parts) == 3 else 1

    if not anime_id: return await respond_with(res, stremio_response=True)

    try:
        # Progress Update Logik
        anime_data = await KitsuClient.get_anime(anime_id, user["access_token"])
        total = anime_data.get("data", {}).get("attributes", {}).get("episodeCount")
        status = "completed" if total and episode >= total else "current"
        
        search = await KitsuClient.search_library_entries(user["id"], anime_id, user["access_token"])
        entries = search.get("data", [])

        if entries:
            await KitsuClient.update_library_entry(entries[0]["id"], episode, status, user["access_token"])
        else:
            await KitsuClient.create_library_entry(user["id"], anime_id, episode, status, user["access_token"])

        await update_user_progress(user, anime_id, episode)
        logger.info(f"Synced: {anime_id} Ep {episode}")
    except Exception as e:
        logger.error(f"Sync Error: {e}")

    return await respond_with(res, stremio_response=True)
