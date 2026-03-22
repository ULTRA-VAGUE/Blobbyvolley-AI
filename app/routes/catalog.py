import logging
from urllib.parse import unquote
from quart import Blueprint, abort, current_app
from app.services.db import get_valid_user
from app.services.kitsu_client import KitsuClient
from .manifest import MANIFEST
from .utils import respond_with

catalog_bp = Blueprint("catalog", __name__)
logger = logging.getLogger(__name__)

def _parse_stremio_filters(extra: str | None) -> dict:
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
    if catalog_type != "anime" or catalog_id not in valid_ids: abort(404)
    user, error = await get_valid_user(user_id)
    if error: return await respond_with({"metas": []}, stremio_response=True)
    filters = _parse_stremio_filters(extras)
    access_token = user.get("access_token")
    stremio_metas = []
    try:
        if catalog_id == "kitsu_search":
            search_query = filters.get("search")
            if not search_query: return await respond_with({"metas": []}, stremio_response=True)
            data = await KitsuClient.search_anime(search_query, access_token)
            items = data.get("data", [])
        else:
            offset = int(filters.get("skip", 0))
            data = await KitsuClient.get_library_catalog(user.get("id"), catalog_id, offset, access_token)
            items = data.get("data", [])
            included = data.get("included", [])
            anime_dict = {i["id"]: i.get("attributes", {}) for i in included if i.get("type") == "anime"}

        for item in items:
            if catalog_id == "kitsu_search":
                attrs = item.get("attributes", {})
                a_id = item.get("id")
            else:
                rel = item.get("relationships", {}).get("anime", {}).get("data")
                if not rel: continue
                a_id = rel.get("id")
                attrs = anime_dict.get(a_id)
            if not attrs: continue
            k_type = attrs.get("subtype", "TV")
            stremio_metas.append({
                "id": f"kitsu:{a_id}",
                "type": "movie" if k_type == "movie" else "series",
                "name": attrs.get("canonicalTitle") or attrs.get("titles", {}).get("en_jp", "Unknown"),
                "poster": (attrs.get("posterImage") or {}).get("large", ""),
                "description": attrs.get("synopsis") or ""
            })
        return await respond_with({"metas": stremio_metas}, stremio_response=True)
    except Exception as e:
        logger.error(f"Catalog Error: {e}")
        return await respond_with({"metas": []}, stremio_response=True)

@catalog_bp.route("/<user_id>/meta/<string:catalog_type>/<string:stremio_id>.json")
async def addon_meta(user_id: str, catalog_type: str, stremio_id: str):
    if catalog_type not in ["anime", "series", "movie"] or not stremio_id.startswith("kitsu:"):
        return await respond_with({"meta": {}}, stremio_response=True)

    user, error = await get_valid_user(user_id)
    if error: return await respond_with({"meta": {}}, stremio_response=True)

    anime_id = stremio_id.split(":")[1]
    access_token = user.get("access_token")

    try:
        # 1. Basis-Informationen laden
        resp = await KitsuClient.get_anime_with_mappings(anime_id, access_token)
        data = resp.get("data", {})
        attrs = data.get("attributes", {})
        included = resp.get("included", [])

        # IMDb/MAL IDs extrahieren
        imdb_id = next((m["attributes"]["externalId"] for m in included if m["type"] == "mappings" and m["attributes"]["externalSite"] == "imdb/anime"), None)
        mal_id = next((m["attributes"]["externalId"] for m in included if m["type"] == "mappings" and m["attributes"]["externalSite"] == "myanimelist/anime"), None)

        title = attrs.get("canonicalTitle") or attrs.get("titles", {}).get("en_jp", "Unknown")
        poster = (attrs.get("posterImage") or {}).get("large", "")
        
        meta = {
            "id": stremio_id,
            "type": "movie" if attrs.get("subtype") == "movie" else "series",
            "name": title,
            "poster": poster,
            "background": (attrs.get("coverImage") or {}).get("large", ""),
            "logo": poster,
            "description": attrs.get("synopsis", ""),
            "releaseInfo": attrs.get("startDate", "")[:4] if attrs.get("startDate") else "",
            "runtime": f"{attrs.get('episodeLength')} min" if attrs.get('episodeLength') else None,
            "genres": [g["attributes"]["title"] for g in included if g["type"] == "genres"],
            "imdb_id": imdb_id,
            "mal_id": mal_id
        }

        # 2. Episoden laden (Robust gebaut)
        if meta["type"] == "series":
            videos = []
            try:
                ep_resp = await KitsuClient.get_anime_episodes(anime_id, access_token)
                ep_list = ep_resp.get("data", [])
                for ep in ep_list:
                    e_attrs = ep.get("attributes", {})
                    num = e_attrs.get("number")
                    videos.append({
                        "id": f"{stremio_id}:{num}", # Fix: Stremio-konforme ID Formatierung
                        "title": e_attrs.get("canonicalTitle") or f"Episode {num}",
                        "season": 1,
                        "episode": num,
                        "released": e_attrs.get("airdate")
                    })
            except Exception as e:
                logger.warning(f"Could not fetch real episode titles for {anime_id}: {e}")

            # Fallback falls API keine Episoden liefert
            if not videos:
                count = attrs.get("episodeCount") or 1
                for i in range(1, count + 1):
                    videos.append({"id": f"{stremio_id}:{i}", "title": f"Episode {i}", "season": 1, "episode": i})
            
            meta["videos"] = videos

        return await respond_with({"meta": meta}, cache_max_age=86400, stremio_response=True)
    except Exception as e:
        logger.error(f"Meta Error: {e}")
        return await respond_with({"meta": {}}, stremio_response=True)
