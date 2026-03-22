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
    
    # Validate Catalog ID against Manifest
    valid_ids = [c["id"] for c in MANIFEST["catalogs"]]
    if catalog_type != "anime" or catalog_id not in valid_ids:
        abort(404)

    # User Validation
    user, error = await get_valid_user(user_id)
    if error:
        logger.warning(f"Catalog access denied for {user_id}: {error}")
        return await respond_with({"metas": []}, stremio_response=True)

    # Caching Logic
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
                poster_img = attrs.get("posterImage") or {}
                poster = poster_img.get("large") if isinstance(poster_img, dict) else ""
                
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

            # Mapping
            anime_dict = {item["id"]: item.get("attributes", {}) for item in included if item.get("type") == "anime"}

            for entry in entries:
                try:
                    anime_data = entry.get("relationships", {}).get("anime", {}).get("data")
                    if not anime_data: continue
                        
                    anime_id = anime_data.get("id")
                    anime_attrs = anime_dict.get(anime_id)
                    if not anime_attrs: continue

                    # Fallbacks
                    title = anime_attrs.get("canonicalTitle") or anime_attrs.get("titles", {}).get("en_jp", "Unknown")
                    poster_img = anime_attrs.get("posterImage") or {}
                    poster = poster_img.get("large") if isinstance(poster_img, dict) else ""

                   
                    kitsu_type = anime_attrs.get("subtype", "TV")
                    stremio_type = "movie" if kitsu_type == "movie" else "series"

                    stremio_metas.append({
                        "id": f"kitsu:{anime_id}",
                        "type": stremio_type,
                        "name": title,
                        "poster": poster,
                        "description": anime_attrs.get("synopsis") or ""
                    })
                except Exception:
                    continue

        return await respond_with(
            {"metas": stremio_metas},
            private=False,
            cache_max_age=cache_time,
            stale_revalidate=0,           
            stremio_response=True
        )

    except Exception as e:
        logger.error(f"Catalog Error for user {user_id}: {e}")
        return await respond_with({"metas": []}, stremio_response=True)

@catalog_bp.route("/<user_id>/meta/<string:catalog_type>/<string:stremio_id>.json")
async def addon_meta(user_id: str, catalog_type: str, stremio_id: str):
    if catalog_type != "anime" or not stremio_id.startswith("kitsu:"):
        return await respond_with({"meta": {}}, stremio_response=True)

    user, error = await get_valid_user(user_id)
    if error:
        return await respond_with({"meta": {}}, stremio_response=True)

    anime_id = stremio_id.split(":")[1]
    access_token = user.get("access_token")

    try:
        # 1. Basis-Informationen laden (Authentifiziert -> 18+ Content wird nicht geblockt)
        response = await KitsuClient.get_anime(anime_id, access_token)
        anime_data = response.get("data", {}).get("attributes")

        if not anime_data:
            return await respond_with({"meta": {}}, stremio_response=True)

        title = anime_data.get("canonicalTitle") or anime_data.get("titles", {}).get("en_jp", "Unknown")
        poster_img = anime_data.get("posterImage") or {}
        bg_img = anime_data.get("coverImage") or {}
        
        kitsu_type = anime_data.get("subtype", "TV")
        stremio_type = "movie" if kitsu_type == "movie" else "series"

        meta = {
            "id": stremio_id,
            "type": stremio_type,
            "name": title,
            "poster": poster_img.get("large") if isinstance(poster_img, dict) else "",
            "background": bg_img.get("large") if isinstance(bg_img, dict) else "",
            "description": anime_data.get("synopsis", ""),
            "releaseInfo": anime_data.get("startDate", "")[:4] if anime_data.get("startDate") else ""
        }

        # 2. Episoden-Array (Videos) zusammenbauen
        if stremio_type == "series":
            episode_count = anime_data.get("episodeCount")
            
            # Falls die Serie ongoing ist und keinen episodeCount hat, holen wir die Nummer der neusten Episode
            if not episode_count:
                latest_ep_data = await KitsuClient.get_latest_episode(anime_id, access_token)
                if latest_ep_data and latest_ep_data.get("data"):
                    latest_ep = latest_ep_data["data"][0]
                    episode_count = latest_ep.get("attributes", {}).get("number")
            
            # Fallback, falls Kitsu absolut keine Daten liefert
            max_eps = episode_count if episode_count else 1
            
            videos = []
            for i in range(1, max_eps + 1):
                videos.append({
                    "id": f"kitsu:{anime_id}:{i}",
                    "title": f"Episode {i}",
                    "season": 1,
                    "episode": i
                })
                
            meta["videos"] = videos

        return await respond_with(
            {"meta": meta},
            private=False,
            cache_max_age=86400,
            stremio_response=True
        )

    except Exception as e:
        logger.error(f"Meta Error for {stremio_id}: {e}")
        return await respond_with({"meta": {}}, stremio_response=True)
