from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json
import requests
import uuid
from datetime import datetime

app = FastAPI()

# ================= CORS =================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================= FIREBASE =================
firebase_key = json.loads(os.environ["FIREBASE_KEY"])

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key)
    firebase_admin.initialize_app(cred)

db = firestore.client()

VERIFY_TOKEN = "campusbot"
PHONE_NUMBER_ID = "946946368512302"
ACCESS_TOKEN = os.environ["WHATSAPP_TOKEN"]
HF_TOKEN = os.environ.get("HF_TOKEN", "")

# ================= PRIORITY CLASSIFIER =================
# 1. PRIMARY   — typeform/distilbart-mnli-12-3 (~0.8s warm)
# 2. SECONDARY — facebook/bart-large-mnli (~2-4s warm)
# 3. FALLBACK  — keyword matching (instant)
# 5s timeout per model — student never waits more than ~6s total

HF_CANDIDATE_LABELS = [
    "fire, smoke, or safety emergency",
    "water leakage, flooding, or water supply failure",
    "no water supply, dry tap, or water not coming at all",
    "water quality, contamination, or unsafe drinking water",
    "sewage, drain overflow, or toilet not working",
    "lift or elevator not working",
    "electrical hazard, short circuit, or power failure",
    "AC, geyser, or heating not working at all",
    "wifi or internet not working",
    "water dispenser or washing machine issue",
    "mess food quality or hygiene complaint",
    "general maintenance or minor repair",
]

LABEL_PRIORITY_MAP = {
    "fire, smoke, or safety emergency": "High",
    "water leakage, flooding, or water supply failure": "High",
    "no water supply, dry tap, or water not coming at all": "High",
    "water quality, contamination, or unsafe drinking water": "High",
    "sewage, drain overflow, or toilet not working": "High",
    "lift or elevator not working": "High",
    "electrical hazard, short circuit, or power failure": "High",
    "AC, geyser, or heating not working at all": "Medium",
    "wifi or internet not working": "Medium",
    "water dispenser or washing machine issue": "Medium",
    "mess food quality or hygiene complaint": "Medium",
    "general maintenance or minor repair": "Low",
}

HF_MODELS = [
    "typeform/distilbart-mnli-12-3",
    "facebook/bart-large-mnli",
]

KEYWORD_RULES = [
    ("High", [
        "fire", "smoke", "burning", "flame",
        "flood", "flooding", "leaking", "water leak",
        "no water", "water not coming", "tap is dry", "dry tap",
        "no water supply", "completely dry", "not a drop", "water supply",
        "contaminated", "dirty water", "smell", "yellowish", "unsafe to drink",
        "flush", "flush not working", "toilet overflow", "sewage", "drain overflow",
        "lift stuck", "lift not working", "elevator stuck", "elevator not working",
        "short circuit", "electric shock", "no electricity", "power cut", "sparks", "blackening",
        "urgent", "emergency", "immediately", "asap",
    ]),
    ("Medium", [
        "wifi", "internet not working", "no internet",
        "water dispenser", "dispenser not working",
        "washing machine", "washer", "fridge", "oven",
        "ac not working", "no cooling", "geyser not working", "no hot water",
        "fan not working", "lights not working", "switch not working",
        "mess food", "food quality", "cold food", "vending",
    ]),
]


def _call_hf_model(model: str, text: str) -> str:
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    response = requests.post(
        url, headers=headers,
        json={"inputs": text, "parameters": {"candidate_labels": HF_CANDIDATE_LABELS}},
        timeout=5,
    )
    response.raise_for_status()
    result = response.json()
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"HF model loading: {result['error']}")
    top_label = result["labels"][0]
    top_score = result["scores"][0]
    priority = LABEL_PRIORITY_MAP.get(top_label, "Low")
    print(f"HF [{model}] label={top_label!r} score={top_score:.2f} priority={priority}")
    return priority


def _classify_keywords(category: str, description: str) -> str:
    text = f"{category} {description}".lower()
    for priority, keywords in KEYWORD_RULES:
        for kw in keywords:
            if kw in text:
                print(f"KEYWORD FALLBACK: {kw!r} -> {priority}")
                return priority
    return "Low"


def classify_priority(category: str, description: str) -> str:
    if HF_TOKEN:
        text = f"{category}: {description}"
        for model in HF_MODELS:
            try:
                return _call_hf_model(model, text)
            except Exception as e:
                print(f"HF [{model}] failed: {e} - trying next")
    print("Using keyword fallback")
    return _classify_keywords(category, description)


# ================= INPUT VALIDATION =================
import re

def validate_name(text):
    """Letters and spaces only, max 50 chars."""
    text = text.strip()
    if len(text) > 50:
        return None, "Name must be under 50 characters. Please enter your name again:"
    if not re.match(r"^[A-Za-z\s]+$", text):
        return None, "Name should contain only letters and spaces (no numbers or symbols). Please enter your name again:"
    return text, None

def validate_room(text):
    """Alphanumeric and hyphens only, max 5 chars. e.g. A-203, B305."""
    text = text.strip().upper()
    if len(text) > 5:
        return None, "Room number must be 5 characters or less (e.g. A203, B305). Please enter your room number again:"
    if not re.match(r"^[A-Za-z0-9\-]+$", text):
        return None, "Room number should only contain letters, numbers, or a hyphen (e.g. A203, B-25). Please try again:"
    return text, None

def validate_slot(text):
    """Any text, max 60 chars."""
    text = text.strip()
    if len(text) > 60:
        return None, "Please keep your availability to under 60 characters (e.g. Tomorrow 10am-12pm). Try again:"
    if len(text) < 3:
        return None, "Please enter a valid date and time (e.g. Tomorrow 10am-12pm, Today after 6pm):"
    return text, None

def validate_description(text):
    """Any text, max 500 chars."""
    text = text.strip()
    if len(text) > 500:
        return None, "Description is too long. Please keep it under 500 characters and try again:"
    if len(text) < 5:
        return None, "Please describe the issue in a few words at least:"
    return text, None

def validate_ticket_id(text):
    """8 alphanumeric characters, preserve original case for Firestore lookup."""
    text = text.strip()
    if not re.match(r"^[A-Za-z0-9]{8}$", text):
        return None, "Ticket ID should be 8 characters (letters and numbers only, e.g. a3f9b2c1). Please try again:"
    return text, None


# ================= TECHNICIAN HELPERS =================

def get_technician_name(tech_id: str) -> str:
    if not tech_id:
        return ""
    doc = db.collection("technicians").document(tech_id.lower()).get()
    if doc.exists:
        d = doc.to_dict()
        return d.get("name") or d.get("Name") or tech_id
    return tech_id

def get_technician_phone(tech_id: str) -> str:
    if not tech_id:
        return ""
    doc = db.collection("technicians").document(tech_id.lower()).get()
    if doc.exists:
        d = doc.to_dict()
        return d.get("phone") or d.get("Phone") or ""
    return ""


# ================= TECHNICIAN ENDPOINTS =================
@app.get("/technicians")
def get_technicians():
    docs = db.collection("technicians").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


class TechnicianSave(BaseModel):
    tech_id: str
    name: str
    phone: str
    category: str | None = None


@app.post("/technicians")
def save_technician(data: TechnicianSave):
    db.collection("technicians").document(data.tech_id.lower()).set({
        "name": data.name,
        "phone": data.phone,
        "category": data.category or "",
        "updated_at": datetime.utcnow(),
    })
    return {"message": "Technician saved"}


@app.delete("/technicians/{tech_id}")
def delete_technician(tech_id: str):
    db.collection("technicians").document(tech_id.lower()).delete()
    return {"message": "Technician deleted"}


# ================= GET TICKETS =================
@app.get("/tickets")
def get_tickets():
    tickets = (
        db.collection("tickets")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .stream()
    )
    result = []
    for ticket in tickets:
        data = ticket.to_dict()
        result.append({
            "id": ticket.id,
            "phone": data.get("phone", ""),
            "name": data.get("name", ""),
            "hostel_building": data.get("hostel_building", ""),
            "bucket": data.get("bucket", ""),
            "category": data.get("category", ""),
            "room": data.get("room", ""),
            "available_slot": data.get("available_slot", ""),
            "description": data.get("description", ""),
            "priority": data.get("priority", ""),
            "status": data.get("status", "Open"),
            "assigned_to": data.get("assigned_to", ""),
            "admin_comment": data.get("admin_comment", ""),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        })
    return result


# ================= UPDATE TICKET =================
class TicketUpdate(BaseModel):
    ticket_id: str
    status: str | None = None
    assigned_to: str | None = None
    admin_comment: str | None = None
    priority: str | None = None


@app.put("/update-ticket")
def update_ticket(data: TicketUpdate):
    ticket_ref = db.collection("tickets").document(data.ticket_id)
    ticket_doc = ticket_ref.get()

    if not ticket_doc.exists:
        return {"error": "Ticket not found"}

    ticket_data = ticket_doc.to_dict()
    update_data = {}

    if data.status:
        update_data["status"] = data.status
    if data.assigned_to is not None:
        update_data["assigned_to"] = data.assigned_to
    if data.admin_comment is not None:
        update_data["admin_comment"] = data.admin_comment
    if data.priority is not None:
        update_data["priority"] = data.priority

    update_data["updated_at"] = datetime.utcnow()
    ticket_ref.update(update_data)

    phone = ticket_data.get("phone")
    status = update_data.get("status", ticket_data.get("status"))
    technician_raw = update_data.get("assigned_to", ticket_data.get("assigned_to", "")).strip()
    comment = update_data.get("admin_comment", ticket_data.get("admin_comment", "")).strip()

    technician = get_technician_name(technician_raw)

    # Notify student if status or admin_comment changed

    should_notify = bool(data.status or data.admin_comment)

    if should_notify and phone:
        if status == "Closed":
            comment_line = f"\n{comment}" if comment else "\nIssue resolved successfully."
            send_text(phone, f"""✅ Issue Resolved

Ticket ID: {data.ticket_id}{comment_line}

If the issue persists, please raise a new complaint.""")
        else:
            comment_line = f"\n📝 {comment}" if comment else ""
            send_text(phone, f"""📢 Update on your complaint

Ticket ID: {data.ticket_id}
Status: {status}{comment_line}

We’ll keep you posted on further updates.""")


    # Notify technician if assigned_to changed
    if data.assigned_to and data.assigned_to.strip():
        tech_id_lookup = data.assigned_to.strip()
        print(f"TECH LOOKUP: assigned_to='{tech_id_lookup}' lower='{tech_id_lookup.lower()}'")
        tech_doc = db.collection("technicians").document(tech_id_lookup.lower()).get()
        print(f"TECH DOC EXISTS: {tech_doc.exists}")
        if tech_doc.exists:
            print(f"TECH DOC DATA: {tech_doc.to_dict()}")
        tech_phone = get_technician_phone(tech_id_lookup)
        print(f"TECH PHONE RESULT: '{tech_phone}'")
        if tech_phone:
            slot_line = f"\nAvailable: {ticket_data.get('available_slot')}" if ticket_data.get('available_slot') else ""
            send_text(tech_phone, f"""🔧 New Ticket Assigned to You

Ticket ID: {data.ticket_id}
Category: {ticket_data.get('category', '')}
Building: {ticket_data.get('hostel_building', '')}
Room: {ticket_data.get('room', '')}{slot_line}

Issue: {ticket_data.get('description', '')}

Please proceed at the earliest.""")
        else:
            print(f"TECH PHONE EMPTY: no message sent for '{tech_id_lookup}'")
    return {"message": "Ticket updated successfully", "updated_fields": update_data}


# ================= WEBHOOK VERIFY =================
@app.get("/webhook")
def verify(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge"))
    return PlainTextResponse("Verification failed", status_code=403)


# ================= HEALTH =================
@app.get("/health")
def health():
    return {"status": "ok"}


# ================= RECEIVE MESSAGE =================
@app.post("/webhook")
async def receive(request: Request):
    body = await request.json()
    print("FULL BODY:", body)

    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        messages = value.get("messages")

        if not messages:
            return {"status": "no message"}

        message = messages[0]
        phone = message["from"]
        msg_type = message["type"]

        convo_ref = db.collection("conversations").document(phone)
        convo = convo_ref.get().to_dict() or {}

        # ================= TEXT INPUT =================
        if msg_type == "text":
            text = message["text"]["body"].strip()
            text_lower = text.lower()

            if text_lower in ["hi", "hello", "menu"]:
                convo_ref.delete()
                send_main_menu(phone)
                return {"status": "ok"}

            step = convo.get("step")

            if step == "waiting_name":
                clean, err = validate_name(text)
                if err:
                    send_text(phone, f"❌ {err}")
                    return {"status": "ok"}
                convo_ref.set({"name": clean, "step": "waiting_building"}, merge=True)
                send_building_list(phone)
                return {"status": "ok"}

            elif step == "waiting_room":
                clean, err = validate_room(text)
                if err:
                    send_text(phone, f"❌ {err}")
                    return {"status": "ok"}
                # Room Specific (AC/Electrical/Furniture) goes to slot, all others go direct to description
                if convo.get("is_room_specific", False):
                    convo_ref.set({"room": clean, "step": "waiting_slot"}, merge=True)
                    send_text(phone, "📅 When are you available for resolution?\n\nEnter a date and time\n(e.g. Tomorrow 10am-12pm)")
                else:
                    convo_ref.set({"room": clean, "step": "waiting_description_direct"}, merge=True)
                    send_text(phone, "📝 Briefly describe the issue:")
                return {"status": "ok"}

            elif step == "waiting_slot":
                clean, err = validate_slot(text)
                if err:
                    send_text(phone, f"❌ {err}")
                    return {"status": "ok"}
                convo_ref.set({"available_slot": clean, "step": "waiting_description"}, merge=True)
                send_text(phone, "📝 Briefly describe the issue:")
                return {"status": "ok"}

            elif step == "waiting_description":
                clean, err = validate_description(text)
                if err:
                    send_text(phone, f"❌ {err}")
                    return {"status": "ok"}
                category = convo.get("category", "")
                convo_ref.set({"description": clean}, merge=True)
                auto_priority = classify_priority(category, clean)
                complete_ticket(phone, auto_priority)
                convo_ref.delete()
                return {"status": "ok"}

            elif step == "waiting_description_direct":
                clean, err = validate_description(text)
                if err:
                    send_text(phone, f"❌ {err}")
                    return {"status": "ok"}
                category = convo.get("category", "")
                convo_ref.set({"description": clean}, merge=True)
                auto_priority = classify_priority(category, clean)
                complete_ticket(phone, auto_priority)
                convo_ref.delete()
                return {"status": "ok"}

            elif step == "waiting_ticket_lookup":
                clean, err = validate_ticket_id(text)
                if err:
                    send_text(phone, f"❌ {err}")
                    return {"status": "ok"}
                fetch_ticket_status(phone, clean)
                convo_ref.delete()
                return {"status": "ok"}

            else:
                send_main_menu(phone)
                return {"status": "ok"}

        # ================= BUTTON / LIST INTERACTION =================
        elif msg_type == "interactive":
            interactive = message["interactive"]
            interactive_type = interactive.get("type")

            if interactive_type == "list_reply":
                selected = interactive["list_reply"]["id"]
            else:
                selected = interactive["button_reply"]["id"]

            # ---- GLOBAL BACK ----
            if selected == "back_main":
                convo_ref.delete()
                send_main_menu(phone)
                return {"status": "ok"}

            if selected == "back_bucket":
                send_bucket_buttons(phone)
                return {"status": "ok"}

            if selected == "back_hostel":
                send_hostel_menu(phone)
                return {"status": "ok"}

            # ---- MAIN MENU ----
            if selected == "raise":
                convo_ref.delete()
                convo_ref.set({"step": "waiting_name"})
                send_text(phone, "👋 Let's get started!\n\nPlease enter your *full name*:")
                return {"status": "ok"}

            elif selected == "status_check":
                convo_ref.delete()
                convo_ref.set({"step": "waiting_ticket_lookup"})
                send_text(phone, "🔍 Enter your *Ticket ID* to check status:\n\n(8-character code from your confirmation message, e.g. A3F9B2C1)")
                return {"status": "ok"}

            elif selected == "emergency":
                send_emergency_contacts(phone)
                return {"status": "ok"}

            # ---- BUILDING SELECTION (list) ----
            elif selected in ["bldg_lh", "bldg_b25", "bldg_b26",
                              "bldg_b27", "bldg_b29", "bldg_b30"]:
                labels = {
                    "bldg_lh":  "LH",
                    "bldg_b25": "B25",
                    "bldg_b26": "B26",
                    "bldg_b27": "B27",
                    "bldg_b29": "B29",
                    "bldg_b30": "B30",
                }
                convo_ref.set({
                    "hostel_building": labels[selected],
                    "step": "waiting_bucket"
                }, merge=True)
                send_bucket_buttons(phone)
                return {"status": "ok"}

            # ---- CATEGORY SELECTION ----

            # Hostel — show sub-menu
            elif selected == "cat_hostel":
                convo_ref.set({"bucket": "Hostel"}, merge=True)
                send_hostel_menu(phone)
                return {"status": "ok"}

            # Others — show others list
            elif selected == "cat_others":
                convo_ref.set({"bucket": "Others"}, merge=True)
                send_others_list(phone)
                return {"status": "ok"}

            # ---- OTHERS LIST ----
            elif selected == "cat_mess":
                convo_ref.set({
                    "category": "Mess & Food",
                    "is_room_specific": False,
                    "step": "waiting_description_direct"
                }, merge=True)
                send_text(phone, "🍽️ Describe the Mess & Food issue:")
                return {"status": "ok"}

            elif selected == "cat_rec":
                convo_ref.set({
                    "category": "Rec Centre",
                    "is_room_specific": False,
                    "step": "waiting_description_direct"
                }, merge=True)
                send_text(phone, "🏋️ Describe the Rec Centre issue:")
                return {"status": "ok"}

            elif selected == "cat_general":
                convo_ref.set({
                    "category": "General",
                    "is_room_specific": False,
                    "step": "waiting_description_direct"
                }, merge=True)
                send_text(phone, "📝 Describe your complaint:")
                return {"status": "ok"}

            # ---- HOSTEL SUB-MENU (buttons) ----
            elif selected == "hostel_room":
                # 2 options only — use buttons
                send_room_specific_buttons(phone)
                return {"status": "ok"}

            elif selected == "hostel_common":
                send_common_utilities_list(phone)
                return {"status": "ok"}

            # ---- ROOM SPECIFIC (list — 4 options) ----
            elif selected in ["cat_ac", "cat_electrical", "cat_furniture"]:
                labels = {
                    "cat_ac":          "AC",
                    "cat_electrical":  "Electrical",
                    "cat_furniture":   "Furniture",
                }
                convo_ref.set({
                    "category": labels[selected],
                    "is_room_specific": True,
                    "step": "waiting_room"
                }, merge=True)
                send_text(phone, "🚪 Enter your *room number*:")
                return {"status": "ok"}

            elif selected == "cat_wifi":
                convo_ref.set({
                    "category": "WiFi",
                    "is_room_specific": False,
                    "step": "waiting_room"
                }, merge=True)
                send_text(phone, "🚪 Enter your *room number*:")
                return {"status": "ok"}

            # ---- COMMON UTILITIES (list — 8 options) ----
            elif selected in ["cat_water_disp", "cat_fridge", "cat_oven",
                              "cat_geyser", "cat_vending", "cat_washing",
                              "cat_elevator", "cat_washroom"]:
                labels = {
                    "cat_water_disp": "Water Dispenser",
                    "cat_fridge":     "Fridge",
                    "cat_oven":       "Oven",
                    "cat_geyser":     "Geyser",
                    "cat_vending":    "Vending Machine",
                    "cat_washing":    "Washing Machine",
                    "cat_elevator":   "Elevator",
                    "cat_washroom":   "Washroom",
                }
                convo_ref.set({
                    "category": labels[selected],
                    "is_room_specific": False,
                    "step": "waiting_room"
                }, merge=True)
                send_text(phone, "🚪 Enter your *room number*:")
                return {"status": "ok"}

    except Exception as e:
        print("ERROR:", e)

    return {"status": "ok"}


# ================= MENUS =================

def send_main_menu(phone):
    send_buttons(phone, "👋 Welcome to Campus Companion!\n\nHow can we help you?", [
        ("raise",        "Raise Complaint"),
        ("status_check", "Check Ticket Status"),
        ("emergency",    "Emergency Contacts"),
    ])


def send_building_list(phone):
    send_list(
        phone,
        header="Hostel Block",
        body="Which block are you in?",
        button_label="Select Block",
        sections=[{
            "title": "Hostel Blocks",
            "rows": [
                {"id": "bldg_lh",  "title": "LH"},
                {"id": "bldg_b25", "title": "B25"},
                {"id": "bldg_b26", "title": "B26"},
                {"id": "bldg_b27", "title": "B27"},
                {"id": "bldg_b29", "title": "B29"},
                {"id": "bldg_b30", "title": "B30"},
            ],
        }],
    )


def send_bucket_buttons(phone):
    send_buttons(phone, "📂 What is your complaint about?", [
        ("cat_hostel", "Hostel"),
        ("cat_others", "Others"),
    ])


def send_hostel_menu(phone):
    send_buttons(phone, "🏠 Hostel — Select type:", [
        ("hostel_room",   "Room Specific"),
        ("hostel_common", "Common Utilities"),
        ("back_bucket",   "⬅ Back"),
    ])


def send_room_specific_buttons(phone):
    # 4 options — use list
    send_list(
        phone,
        header="Room Specific Issues",
        body="Select the type of issue in your room:",
        button_label="Select Issue",
        sections=[{
            "title": "Room Issues",
            "rows": [
                {"id": "cat_ac",         "title": "AC",          "description": "Not cooling, noisy, leaking, not working"},
                {"id": "cat_electrical", "title": "Electrical",  "description": "Switches, wiring, sockets, power points"},
                {"id": "cat_furniture",  "title": "Furniture",   "description": "Bed, table, chair, cupboard, fittings"},
                {"id": "cat_wifi",       "title": "WiFi",        "description": "Slow, not connecting, no internet"},
            ],
        }],
    )


def send_others_list(phone):
    # Others bucket — Mess & Food, Rec Centre, Other/General
    send_list(
        phone,
        header="Others",
        body="Select the type of complaint:",
        button_label="Select Type",
        sections=[{
            "title": "Other Complaints",
            "rows": [
                {"id": "cat_mess",    "title": "Mess & Food",    "description": "Food quality, hygiene, timings, menu"},
                {"id": "cat_rec",     "title": "Rec Centre",     "description": "Equipment, cleanliness, access"},
                {"id": "cat_general", "title": "Other / General","description": "Any other complaint not listed above"},
            ],
        }],
    )


def send_common_utilities_list(phone):
    # 8 options — list required
    send_list(
        phone,
        header="Common Utilities",
        body="Select the utility with an issue:",
        button_label="Select Utility",
        sections=[
            {
                "title": "Water & Appliances",
                "rows": [
                    {"id": "cat_water_disp", "title": "Water Dispenser",  "description": "Not working or water quality issue"},
                    {"id": "cat_fridge",     "title": "Fridge",           "description": "Common area fridge issue"},
                    {"id": "cat_oven",       "title": "Oven",             "description": "Not working or safety concern"},
                    {"id": "cat_geyser",     "title": "Geyser",           "description": "No hot water or fault"},
                    {"id": "cat_vending",    "title": "Vending Machine",  "description": "Stuck, not dispensing, payment"},
                    {"id": "cat_washing",    "title": "Washing Machine",  "description": "Not working or mid-cycle stop"},
                ],
            },
            {
                "title": "Infrastructure",
                "rows": [
                    {"id": "cat_elevator", "title": "Elevator",        "description": "Not working, stuck, noise"},
                    {"id": "cat_washroom", "title": "Washroom Issues", "description": "Flush, hygiene, cleaning, drain"},
                ],
            },
        ],
    )


def send_emergency_contacts(phone):
    send_text(phone, """🚨 Emergency Contacts

Campus Security: +91XXXXXXXXXX
Medical Emergency: +91XXXXXXXXXX
Hostel Warden: +91XXXXXXXXXX
Maintenance Emergency: +91XXXXXXXXXX

Type 'menu' to return to main menu.""")


# ================= WHATSAPP SENDERS =================

def send_buttons(phone, text, buttons):
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b[0], "title": b[1]}}
                    for b in buttons
                ],
            },
        },
    }
    send_whatsapp(data)


def send_list(phone, header, body, button_label, sections):
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": header},
            "body": {"text": body},
            "action": {
                "button": button_label,
                "sections": [
                    {
                        "title": s["title"],
                        "rows": [
                            {
                                "id": r["id"],
                                "title": r["title"],
                                **({"description": r["description"]} if r.get("description") else {}),
                            }
                            for r in s["rows"]
                        ],
                    }
                    for s in sections
                ],
            },
        },
    }
    send_whatsapp(data)


def send_text(phone, text):
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text},
    }
    send_whatsapp(data)


# ================= CREATE TICKET =================

def complete_ticket(phone, priority):
    convo = db.collection("conversations").document(phone).get().to_dict() or {}
    ticket_id = str(uuid.uuid4())[:8]
    is_room = convo.get("is_room_specific", False)

    db.collection("tickets").document(ticket_id).set({
        "phone": phone,
        "name": convo.get("name", ""),
        "hostel_building": convo.get("hostel_building", ""),
        "bucket": convo.get("bucket", ""),
        "category": convo.get("category", ""),
        "room": convo.get("room", ""),
        "available_slot": convo.get("available_slot", "") if is_room else "",
        "description": convo.get("description", ""),
        "priority": priority,
        "status": "Open",
        "assigned_to": "",
        "admin_comment": "",
        "created_at": datetime.utcnow(),
        "updated_at": None,
    })

    # Ticket number only — no extra details shown to student
    send_text(phone, f"✅ Complaint registered!\n\nYour Ticket ID: *{ticket_id}*\n\nWe'll update you on WhatsApp once there's a status change.")


# ================= TICKET STATUS LOOKUP =================

def fetch_ticket_status(phone, ticket_id):
    doc = db.collection("tickets").document(ticket_id).get()

    if not doc.exists:
        send_text(phone, f"❌ Ticket *{ticket_id}* not found.\n\nPlease check the ID and try again, or type 'menu' to go back.")
        return

    data = doc.to_dict()
    status    = data.get("status", "Open")
    category  = data.get("category", "")
    comment   = data.get("admin_comment", "").strip()
    assigned  = data.get("assigned_to", "").strip()
    updated   = data.get("updated_at")

    technician = get_technician_name(assigned) if assigned else ""

    lines = [
        f"📋 Ticket Status",
        f"",
        f"Ticket ID: {ticket_id}",
        f"Category: {category}",
        f"Status: {status}",
    ]
    if comment:
        lines.append(f"Admin Note: {comment}")

    lines.append("")
    lines.append("Type 'menu' to go back to main menu.")

    send_text(phone, "\n".join(lines))


# ================= WHATSAPP API =================

def send_whatsapp(data):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, json=data)
    print("WHATSAPP STATUS:", response.status_code)
    print("WHATSAPP RESPONSE:", response.text)
