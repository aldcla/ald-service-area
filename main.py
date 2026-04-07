from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import httpx
import json
import os
import math
from pathlib import Path

app = FastAPI(title="ALD Service Area Checker")

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
if not GOOGLE_MAPS_API_KEY:
    raise RuntimeError("GOOGLE_MAPS_API_KEY environment variable is required")
OUR_PHONE = "(818) 593-0943"

# Load polygon at startup
_polygon_path = Path(__file__).parent / "polygon.json"
try:
    with open(_polygon_path) as f:
        POLYGON = json.load(f)
    print(f"Loaded polygon with {len(POLYGON)} points")
except Exception as e:
    print(f"ERROR loading polygon: {e}")
    POLYGON = []

# Load ALD franchise locations at startup
_locations_path = Path(__file__).parent / "ald_locations.json"
try:
    with open(_locations_path) as f:
        ALD_LOCATIONS = json.load(f)
    print(f"Loaded {len(ALD_LOCATIONS)} ALD franchise locations")
except Exception as e:
    print(f"ERROR loading ALD locations: {e}")
    ALD_LOCATIONS = []


def point_in_polygon(lat: float, lng: float) -> bool:
    """Ray casting algorithm."""
    n = len(POLYGON)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = POLYGON[i]["lng"], POLYGON[i]["lat"]
        xj, yj = POLYGON[j]["lng"], POLYGON[j]["lat"]
        if (yi > lat) != (yj > lat):
            x_intersect = xi + (lat - yi) * (xj - xi) / (yj - yi)
            if lng < x_intersect:
                inside = not inside
        j = i
    return inside


def haversine_miles(lat1, lng1, lat2, lng2):
    """Calculate distance in miles between two lat/lng points."""
    R = 3958.8  # Earth radius in miles
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def find_nearest_locations(lat: float, lng: float, max_results: int = 3, max_miles: float = 100):
    """Find nearest ALD franchise locations to a given lat/lng."""
    results = []
    for loc in ALD_LOCATIONS:
        if loc.get("lat") is None or loc.get("lng") is None:
            continue
        dist = haversine_miles(lat, lng, loc["lat"], loc["lng"])
        if dist <= max_miles:
            results.append({
                "name": loc["name"],
                "phone": loc["phone"],
                "city": loc.get("city"),
                "state": loc.get("state"),
                "zip": loc.get("zip"),
                "distance_miles": round(dist, 1),
                "website": loc.get("website"),
            })
    results.sort(key=lambda x: x["distance_miles"])
    return results[:max_results]


async def geocode_address(address: str):
    """Geocode an address and return lat, lng, formatted_address."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": GOOGLE_MAPS_API_KEY},
        )
        data = resp.json()

    if data.get("status") != "OK" or not data.get("results"):
        return None, None, None

    result = data["results"][0]
    loc = result["geometry"]["location"]
    return loc["lat"], loc["lng"], result["formatted_address"]


async def resolve_address(address: str) -> dict:
    """Geocode address and return service area result."""
    if not address or len(address.strip()) < 5:
        raise HTTPException(status_code=400, detail="Address is too short")

    lat, lng, formatted = await geocode_address(address)

    if lat is None:
        return {
            "in_area": None,
            "error": "Geocoding failed",
            "address": address,
            "message": "Could not verify the address — please continue the call normally.",
        }

    in_area = point_in_polygon(lat, lng)

    return {
        "in_area": in_area,
        "formatted_address": formatted,
        "lat": lat,
        "lng": lng,
        "message": (
            "Address is within our service area."
            if in_area
            else "That address appears to be outside our local service area."
        ),
    }


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "ALD Service Area Checker",
        "polygon_points": len(POLYGON),
        "franchise_locations": len(ALD_LOCATIONS),
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "polygon_points": len(POLYGON),
        "franchise_locations": len(ALD_LOCATIONS),
    }


@app.post("/check")
async def check_service_area(request: Request):
    """
    Accept address in multiple formats:
    - Direct (Bland AI):  {"address": "123 Main St, Glendale CA"}
    - Retell wrapper:     {"name": "CheckServiceArea", "args": {"address": "..."}, "call": {...}}
    - Retell args-only:   {"address": "123 Main St, Glendale CA"}  (same as direct)
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Extract address from any supported format
    if "args" in body and isinstance(body["args"], dict):
        # Retell default format: {name, call, args: {address: ...}}
        address = body["args"].get("address", "")
    else:
        # Direct format or Retell args-only: {address: ...}
        address = body.get("address", "")

    if not address:
        raise HTTPException(status_code=400, detail="No address provided")

    return await resolve_address(address)


@app.post("/find-location")
async def find_location(request: Request):
    """
    Find the nearest ALD franchise office for an out-of-area caller.
    
    Accept zip code or address in multiple formats:
    - Direct:         {"zip_code": "90210"} or {"address": "Beverly Hills, CA"}
    - Retell wrapper: {"name": "FindNearestOffice", "args": {"zip_code": "90210"}, "call": {...}}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Extract args from any supported format
    if "args" in body and isinstance(body["args"], dict):
        args = body["args"]
    else:
        args = body

    zip_code = args.get("zip_code", "").strip()
    address = args.get("address", "").strip()

    # Use zip code if provided, otherwise use address
    search_query = zip_code if zip_code else address
    if not search_query:
        raise HTTPException(status_code=400, detail="No zip_code or address provided")

    lat, lng, formatted = await geocode_address(search_query)

    if lat is None:
        return {
            "found": False,
            "message": "Could not look up that location. Please try again.",
            "locations": [],
        }

    nearby = find_nearest_locations(lat, lng, max_results=3, max_miles=100)

    if not nearby:
        return {
            "found": False,
            "message": "We could not find an American Leak Detection office near that area. You can visit americanleakdetection.com/locations to find the closest office.",
            "locations": [],
        }

    # If there's only one result, give that number directly
    if len(nearby) == 1:
        loc = nearby[0]
        return {
            "found": True,
            "count": 1,
            "referral_phone": loc["phone"],
            "referral_name": loc["name"],
            "message": f"The nearest office is {loc['name']}. Their phone number is {loc['phone']}.",
            "locations": nearby,
        }

    # If there are multiple results, filter out our number
    non_ours = [l for l in nearby if l["phone"] != OUR_PHONE]
    
    if not non_ours:
        # All results are our number (shouldn't happen for out-of-area)
        loc = nearby[0]
        return {
            "found": True,
            "count": 1,
            "referral_phone": loc["phone"],
            "referral_name": loc["name"],
            "message": f"The nearest office is {loc['name']}. Their phone number is {loc['phone']}.",
            "locations": nearby,
        }

    # Give the closest non-our number
    best = non_ours[0]
    return {
        "found": True,
        "count": len(non_ours),
        "referral_phone": best["phone"],
        "referral_name": best["name"],
        "message": f"The nearest office is {best['name']}. Their phone number is {best['phone']}.",
        "locations": non_ours,
    }
