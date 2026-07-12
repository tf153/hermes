"""Place data for any destination via Linkup structured web search.

For each persona category we ask Linkup for real spots with coordinates,
ratings and review snippets, then cache the merged result per destination in
data/places/{slug}.json. Photos are added separately (see app.photos).

Falls back to SerpAPI Google Maps data when Linkup has no key or returns
nothing, and to a small built-in Goa seed list as a last resort for Goa trips.
"""

import asyncio
import json
import logging
import re
import statistics

import httpx

from app import photos
from app.config import settings

logger = logging.getLogger(__name__)

LINKUP_URL = "https://api.linkup.so/v1/search"

# Linkup rate-limits aggressively; keep few requests in flight and retry 429s.
_CONCURRENCY = 2
_MAX_ATTEMPTS = 3

# Persona-oriented searches; "{dest}" is filled with the destination name.
CATEGORY_QUERIES: dict[str, str] = {
    "pilgrimage": "most famous temples, churches and shrines in {dest}",
    "beaches": "best beaches or waterfronts in {dest}",
    "sunset": "best sunset viewpoints in {dest}",
    "cafes": "best cafes for slow mornings in {dest}",
    "trek": "best forts, hills and hiking viewpoints in {dest}",
    "food": "best local restaurants and food spots in {dest}",
    "family": "best family friendly attractions in {dest} for kids",
    "nature": "best waterfalls, parks and nature spots in {dest}",
}

PLACES_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "places": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Official place name as on Google Maps"},
                        "latitude": {"type": "number", "description": "Exact GPS latitude of the place"},
                        "longitude": {"type": "number", "description": "Exact GPS longitude of the place"},
                        "rating": {"type": "number", "description": "Google Maps star rating, e.g. 4.5"},
                        "reviews_count": {"type": "integer", "description": "Approximate number of Google reviews"},
                        "type": {"type": "string", "description": "Short kind, e.g. Beach, Fort, Cafe, Temple"},
                        "description": {"type": "string", "description": "One-line description of the place"},
                        "address": {"type": "string", "description": "Locality/neighbourhood"},
                        "review_snippets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "1-3 short real traveler review quotes",
                        },
                    },
                    "required": ["name", "latitude", "longitude"],
                },
            }
        },
        "required": ["places"],
    }
)


def _slug(destination: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", destination.lower()).strip("_") or "trip"


def _trim_place(raw: dict, category: str) -> dict | None:
    name = (raw.get("name") or "").strip()
    lat, lng = raw.get("latitude"), raw.get("longitude")
    if not name or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return {
        "name": name,
        "lat": float(lat),
        "lng": float(lng),
        "rating": raw.get("rating"),
        "reviews_count": raw.get("reviews_count"),
        "type": raw.get("type"),
        "category": category,
        "description": raw.get("description"),
        "address": raw.get("address"),
        "thumbnail": None,
        "reviews": [s[:280] for s in (raw.get("review_snippets") or [])[:3]],
    }


def _drop_outliers(places: list[dict], max_deg: float = 2.5) -> list[dict]:
    """Drop places far from the cluster median (guards hallucinated coords)."""
    if len(places) < 4:
        return places
    med_lat = statistics.median(p["lat"] for p in places)
    med_lng = statistics.median(p["lng"] for p in places)
    kept = [
        p
        for p in places
        if abs(p["lat"] - med_lat) <= max_deg and abs(p["lng"] - med_lng) <= max_deg
    ]
    dropped = len(places) - len(kept)
    if dropped:
        logger.info("dropped %d place(s) with outlier coordinates", dropped)
    return kept or places


async def _search_category(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    category: str,
    destination: str,
) -> list[dict]:
    query = CATEGORY_QUERIES[category].format(dest=destination)
    prompt = (
        f"{query}. For each place give its exact GPS coordinates (latitude and "
        f"longitude), Google Maps star rating, approximate number of Google "
        f"reviews, a one-line description, its locality, and 1-3 short real "
        f"traveler review quotes. List 6-8 places."
    )
    data = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with semaphore:
                resp = await client.post(
                    LINKUP_URL,
                    headers={"Authorization": f"Bearer {settings.linkup_api_key}"},
                    json={
                        "q": prompt,
                        "depth": "standard",
                        "outputType": "structured",
                        "structuredOutputSchema": PLACES_SCHEMA,
                    },
                )
            if resp.status_code == 429 and attempt < _MAX_ATTEMPTS:
                wait = float(resp.headers.get("Retry-After") or 2 * attempt)
                logger.info(
                    "Linkup rate-limited (%s/%s), retrying in %.0fs", destination, category, wait
                )
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except httpx.HTTPError as exc:
            logger.warning("Linkup search failed for %s/%s: %s", destination, category, exc)
            return []
    if data is None:
        return []

    places = []
    for raw in data.get("places") or []:
        place = _trim_place(raw, category)
        if place:
            places.append(place)
    logger.info("linkup %s/%s: %d usable places", destination, category, len(places))
    return places


async def fetch_places(destination: str, force: bool = False) -> list[dict]:
    """Return real spots for a destination (cached per destination)."""
    cache = settings.places_dir / f"{_slug(destination)}.json"
    if cache.exists() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data:
                return data
        except (json.JSONDecodeError, OSError):
            pass

    places: list[dict] = []
    if settings.linkup_api_key:
        logger.info(
            "fetching %s places via Linkup (%d categories)",
            destination,
            len(CATEGORY_QUERIES),
        )
        semaphore = asyncio.Semaphore(_CONCURRENCY)
        async with httpx.AsyncClient(timeout=90) as client:
            results = await asyncio.gather(
                *(
                    _search_category(client, semaphore, category, destination)
                    for category in CATEGORY_QUERIES
                )
            )
        by_name: dict[str, dict] = {}
        for category_places in results:
            for place in category_places:
                key = place["name"].lower()
                if key not in by_name:
                    by_name[key] = place
        places = _drop_outliers(list(by_name.values()))

    if not places:
        logger.warning("Linkup gave no places for %s, trying SerpAPI", destination)
        places = await photos.fetch_places(destination)

    if not places and "goa" in destination.lower():
        logger.warning("using built-in Goa seed places")
        places = _seed_places()

    if not places:
        raise RuntimeError(
            f"could not find any places for {destination!r} - "
            "check LINKUP_API_KEY / SERPAPI_API_KEY"
        )

    cache.write_text(json.dumps(places, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("cached %d places for %s", len(places), destination)
    return places


def _seed_places() -> list[dict]:
    """Hand-seeded Goa spots so the default demo works without any API key."""
    return [
        {"name": "Basilica of Bom Jesus", "lat": 15.5009, "lng": 73.9116, "rating": 4.6, "reviews_count": 45000, "type": "Basilica", "category": "pilgrimage", "description": "UNESCO church holding the relics of St. Francis Xavier.", "address": "Old Goa", "thumbnail": None, "reviews": []},
        {"name": "Se Cathedral", "lat": 15.5031, "lng": 73.9119, "rating": 4.6, "reviews_count": 20000, "type": "Cathedral", "category": "pilgrimage", "description": "One of Asia's largest churches.", "address": "Old Goa", "thumbnail": None, "reviews": []},
        {"name": "Shri Mangeshi Temple", "lat": 15.4327, "lng": 74.0060, "rating": 4.7, "reviews_count": 30000, "type": "Hindu temple", "category": "pilgrimage", "description": "Serene temple dedicated to Lord Manguesh.", "address": "Mardol, Ponda", "thumbnail": None, "reviews": []},
        {"name": "Palolem Beach", "lat": 15.0100, "lng": 74.0233, "rating": 4.5, "reviews_count": 42000, "type": "Beach", "category": "beaches", "description": "Crescent-shaped calm beach in South Goa.", "address": "Canacona", "thumbnail": None, "reviews": []},
        {"name": "Baga Beach", "lat": 15.5553, "lng": 73.7517, "rating": 4.3, "reviews_count": 90000, "type": "Beach", "category": "beaches", "description": "Lively North Goa beach with water sports.", "address": "Baga", "thumbnail": None, "reviews": []},
        {"name": "Chapora Fort", "lat": 15.6039, "lng": 73.7369, "rating": 4.3, "reviews_count": 33000, "type": "Fort", "category": "sunset", "description": "Hilltop fort with sweeping sunset views.", "address": "Chapora", "thumbnail": None, "reviews": []},
        {"name": "Cabo de Rama Fort", "lat": 15.0894, "lng": 73.9200, "rating": 4.4, "reviews_count": 8000, "type": "Fort", "category": "trek", "description": "Clifftop fort with dramatic sea views.", "address": "Canacona", "thumbnail": None, "reviews": []},
        {"name": "Fort Aguada", "lat": 15.4925, "lng": 73.7734, "rating": 4.4, "reviews_count": 55000, "type": "Fort", "category": "sunset", "description": "17th-century fort and lighthouse over the Arabian Sea.", "address": "Sinquerim", "thumbnail": None, "reviews": []},
        {"name": "Dudhsagar Falls", "lat": 15.3144, "lng": 74.3144, "rating": 4.5, "reviews_count": 28000, "type": "Waterfall", "category": "nature", "description": "Towering four-tier waterfall on the Mandovi river.", "address": "Sonaulim", "thumbnail": None, "reviews": []},
        {"name": "Sahakari Spice Farm", "lat": 15.4506, "lng": 74.0392, "rating": 4.3, "reviews_count": 12000, "type": "Spice plantation", "category": "family", "description": "Guided spice plantation tour with lunch.", "address": "Ponda", "thumbnail": None, "reviews": []},
        {"name": "Cafe Bodega (Sunaparanta)", "lat": 15.4844, "lng": 73.8330, "rating": 4.5, "reviews_count": 3500, "type": "Cafe", "category": "cafes", "description": "Peaceful courtyard cafe in an arts centre.", "address": "Altinho, Panaji", "thumbnail": None, "reviews": []},
        {"name": "The Fisherman's Wharf", "lat": 15.3860, "lng": 73.9060, "rating": 4.3, "reviews_count": 26000, "type": "Restaurant", "category": "food", "description": "Riverside Goan seafood restaurant.", "address": "Cavelossim", "thumbnail": None, "reviews": []},
        {"name": "Fontainhas Latin Quarter", "lat": 15.4979, "lng": 73.8330, "rating": 4.5, "reviews_count": 9000, "type": "Historic area", "category": "cafes", "description": "Colourful Portuguese-era lanes, cafes and galleries.", "address": "Panaji", "thumbnail": None, "reviews": []},
        {"name": "Anjuna Beach", "lat": 15.5735, "lng": 73.7400, "rating": 4.3, "reviews_count": 60000, "type": "Beach", "category": "sunset", "description": "Rocky-edged beach known for sunsets and flea market.", "address": "Anjuna", "thumbnail": None, "reviews": []},
    ]
