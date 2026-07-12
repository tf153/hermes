"""SerpAPI Google Maps helpers: a real photo per place, and a place-data
fallback for any destination (used when Linkup has no key or returns nothing).

Everything here is best-effort and returns empty/None on failure so the pipeline
degrades gracefully.
"""

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search"

# Persona-oriented categories; "{dest}" is filled with the destination.
CATEGORY_QUERIES: dict[str, str] = {
    "pilgrimage": "famous temples and churches in {dest}",
    "beaches": "best beaches in {dest}",
    "sunset": "best sunset viewpoints in {dest}",
    "cafes": "best cafes for slow mornings in {dest}",
    "trek": "forts, hills and viewpoints in {dest}",
    "food": "best local restaurants in {dest}",
    "family": "family friendly attractions in {dest} with kids",
    "nature": "waterfalls, parks and nature spots in {dest}",
}


async def _maps_search(client: httpx.AsyncClient, query: str) -> dict | None:
    if not settings.serpapi_api_key:
        return None
    try:
        resp = await client.get(
            SERPAPI_URL,
            params={
                "engine": "google_maps",
                "type": "search",
                "q": query,
                "hl": "en",
                "api_key": settings.serpapi_api_key,
            },
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("serpapi maps search failed (%s): %s", query, exc)
        return None


def _results(data: dict | None) -> list[dict]:
    if not data:
        return []
    if data.get("local_results"):
        return data["local_results"]
    if data.get("place_results"):
        return [data["place_results"]]
    return []


WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "HermesTravel/1.0 (+https://growthx.club)"}


async def _serp_photo(client: httpx.AsyncClient, name: str, destination: str) -> str | None:
    data = await _maps_search(client, f"{name}, {destination}")
    for place in _results(data)[:1]:
        if place.get("thumbnail"):
            return place["thumbnail"]
    return None


async def _wiki_photo(client: httpx.AsyncClient, name: str, destination: str) -> str | None:
    """Keyless representative image for a place from Wikipedia (best match)."""
    city = destination.split(",")[0].strip()
    for query in (f"{name} {city}", name):
        try:
            resp = await client.get(
                WIKI_API,
                params={
                    "action": "query", "format": "json", "generator": "search",
                    "gsrsearch": query, "gsrlimit": "1",
                    "prop": "pageimages", "piprop": "original|thumbnail", "pithumbsize": "1000",
                },
                headers=WIKI_HEADERS,
            )
            resp.raise_for_status()
            pages = (resp.json().get("query") or {}).get("pages") or {}
            for page in pages.values():
                img = (page.get("original") or page.get("thumbnail") or {}).get("source")
                if img:
                    return img
        except (httpx.HTTPError, ValueError):
            continue
    return None


async def _best_photo(client: httpx.AsyncClient, name: str, destination: str) -> str | None:
    if settings.serpapi_api_key:
        thumb = await _serp_photo(client, name, destination)
        if thumb:
            return thumb
    return await _wiki_photo(client, name, destination)


async def attach_photos(stops: list[dict], destination: str) -> None:
    """Fill in a real photo URL for each stop that lacks one (in place).

    Uses SerpAPI Google Maps when a key is set, else keyless Wikipedia images.
    """
    if not stops:
        return
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        thumbs = await asyncio.gather(
            *(_best_photo(client, s.get("name", ""), destination) for s in stops)
        )
    for stop, thumb in zip(stops, thumbs):
        if thumb and not stop.get("thumbnail"):
            stop["thumbnail"] = thumb


def _trim(place: dict, category: str) -> dict | None:
    coords = place.get("gps_coordinates") or {}
    lat, lng = coords.get("latitude"), coords.get("longitude")
    name = place.get("title")
    if not name or lat is None or lng is None:
        return None
    return {
        "name": name,
        "lat": lat,
        "lng": lng,
        "rating": place.get("rating"),
        "reviews_count": place.get("reviews"),
        "type": place.get("type"),
        "category": category,
        "description": place.get("description"),
        "address": place.get("address"),
        "thumbnail": place.get("thumbnail"),
        "reviews": [],
    }


async def fetch_places(destination: str) -> list[dict]:
    """Google Maps place data (coords + photo + rating) for any destination."""
    if not settings.serpapi_api_key:
        return []
    by_name: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        results = await asyncio.gather(
            *(
                _maps_search(client, q.format(dest=destination))
                for q in CATEGORY_QUERIES.values()
            )
        )
    for (category, _), data in zip(CATEGORY_QUERIES.items(), results):
        for raw in _results(data)[:10]:
            trimmed = _trim(raw, category)
            if trimmed and trimmed["name"] not in by_name:
                by_name[trimmed["name"]] = trimmed
    return list(by_name.values())
