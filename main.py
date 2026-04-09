from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel
import httpx
import json
import os
import math
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
app = FastAPI(title="ALD Service Area Checker")
logger = logging.getLogger("ald")
logging.basicConfig(level=logging.INFO)

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
if not GOOGLE_MAPS_API_KEY:
    raise RuntimeError("GOOGLE_MAPS_API_KEY environment variable is required")

# Email config (optional — webhook endpoint only works if these are set)
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFICATION_EMAILS = [
    e.strip()
    for e in os.environ.get("NOTIFICATION_EMAILS", "").split(",")
    if e.strip()
]

OUR_PHONE = "(818) 593-0943"
LA_TZ = timezone(timedelta(hours=-7))  # Pacific Daylight Time

# ──────────────────────────────────────────────
# Load data files at startup
# ──────────────────────────────────────────────
_polygon_path = Path(__file__).parent / "polygon.json"
try:
    with open(_polygon_path) as f:
        POLYGON = json.load(f)
    print(f"Loaded polygon with {len(POLYGON)} points")
except Exception as e:
    print(f"ERROR loading polygon: {e}")
    POLYGON = []

_locations_path = Path(__file__).parent / "ald_locations.json"
try:
    with open(_locations_path) as f:
        ALD_LOCATIONS = json.load(f)
    print(f"Loaded {len(ALD_LOCATIONS)} ALD franchise locations")
except Exception as e:
    print(f"ERROR loading ALD locations: {e}")
    ALD_LOCATIONS = []

# Track processed call IDs in memory (resets on redeploy, but that's fine —
# Retell only sends each call_analyzed event once)
_processed_calls = set()


# ──────────────────────────────────────────────
# Geo utilities
# ──────────────────────────────────────────────
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
    R = 3958.8
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


# ──────────────────────────────────────────────
# Email utilities
# ──────────────────────────────────────────────
def send_email(subject: str, html_body: str, recipients: list[str]):
    """Send an email via Gmail SMTP. Raises on failure."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError("Email not configured: GMAIL_ADDRESS and GMAIL_APP_PASSWORD required")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"ALD AI Receptionist <{GMAIL_ADDRESS}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())

    logger.info(f"Email sent to {recipients}: {subject}")


def format_call_email(call: dict) -> tuple[str, str]:
    """
    Given a Retell call object, return (subject, html_body).
    """
    call_id = call.get("call_id", "unknown")
    from_number = call.get("from_number", "Unknown")
    to_number = call.get("to_number", "Unknown")
    duration_ms = call.get("duration_ms", 0)
    start_ts = call.get("start_timestamp")
    recording_url = call.get("recording_url", "")
    transcript = call.get("transcript", "No transcript available")
    disconnect = call.get("disconnection_reason", "unknown")

    analysis = call.get("call_analysis", {})
    summary = analysis.get("call_summary", "No summary available")
    sentiment = analysis.get("user_sentiment", "Unknown")
    custom = analysis.get("custom_analysis_data", {})

    # Format duration
    total_sec = duration_ms // 1000
    mins, secs = divmod(total_sec, 60)
    duration_str = f"{mins}m {secs}s"

    # Format timestamp in LA time
    if start_ts:
        try:
            dt = datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc)
            dt_la = dt.astimezone(LA_TZ)
            time_str = dt_la.strftime("%B %d, %Y at %I:%M %p PT")
        except Exception:
            time_str = str(start_ts)
    else:
        time_str = "Unknown"

    # Format phone
    def fmt_phone(p):
        if p and len(p) == 12 and p.startswith("+1"):
            d = p[2:]
            return f"({d[:3]}) {d[3:6]}-{d[6:]}"
        return p or "Unknown"

    caller_phone = fmt_phone(from_number)
    caller_name = custom.get("caller_name", "Unknown Caller")

    # Subject
    subject = f"📞 New ALD Call — {caller_name} — {time_str}"

    # Build intake data rows
    intake_rows = ""
    # Define display order for known fields
    field_labels = {
        "caller_name": "Caller Name",
        "property_address": "Property Address",
        "caller_phone": "Caller Phone",
        "caller_email": "Caller Email",
        "referral_source": "How They Heard About Us",
        "property_type": "Property Type",
        "symptom_type": "Leak Symptom",
        "urgency_level": "Urgency",
        "has_insurance": "Has Insurance",
        "insurance_company": "Insurance Company",
        "claim_number": "Claim Number",
        "adjuster_name": "Adjuster Name",
        "adjuster_phone": "Adjuster Phone",
        "adjuster_email": "Adjuster Email",
        "water_meter_reading": "Water Meter Reading",
        "bathroom_count": "Bathroom Count (1st Floor)",
        "pool_or_spa": "Pool/Spa",
        "photos_available": "Photos Available",
        "caller_relationship": "Caller Relationship",
        "decision_maker": "Decision Maker",
        "decision_maker_name": "Decision Maker Name",
        "decision_maker_phone": "Decision Maker Phone",
        "decision_maker_email": "Decision Maker Email",
        "property_access": "Property Access Notes",
        "gate_code": "Gate Code",
        "additional_notes": "Additional Notes",
    }

    # First show known fields in order
    shown = set()
    for key, label in field_labels.items():
        if key in custom and custom[key]:
            val = str(custom[key])
            intake_rows += f'<tr><td style="padding:6px 12px;font-weight:bold;vertical-align:top;white-space:nowrap;">{label}</td><td style="padding:6px 12px;">{val}</td></tr>\n'
            shown.add(key)

    # Then show any extra fields not in our known list
    for key, val in custom.items():
        if key not in shown and val:
            label = key.replace("_", " ").title()
            intake_rows += f'<tr><td style="padding:6px 12px;font-weight:bold;vertical-align:top;white-space:nowrap;">{label}</td><td style="padding:6px 12px;">{str(val)}</td></tr>\n'

    # Escape transcript for HTML
    transcript_html = transcript.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    # Build HTML
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto;">
        <div style="background: #1a5276; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
            <h1 style="margin:0; font-size:22px;">📞 American Leak Detection — New Call</h1>
            <p style="margin:8px 0 0; opacity:0.9;">{time_str}</p>
        </div>

        <div style="border: 1px solid #ddd; border-top: none; padding: 20px;">
            <h2 style="color:#1a5276; font-size:16px; margin-top:0;">Call Details</h2>
            <table style="border-collapse:collapse; width:100%;">
                <tr><td style="padding:6px 12px;font-weight:bold;width:140px;">Caller Phone</td><td style="padding:6px 12px;">{caller_phone}</td></tr>
                <tr><td style="padding:6px 12px;font-weight:bold;">Duration</td><td style="padding:6px 12px;">{duration_str}</td></tr>
                <tr><td style="padding:6px 12px;font-weight:bold;">Sentiment</td><td style="padding:6px 12px;">{sentiment}</td></tr>
                <tr><td style="padding:6px 12px;font-weight:bold;">Disconnect</td><td style="padding:6px 12px;">{disconnect}</td></tr>
                <tr><td style="padding:6px 12px;font-weight:bold;">Call ID</td><td style="padding:6px 12px;font-size:12px;color:#666;">{call_id}</td></tr>
            </table>

            <h2 style="color:#1a5276; font-size:16px;">AI Summary</h2>
            <div style="background:#f0f4f8; padding:14px; border-radius:6px; line-height:1.5;">
                {summary}
            </div>

            <h2 style="color:#1a5276; font-size:16px;">Intake Data</h2>
            <table style="border-collapse:collapse; width:100%; background:#fafafa; border-radius:6px;">
                {intake_rows if intake_rows else '<tr><td style="padding:12px;color:#888;">No structured intake data collected</td></tr>'}
            </table>

            {"<h2 style='color:#1a5276; font-size:16px;'>Recording</h2><p><a href='" + recording_url + "' style='color:#2980b9;'>🎧 Listen to recording</a></p>" if recording_url else ""}

            <h2 style="color:#1a5276; font-size:16px;">Full Transcript</h2>
            <div style="background:#f9f9f9; padding:14px; border-radius:6px; font-size:13px; line-height:1.6; max-height:500px; overflow-y:auto;">
                {transcript_html}
            </div>
        </div>

        <div style="background:#f0f0f0; padding:12px 20px; border-radius:0 0 8px 8px; text-align:center; font-size:12px; color:#888;">
            American Leak Detection AI Receptionist — Automated Notification
        </div>
    </div>
    """

    return subject, html


# ──────────────────────────────────────────────
# Existing endpoints
# ──────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "ALD Service Area Checker",
        "polygon_points": len(POLYGON),
        "franchise_locations": len(ALD_LOCATIONS),
        "email_configured": bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD),
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "polygon_points": len(POLYGON),
        "franchise_locations": len(ALD_LOCATIONS),
        "email_configured": bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD),
    }


@app.get("/time")
def get_current_time():
    """Return the current time in Pacific Time for the AI agent."""
    pacific = timezone(timedelta(hours=-7))  # PDT (summer)
    now = datetime.now(pacific)
    hour = now.hour
    is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6
    is_business_hours = (not is_weekend) and (8 <= hour < 17)
    return {
        "current_time": now.strftime("%I:%M %p"),
        "day_of_week": now.strftime("%A"),
        "date": now.strftime("%B %d, %Y"),
        "timezone": "Pacific Time",
        "is_business_hours": is_business_hours,
        "office_status": "open" if is_business_hours else "closed",
    }


@app.post("/time")
def get_current_time_post():
    """POST version of /time for Retell custom tool compatibility."""
    return get_current_time()


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

    if "args" in body and isinstance(body["args"], dict):
        address = body["args"].get("address", "")
    else:
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

    if "args" in body and isinstance(body["args"], dict):
        args = body["args"]
    else:
        args = body

    zip_code = args.get("zip_code", "").strip()
    address = args.get("address", "").strip()

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

    non_ours = [l for l in nearby if l["phone"] != OUR_PHONE]

    if not non_ours:
        loc = nearby[0]
        return {
            "found": True,
            "count": 1,
            "referral_phone": loc["phone"],
            "referral_name": loc["name"],
            "message": f"The nearest office is {loc['name']}. Their phone number is {loc['phone']}.",
            "locations": nearby,
        }

    best = non_ours[0]
    return {
        "found": True,
        "count": len(non_ours),
        "referral_phone": best["phone"],
        "referral_name": best["name"],
        "message": f"The nearest office is {best['name']}. Their phone number is {best['phone']}.",
        "locations": non_ours,
    }


# ──────────────────────────────────────────────
# Retell Webhook Endpoint
# ──────────────────────────────────────────────
def _send_call_email(call: dict):
    """Background task: format and send the call email. Logs errors instead of raising."""
    call_id = call.get("call_id", "unknown")
    try:
        subject, html_body = format_call_email(call)
        send_email(subject, html_body, NOTIFICATION_EMAILS)
        logger.info(f"✅ Email sent for call {call_id}")
    except Exception as e:
        logger.error(f"❌ Failed to send email for call {call_id}: {e}")


@app.post("/webhook/retell")
async def retell_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive Retell AI webhook events.
    On 'call_analyzed', send an email summary to the configured notification addresses.
    Returns 200 immediately; email is sent in background.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = body.get("event", "")
    logger.info(f"Webhook received: event={event}")

    # Only process call_analyzed events
    if event != "call_analyzed":
        return {"status": "ignored", "event": event}

    # Check email config
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not NOTIFICATION_EMAILS:
        logger.error("Email not configured — skipping email send")
        return {
            "status": "error",
            "message": "Email not configured. Set GMAIL_ADDRESS, GMAIL_APP_PASSWORD, and NOTIFICATION_EMAILS env vars.",
        }

    call = body.get("call", {})
    call_id = call.get("call_id", "unknown")

    # Dedup
    if call_id in _processed_calls:
        logger.info(f"Call {call_id} already processed — skipping")
        return {"status": "duplicate", "call_id": call_id}

    _processed_calls.add(call_id)

    # Send email in background so we return 200 fast (Retell has timeout)
    background_tasks.add_task(_send_call_email, call)

    return {"status": "accepted", "call_id": call_id}


@app.post("/webhook/retell/test")
async def retell_webhook_test():
    """
    Health check for the webhook endpoint.
    Returns config status so you can verify email is set up correctly.
    """
    return {
        "status": "ok",
        "email_configured": bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD),
        "notification_emails": NOTIFICATION_EMAILS if NOTIFICATION_EMAILS else "NOT SET",
        "gmail_address": GMAIL_ADDRESS if GMAIL_ADDRESS else "NOT SET",
    }
