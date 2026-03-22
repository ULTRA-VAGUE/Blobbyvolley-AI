import logging
from urllib.parse import unquote
from quart import Blueprint, abort, current_app
from app.services.db import get_valid_user
from app.services.kitsu_client import KitsuClient
from .utils import respond_with

catalog_bp = Blueprint("catalog", __name__)
logger = logging.getLogger(__name__)

@catalog_bp.route("/<user_id>/meta/<string:catalog_type>/<string:stremio_id>.json")
async def addon_meta(user_id: str, catalog_type: str, stremio_id: str):
    if not stremio_id.startswith(("kitsu:", "tt")):
        return await respond_with({"meta": {}}, stremio_response=True)

    user, error = await get_valid_user(user_id)
    if error: return await respond_with({"meta": {}}, stremio_response=True)

    # Wir brauchen immer die Kitsu-ID für die API
    anime_id = stremio_id.split(":")[1] if stremio_id.startswith("kitsu:") else None
    
    # Falls Stremio uns mit einer tt ID fragt, müssen wir sie erst auflösen
    if stremio_id.startswith("tt"):
        map_resp = await KitsuClient.get_anime_by_external_id(stremio_id, user["access_token"])
        if map_resp.get("data"):
            anime_id = map_resp["data"][0]["relationships"]["item"]["data"]["id"]
    
    if not anime_id: return await respond_with({"meta": {}}, stremio_response=True)

    try:
        resp = await KitsuClient.get_anime_with_mappings(anime_id, user["access_token"])
        data = resp.get("data", {})
        attrs = data.get("attributes", {})
        included = resp.get("included", [])

        # IMDb-ID finden für Debrid
        imdb_id = next((m["attributes"]["externalId"] for m in included if m["type"] == "mappings" and m["attributes"]["externalSite"] == "imdb/anime"), None)
        
        # Stabiles Mapping für Meta
        meta = {
            "id": imdb_id if imdb_id else f"kitsu:{anime_id}",
            "type": "movie" if attrs.get("subtype") == "movie" else "series",
            "name": attrs.get("canonicalTitle") or "Unknown Anime",
            "poster": (attrs.get("posterImage") or {}).get("large", ""),
            "background": (attrs.get("coverImage") or {}).get("large", ""),
            "description": attrs.get("synopsis", ""),
            "releaseInfo": attrs.get("startDate", "")[:4] if attrs.get("startDate") else "",
            "runtime": f"{attrs.get('episodeLength')} min" if attrs.get('episodeLength') else None,
            "genres": [g["attributes"].get("name") or g["attributes"].get("title") for g in included if g["type"] == "genres" and "attributes" in g],
            "imdb_id": imdb_id
        }

        # Episoden-Fix (Season 1 erzwingen)
        if meta["type"] == "series":
            videos = []
            try:
                ep_resp = await KitsuClient.get_anime_episodes(anime_id, user["access_token"])
                for ep in ep_resp.get("data", []):
                    num = ep["attributes"].get("number")
                    # ID Brücke: tt...:1:num für Debrid, sonst kitsu:ID:1:num
                    vid_id = f"{imdb_id}:1:{num}" if imdb_id else f"kitsu:{anime_id}:1:{num}"
                    videos.append({
                        "id": vid_id,
                        "title": ep["attributes"].get("canonicalTitle") or f"Episode {num}",
                        "season": 1,
                        "episode": num,
                        "released": ep["attributes"].get("airdate")
                    })
            except Exception: pass

            if not videos: # Fallback
                for i in range(1, (attrs.get("episodeCount") or 1) + 1):
                    vid_id = f"{imdb_id}:1:{i}" if imdb_id else f"kitsu:{anime_id}:1:{i}"
                    videos.append({"id": vid_id, "title": f"Episode {i}", "season": 1, "episode": i})
            
            meta["videos"] = videos

        return await respond_with({"meta": meta}, cache_max_age=86400, stremio_response=True)
    except Exception as e:
        logger.error(f"Meta Error: {e}")
        return await respond_with({"meta": {}}, stremio_response=True)
