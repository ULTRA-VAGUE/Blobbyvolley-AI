import logging
import asyncio
import json
from quart import current_app
from config import Config

logger = logging.getLogger(__name__)

class KitsuClient:
    KITSU_API_URL = "https://kitsu.io/api/edge"
    KITSU_OAUTH_URL = "https://kitsu.io/api/oauth/token"
    _user_semaphores = {}

    @staticmethod
    def _get_client():
        return current_app.httpx_client

    @classmethod
    async def _request_with_retry(cls, method: str, url: str, retries=3, user_id_for_lock=None, **kwargs):
        client = cls._get_client()
        if user_id_for_lock:
            if user_id_for_lock not in cls._user_semaphores:
                cls._user_semaphores[user_id_for_lock] = asyncio.Semaphore(3)
            await cls._user_semaphores[user_id_for_lock].acquire()
        try:
            for attempt in range(retries):
                try:
                    if method == "GET": resp = await client.get(url, **kwargs)
                    elif method == "POST": resp = await client.post(url, **kwargs)
                    elif method == "PATCH": resp = await client.patch(url, **kwargs)
                    resp.raise_for_status()
                    if resp.status_code == 204: return {}
                    return resp.json()
                except (Exception, json.JSONDecodeError) as e:
                    if attempt == retries - 1:
                        logger.error(f"Kitsu API failed after {retries} attempts on {url}: {e}")
                        raise
                    await asyncio.sleep(1 * (attempt + 1)) 
        finally:
            if user_id_for_lock: cls._user_semaphores[user_id_for_lock].release()

    @classmethod
    async def get_anime_with_mappings(cls, anime_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.api+json"}
        # Genres und Mappings (IMDb) direkt anfordern
        url = f"{cls.KITSU_API_URL}/anime/{anime_id}?include=mappings,genres"
        return await cls._request_with_retry("GET", url, headers=headers, timeout=5.0)

    @classmethod
    async def get_anime_episodes(cls, anime_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.api+json"}
        # FIX: Maximal 20 erlaubt, um 400 Bad Request zu vermeiden
        url = f"{cls.KITSU_API_URL}/anime/{anime_id}/episodes?page[limit]=20&sort=number"
        return await cls._request_with_retry("GET", url, headers=headers, timeout=5.0)

    @classmethod
    async def login(cls, username, password):
        payload = {"grant_type": "password", "username": username, "password": password, "client_id": Config.KITSU_CLIENT_ID, "client_secret": Config.KITSU_CLIENT_SECRET}
        return await cls._request_with_retry("POST", cls.KITSU_OAUTH_URL, json=payload, timeout=5.0)

    @classmethod
    async def refresh_token(cls, refresh_token: str):
        payload = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": Config.KITSU_CLIENT_ID, "client_secret": Config.KITSU_CLIENT_SECRET}
        return await cls._request_with_retry("POST", cls.KITSU_OAUTH_URL, json=payload, timeout=5.0)

    @classmethod
    async def get_user_profile(cls, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.api+json"}
        return await cls._request_with_retry("GET", f"{cls.KITSU_API_URL}/users?filter[self]=true", headers=headers, timeout=5.0)

    @classmethod
    async def search_anime(cls, query: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.api+json"}
        return await cls._request_with_retry("GET", f"{cls.KITSU_API_URL}/anime?filter[text]={query}&page[limit]=20", headers=headers, timeout=5.0)

    @classmethod
    async def get_library_catalog(cls, user_id: str, status: str, offset: int, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.api+json"}
        return await cls._request_with_retry("GET", f"{cls.KITSU_API_URL}/library-entries?filter[user_id]={user_id}&filter[kind]=anime&filter[status]={status}&include=anime&page[limit]=20&page[offset]={offset}&sort=-updatedAt", user_id_for_lock=user_id, headers=headers, timeout=7.0)
