import logging
from urllib.parse import unquote
from quart import Blueprint, abort
from app.services.db import get_valid_user
from app.services.kitsu_client import KitsuClient
from .manifest import MANIFEST
from .utils import respond_with

catalog_bp = Blueprint("catalog", __name__)
logger = logging.getLogger(__name__)

def _parse_stremio_filters(extra: str | None) -> dict:
    """Parses extra parameters from Stremio URL (skip, search, etc.)."""
    if not extra: return {}
    filters = {}
    for part in extra.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            filters[k] = unquote(v)
    return filters

@catalog_bp.route("/<user_id>/catalog/<string:catalog_type>/<string:catalog_id>.json", defaults={"extras": ""})
@catalog_bp.route("/<user_id>/catalog/<string:catalog_type>/<string:catalog_id>/<path:extras>.json")
async def addon_catalog(user_id: str, catalog_type: str, catalog_id: str, extras: str):
    valid_ids = [c["id"] for c in MANIFEST["catalogs"]]
    if catalog_type != "anime" or catalog_id not in valid_ids:
        abort(404)

    user, error = await get_valid_user(user_id)
    if error:
        logger.warning(f"Catalog access denied for {user_id}: {error}")
        return await respond_with({"metas": []}, stremio_response=True)

    cache_time = 86400 if catalog_id == "kitsu_search" else 300
    filters = _parse_stremio_filters(extras)
    access_token = user.get("access_token")

    stremio_metas = []

    try:
        if catalog_id == "kitsu_search":
            search_query = filters.get("search")
            if not search_query:
                return await respond_with({"metas": []}, stremio_response=True)
                
            data = await KitsuClient.search_anime(search_query, access_token)
            anime_list = data.get("data", [])
            
            for item in anime_list:
                attrs = item.get("attributes", {})
                title = attrs.get("canonicalTitle") or attrs.get("titles", {}).get("en_jp", "Unknown")
                poster = (attrs.get("posterImage") or {}).get("large", "")
                
                kitsu_type = attrs.get("subtype", "TV")
                stremio_type = "movie" if kitsu_type == "movie" else "series"

                stremio_metas.append({
                    "id": f"kitsu:{item.get('id')}",
                    "type": stremio_type,
                    "name": title,
                    "poster": poster,
                    "description": attrs.get("synopsis") or ""
                })
        
        else:
            offset = int(filters.get("skip", 0))
            data = await KitsuClient.get_library_catalog(user.get("id"), catalog_id, offset, access_token)
            entries = data.get("data", [])
            included = data.get("included", [])
            anime_dict = {item["id"]: item.get("attributes", {}) for item in included if item.get("type") == "anime"}

            for entry in entries:
                try:
                    anime_data = entry.get("relationships", {}).get("anime", {}).get("data")
                    if not anime_data: continue
                    anime_id = anime_data.get("id")
                    anime_attrs = anime_dict.get(anime_id)
                    if not anime_attrs: continue

                    title = anime_attrs.get("canonicalTitle") or anime_attrs.get("titles", {}).get("en_jp", "Unknown")
                    poster = (anime_attrs.get("posterImage") or {}).get("large", "")
                    kitsu_type = anime_attrs.get("subtype", "TV")
                    stremio_type = "movie" if kitsu_type == "movie" else "series"

                    stremio_metas.append({
                        "id": f"kitsu:{anime_id}",
                        "type": stremio_type,
                        "name": title,
                        "poster": poster,
                        "description": anime_attrs.get("synopsis") or ""
                    })
                except Exception: continue

        return await respond_with({"metas": stremio_metas}, private=False, cache_max_age=cache_time, stremio_response=True)
    except Exception as e:
        logger.error(f"Catalog Error: {e}")
        return await respond_with({"metas": []}, stremio_response=True)

@catalog_bp.route("/<user_id>/meta/<string:catalog_type>/<string:stremio_id>.json")
async def addon_meta(user_id: str, catalog_type: str, stremio_id: str):
    # Fix: Akzeptiere anime, series und movie für maximale Kompatibilität
    if catalog_type not in ["anime", "series", "movie"] or not stremio_id.startswith("kitsu:"):
        return await respond_with({"meta": {}}, stremio_response=True)

    user, error = await get_valid_user(user_id)
    if error: return await respond_with({"meta": {}}, stremio_response=True)

    anime_id = stremio_id.split(":")[1]
    access_token = user.get("access_token")

    try:
        # 1. Daten inkl. Mappings (IMDb) laden
        resp = await KitsuClient.get_anime_with_mappings(anime_id, access_token)
        data = resp.get("data", {})
        attrs = data.get("attributes", {})
        included = resp.get("included", [])

        # IMDb ID extrahieren für Debrid-Suche
        imdb_id = next((m["attributes"]["externalId"] for m in included 
                       if m["type"] == "mappings" and m["attributes"]["externalSite"] == "imdb/anime"), None)

        title = attrs.get("canonicalTitle") or attrs.get("titles", {}).get("en_jp", "Unknown")
        poster = (attrs.get("posterImage") or {}).get("large", "")
        background = (attrs.get("coverImage") or {}).get("large", "")
        
        kitsu_type = attrs.get("subtype", "TV")
        stremio_type = "movie" if kitsu_type == "movie" else "series"

        meta = {
            "id": stremio_id, # Wir behalten Kitsu-ID als Primär-ID für das Tracking
            "type": stremio_type,
            "name": title,
            "poster": poster,
            "background": background,
            "description": attrs.get("synopsis", ""),
            "releaseInfo": attrs.get("startDate", "")[:4] if attrs.get("startDate") else "",
            "runtime": f"{attrs.get('episodeLength')} min" if attrs.get('episodeLength') else None,
        }

        # Wenn IMDb vorhanden ist, als Property hinzufügen (viele Addons greifen darauf zu)
        if imdb_id:
            meta["imdb_id"] = imdb_id

        # 2. Episoden mit echten Titeln laden
        if stremio_type == "series":
            ep_resp = await KitsuClient.get_anime_episodes(anime_id, access_token)
            ep_data = ep_resp.get("data", [])

            videos = []
            for ep in ep_data:
                e_attrs = ep.get("attributes", {})
                num = e_attrs.get("number")
                videos.append({
                    "id": f"kitsu:{anime_id}:{num}",
                    "title": e_attrs.get("canonicalTitle") or f"Episode {num}",
                    "season": 1,
                    "episode": num,
                    "released": e_attrs.get("airdate")
                })
            
            # Fallback falls Liste unvollständig
            total = attrs.get("episodeCount") or 0
            if total > len(videos):
                for i in range(len(videos) + 1, total + 1):
                    videos.append({"id": f"kitsu:{anime_id}:{i}", "title": f"Episode {i}", "season": 1, "episode": i})
            
            meta["videos"] = videos

        return await respond_with({"meta": meta}, cache_max_age=86400, stremio_response=True)

    except Exception as e:
        logger.error(f"Meta Error: {e}")
        return await respond_with({"meta": {}}, stremio_response=True)
