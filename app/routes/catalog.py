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
    if not stremio_id.startswith(("kitsu:", "tt")):
        return await respond_with({"meta": {}}, stremio_response=True)

    user, error = await get_valid_user(user_id)
    if error: return await respond_with({"meta": {}}, stremio_response=True)

    anime_id = stremio_id.split(":")[1] if stremio_id.startswith("kitsu:") else None
    
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

        imdb_id = next((m["attributes"]["externalId"] for m in included if m["type"] == "mappings" and m["attributes"]["externalSite"] == "imdb/anime"), None)
        
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

        if meta["type"] == "series":
            videos = []
            try:
                ep_resp = await KitsuClient.get_anime_episodes(anime_id, user["access_token"])
                for ep in ep_resp.get("data", []):
                    num = ep["attributes"].get("number")
                    vid_id = f"{imdb_id}:1:{num}" if imdb_id else f"kitsu:{anime_id}:1:{num}"
                    videos.append({
                        "id": vid_id,
                        "title": ep["attributes"].get("canonicalTitle") or f"Episode {num}",
                        "season": 1,
                        "episode": num,
                        "released": ep["attributes"].get("airdate")
                    })
            except Exception: pass

            if not videos:
                for i in range(1, (attrs.get("episodeCount") or 1) + 1):
                    vid_id = f"{imdb_id}:1:{i}" if imdb_id else f"kitsu:{anime_id}:1:{i}"
                    videos.append({"id": vid_id, "title": f"Episode {i}", "season": 1, "episode": i})
            
            meta["videos"] = videos

        return await respond_with({"meta": meta}, cache_max_age=86400, stremio_response=True)
    except Exception as e:
        logger.error(f"Meta Error: {e}")
        return await respond_with({"meta": {}}, stremio_response=True)
