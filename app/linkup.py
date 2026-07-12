"""Goa places data via Linkup structured web search (coords + ratings + reviews).

Fetched once across persona categories and cached to data/goa_places.json so
persona selection is fast and cheap. Falls back to a small built-in seed list
when no API key is configured or the search comes back empty.
"""

import asyncio
import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

LINKUP_URL = "https://api.linkup.so/v1/search"

# Persona-oriented searches. Each becomes a category tag on its results.
GOA_QUERIES: dict[str, str] = {
    "pilgrimage": "most famous temples and churches in Goa, India",
    "beaches": "best beaches in Goa, India",
    "sunset": "best sunset viewpoints and forts in Goa, India",
    "cafes": "best cafes for slow mornings in Goa, India",
    "trek": "best forts and hilltop viewpoints to hike in Goa, India",
    "food": "best local Goan restaurants in Goa, India",
    "family": "best family friendly attractions in Goa, India for kids",
    "nature": "best waterfalls and nature spots in Goa, India",
}

# Goa bounding box; drop hallucinated or out-of-state coordinates.
LAT_MIN, LAT_MAX = 14.80, 15.90
LNG_MIN, LNG_MAX = 73.50, 74.50

PLACES_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "places": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Official place name as on Google Maps",
                        },
                        "latitude": {
                            "type": "number",
                            "description": "Exact GPS latitude of the place",
                        },
                        "longitude": {
                            "type": "number",
                            "description": "Exact GPS longitude of the place",
                        },
                        "rating": {
                            "type": "number",
                            "description": "Google Maps star rating, e.g. 4.5",
                        },
                        "reviews_count": {
                            "type": "integer",
                            "description": "Approximate number of Google reviews",
                        },
                        "type": {
                            "type": "string",
                            "description": "Short kind, e.g. Beach, Fort, Cafe, Temple",
                        },
                        "description": {
                            "type": "string",
                            "description": "One-line description of the place",
                        },
                        "address": {
                            "type": "string",
                            "description": "Locality, e.g. 'Old Goa' or 'Anjuna'",
                        },
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


async def _search_category(
    client: httpx.AsyncClient, category: str, query: str
) -> list[dict]:
    prompt = (
        f"{query}. For each place give its exact GPS coordinates (latitude and "
        f"longitude), Google Maps star rating, approximate number of Google "
        f"reviews, a one-line description, its locality, and 1-3 short real "
        f"traveler review quotes. List 6-8 places."
    )
    try:
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
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("Linkup search failed for %s: %s", category, exc)
        return []

    places = []
    for raw in data.get("places") or []:
        place = _trim_place(raw, category)
        if place:
            places.append(place)
    logger.info("linkup %s: %d usable places", category, len(places))
    return places


def _trim_place(raw: dict, category: str) -> dict | None:
    name = (raw.get("name") or "").strip()
    lat, lng = raw.get("latitude"), raw.get("longitude")
    if not name or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    if not (LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX):
        logger.info("dropping %s: coords (%s, %s) outside Goa", name, lat, lng)
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
        "reviews": [s[:280] for s in (raw.get("review_snippets") or [])[:3]],
    }


async def fetch_goa_places(force: bool = False) -> list[dict]:
    """Return Goa spots (cached). Fetches via Linkup on first run."""
    cache = settings.places_cache_path
    if cache.exists() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data:
                return data
        except (json.JSONDecodeError, OSError):
            pass

    if not settings.linkup_api_key:
        logger.info("LINKUP_API_KEY not set, using seed Goa places")
        seed = _seed_places()
        cache.write_text(json.dumps(seed, indent=2, ensure_ascii=False), encoding="utf-8")
        return seed

    logger.info("fetching Goa places via Linkup (%d categories)", len(GOA_QUERIES))
    async with httpx.AsyncClient(timeout=90) as client:
        results = await asyncio.gather(
            *(
                _search_category(client, category, query)
                for category, query in GOA_QUERIES.items()
            )
        )

    by_name: dict[str, dict] = {}
    for category_places in results:
        for place in category_places:
            key = place["name"].lower()
            if key not in by_name:
                by_name[key] = place

    places = list(by_name.values())
    if not places:
        logger.warning("Linkup returned no usable places, using seed list")
        places = _seed_places()
    cache.write_text(json.dumps(places, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("cached %d Goa places", len(places))
    return places


def _seed_places() -> list[dict]:
    """Hand-seeded Goa spots so the demo works without a Linkup key."""
    return [
        {"name": "Basilica of Bom Jesus", "lat": 15.5009, "lng": 73.9116, "rating": 4.6, "reviews_count": 45000, "type": "Basilica", "category": "pilgrimage", "description": "UNESCO church holding the relics of St. Francis Xavier.", "address": "Old Goa", "reviews": []},
        {"name": "Se Cathedral", "lat": 15.5031, "lng": 73.9119, "rating": 4.6, "reviews_count": 20000, "type": "Cathedral", "category": "pilgrimage", "description": "One of Asia's largest churches.", "address": "Old Goa", "reviews": []},
        {"name": "Shri Mangeshi Temple", "lat": 15.4327, "lng": 74.0060, "rating": 4.7, "reviews_count": 30000, "type": "Hindu temple", "category": "pilgrimage", "description": "Serene temple dedicated to Lord Manguesh.", "address": "Mardol, Ponda", "reviews": []},
        {"name": "Palolem Beach", "lat": 15.0100, "lng": 74.0233, "rating": 4.5, "reviews_count": 42000, "type": "Beach", "category": "beaches", "description": "Crescent-shaped calm beach in South Goa.", "address": "Canacona", "reviews": []},
        {"name": "Baga Beach", "lat": 15.5553, "lng": 73.7517, "rating": 4.3, "reviews_count": 90000, "type": "Beach", "category": "beaches", "description": "Lively North Goa beach with water sports.", "address": "Baga", "reviews": []},
        {"name": "Chapora Fort", "lat": 15.6039, "lng": 73.7369, "rating": 4.3, "reviews_count": 33000, "type": "Fort", "category": "sunset", "description": "Hilltop fort with sweeping sunset views.", "address": "Chapora", "reviews": []},
        {"name": "Cabo de Rama Fort", "lat": 15.0894, "lng": 73.9200, "rating": 4.4, "reviews_count": 8000, "type": "Fort", "category": "trek", "description": "Clifftop fort with dramatic sea views.", "address": "Canacona", "reviews": []},
        {"name": "Fort Aguada", "lat": 15.4925, "lng": 73.7734, "rating": 4.4, "reviews_count": 55000, "type": "Fort", "category": "sunset", "description": "17th-century fort and lighthouse over the Arabian Sea.", "address": "Sinquerim", "reviews": []},
        {"name": "Dudhsagar Falls", "lat": 15.3144, "lng": 74.3144, "rating": 4.5, "reviews_count": 28000, "type": "Waterfall", "category": "nature", "description": "Towering four-tier waterfall on the Mandovi river.", "address": "Sonaulim", "reviews": []},
        {"name": "Sahakari Spice Farm", "lat": 15.4506, "lng": 74.0392, "rating": 4.3, "reviews_count": 12000, "type": "Spice plantation", "category": "family", "description": "Guided spice plantation tour with lunch.", "address": "Ponda", "reviews": []},
        {"name": "Cafe Bodega (Sunaparanta)", "lat": 15.4844, "lng": 73.8330, "rating": 4.5, "reviews_count": 3500, "type": "Cafe", "category": "cafes", "description": "Peaceful courtyard cafe in an arts centre.", "address": "Altinho, Panaji", "reviews": []},
        {"name": "The Fisherman's Wharf", "lat": 15.3860, "lng": 73.9060, "rating": 4.3, "reviews_count": 26000, "type": "Restaurant", "category": "food", "description": "Riverside Goan seafood restaurant.", "address": "Cavelossim", "reviews": []},
        {"name": "Fontainhas Latin Quarter", "lat": 15.4979, "lng": 73.8330, "rating": 4.5, "reviews_count": 9000, "type": "Historic area", "category": "cafes", "description": "Colourful Portuguese-era lanes, cafes and galleries.", "address": "Panaji", "reviews": []},
        {"name": "Anjuna Beach", "lat": 15.5735, "lng": 73.7400, "rating": 4.3, "reviews_count": 60000, "type": "Beach", "category": "sunset", "description": "Rocky-edged beach known for sunsets and flea market.", "address": "Anjuna", "reviews": []},
    ]
