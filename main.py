from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
import json
import os
from pathlib import Path

app = FastAPI(title="ALD Service Area Checker")

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "AIzaSyC-HJa-DssnthBPzWgfVtJdZa27HJ3ARZM")

# Load polygon at startup
_polygon_path = Path(__file__).parent / "polygon.json"
try:
    with open(_polygon_path) as f:
        POLYGON = json.load(f)
    print(f"Loaded polygon with {len(POLYGON)} points")
except Exception as e:
    print(f"ERROR loading polygon: {e}")
    POLYGON = []


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


class AddressRequest(BaseModel):
    address: str


@app.get("/")
def root():
    return {"status": "ok", "service": "ALD Service Area Checker", "polygon_points": len(POLYGON)}


@app.get("/health")
def health():
    return {"status": "ok", "polygon_points": len(POLYGON)}


@app.post("/check")
async def check_service_area(request: AddressRequest):
    if not request.address or len(request.address.strip()) < 5:
        raise HTTPException(status_code=400, detail="Address is too short")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": request.address, "key": GOOGLE_MAPS_API_KEY},
        )

    data = resp.json()

    if data.get("status") != "OK" or not data.get("results"):
        return {
            "in_area": None,
            "error": f"Geocoding failed: {data.get('status', 'UNKNOWN')}",
            "address": request.address,
            "message": "Could not verify the address — please continue the call normally.",
        }

    result = data["results"][0]
    loc = result["geometry"]["location"]
    lat, lng = loc["lat"], loc["lng"]
    formatted = result["formatted_address"]

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
