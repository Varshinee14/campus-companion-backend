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
#
# HOW IT WORKS:
#   1. PRIMARY   — typeform/distilbart-mnli-12-3 (fast ~0.8s warm)
#   2. SECONDARY — facebook/bart-large-mnli (larger, ~2-4s warm)
#   3. FALLBACK  — keyword matching (instant, always works)
#
# 5 second timeout per model — student never waits more than ~6s total

HF_CANDIDATE_LABELS = [
    "fire, smoke, or safety emergency",
    "water leakage, flooding, or water supply failure",
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
        "flood", "flooding", "leaking", "water leak", "no water",
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
                print(f"HF [{model}] failed: {e} — trying next")
    print("Using keyword fallback")
    return _classify_keywords(category, description)


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
            "name": data.get("name", ""),
            "hostel_building": data.get("hostel_building", ""),
            "bucket": data.get("bucket", ""),
            "category": data.get("category", ""),
            "room": data.get("room", ""),
            "description": data.get("description", ""),
            "available_slot": data.get("available_slot", ""),
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
    technician = update_data.get("assigned_to", ticket_data.get("assigned_to"))
    comment = update_data.get("admin_comment", ticket_data.get("admin_comment"))

    if status == "Closed":
        send_text(phone, f"""
✅ Issue Resolved

Ticket ID: {data.ticket_id}

Admin Note:
{comment if comment else "Issue resolved successfully."}

If the issue persists, please raise another complaint.
""")
    else:
        send_text(phone, f"""
📢 Ticket Update

Ticket ID: {data.ticket_id}
Status: {status}
Assigned To: {technician if technician else "Pending"}

Admin Note:
{comment if comment else "No additional notes"}

You will receive updates automatically on WhatsApp.
""")

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

            # Always allow reset
            if text_lower in ["hi", "hello", "menu"]:
                convo_ref.delete()
                send_main_menu(phone)
                return {"status": "ok"}

            step = convo.get("step")

            if step == "waiting_name":
                convo_ref.set({"name": text, "step": "waiting_building"}, merge=True)
                send_building_list(phone)
                return {"status": "ok"}

            elif step == "waiting_room":
                convo_ref.set({"room": text, "step": "waiting_slot"}, merge=True)
                send_text(phone, "📅 When are you available for the issue to be resolved?\n\nPlease mention a date and time\n(e.g. Tomorrow 10am - 12pm, or Today after 6pm)")
                return {"status": "ok"}

            elif step == "waiting_slot":
                convo_ref.set({"available_slot": text, "step": "waiting_description"}, merge=True)
                send_text(phone, "📝 Describe the issue briefly:")
                return {"status": "ok"}

            elif step == "waiting_description":
                description = text
                category = convo.get("category", "")
                convo_ref.set({"description": description}, merge=True)
                auto_priority = classify_priority(category, description)
                complete_ticket(phone, auto_priority)
                convo_ref.delete()
                return {"status": "ok"}

            # Non-room-specific path — no room/slot steps
            elif step == "waiting_description_direct":
                description = text
                category = convo.get("category", "")
                convo_ref.set({"description": description}, merge=True)
                auto_priority = classify_priority(category, description)
                complete_ticket(phone, auto_priority)
                convo_ref.delete()
                return {"status": "ok"}

            else:
                send_main_menu(phone)
                return {"status": "ok"}

        # ================= BUTTON INTERACTION =================
        elif msg_type == "interactive":
            interactive = message["interactive"]
            interactive_type = interactive.get("type")

            # ── List reply ──
            if interactive_type == "list_reply":
                selected = interactive["list_reply"]["id"]
            # ── Button reply ──
            else:
                selected = interactive["button_reply"]["id"]

            # ---- GLOBAL BACK HANDLERS ----
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
                # Start with name collection
                send_text(phone, "👋 Let's get started!\n\nPlease enter your *full name*:")
                convo_ref.set({"step": "waiting_name"})
                return {"status": "ok"}

            elif selected == "emergency":
                send_emergency_contacts(phone)
                return {"status": "ok"}

            # ---- BUCKET SELECTION ----
            elif selected == "bucket_mess":
                convo_ref.set({"bucket": "Mess & Food", "category": "mess_food", "step": "waiting_description_direct"}, merge=True)
                send_text(phone, "🍽️ Describe your Mess & Food issue:\n\n(e.g. food quality, hygiene, menu, timings)")
                return {"status": "ok"}

            elif selected == "bucket_hostel":
                convo_ref.set({"bucket": "Hostel"}, merge=True)
                send_hostel_menu(phone)
                return {"status": "ok"}

            elif selected == "bucket_it":
                convo_ref.set({"bucket": "IT & Infra"}, merge=True)
                send_it_menu(phone)
                return {"status": "ok"}

            # ---- HOSTEL SUB-MENU ----
            elif selected == "hostel_room":
                send_room_specific_list(phone)
                return {"status": "ok"}

            elif selected == "hostel_common":
                send_common_utilities_list(phone)
                return {"status": "ok"}

            # ---- ROOM SPECIFIC (list selections) ----
            elif selected in ["cat_electrical_ac", "cat_furniture"]:
                labels = {
                    "cat_electrical_ac": "Electrical / AC",
                    "cat_furniture": "Furniture",
                }
                convo_ref.set({
                    "category": selected,
                    "category_label": labels[selected],
                    "is_room_specific": True,
                    "step": "waiting_room"
                }, merge=True)
                send_text(phone, "🚪 Enter your *room number*:")
                return {"status": "ok"}

            # ---- COMMON UTILITIES (list selections) ----
            elif selected in ["cat_water_disp", "cat_fridge", "cat_oven",
                              "cat_geyser", "cat_vending", "cat_washing",
                              "cat_elevator", "cat_washroom"]:
                labels = {
                    "cat_water_disp": "Water Dispenser",
                    "cat_fridge": "Fridge",
                    "cat_oven": "Oven",
                    "cat_geyser": "Geyser",
                    "cat_vending": "Vending Machine",
                    "cat_washing": "Washing Machine",
                    "cat_elevator": "Elevator",
                    "cat_washroom": "Washroom Issues",
                }
                convo_ref.set({
                    "category": selected,
                    "category_label": labels[selected],
                    "is_room_specific": False,
                    "step": "waiting_description_direct"
                }, merge=True)
                send_text(phone, f"📝 Describe the *{labels[selected]}* issue:")
                return {"status": "ok"}

            # ---- IT / INFRA ----
            elif selected == "cat_wifi":
                convo_ref.set({
                    "category": "cat_wifi",
                    "category_label": "WiFi",
                    "is_room_specific": False,
                    "step": "waiting_description_direct"
                }, merge=True)
                send_text(phone, "📶 Describe the WiFi issue:")
                return {"status": "ok"}

            elif selected == "cat_rec_centre":
                convo_ref.set({
                    "category": "cat_rec_centre",
                    "category_label": "Rec Centre",
                    "is_room_specific": False,
                    "step": "waiting_description_direct"
                }, merge=True)
                send_text(phone, "🏋️ Describe the Rec Centre issue:")
                return {"status": "ok"}

            # ---- BUILDING SELECTION (list reply) ----
            elif selected in ["bldg_a", "bldg_b", "bldg_c", "bldg_d", "bldg_e"]:
                labels = {
                    "bldg_a": "Block A",
                    "bldg_b": "Block B",
                    "bldg_c": "Block C",
                    "bldg_d": "Block D",
                    "bldg_e": "Block E",
                }
                convo_ref.set({
                    "hostel_building": labels[selected],
                    "step": "waiting_bucket"
                }, merge=True)
                send_bucket_buttons(phone)
                return {"status": "ok"}

    except Exception as e:
        print("ERROR:", e)

    return {"status": "ok"}


# ================= MENUS =================

def send_main_menu(phone):
    send_buttons(phone, "👋 Welcome to Campus Companion!\n\nHow can we help you today?", [
        ("raise", "Raise Complaint"),
        ("emergency", "Emergency Contacts"),
    ])


def send_bucket_buttons(phone):
    send_buttons(phone, "📂 Select complaint category:", [
        ("bucket_mess", "Mess & Food"),
        ("bucket_hostel", "Hostel"),
        ("bucket_it", "IT & Infra"),
    ])


def send_hostel_menu(phone):
    send_buttons(phone, "🏠 Hostel — Select type:", [
        ("hostel_room", "Room Specific"),
        ("hostel_common", "Common Utilities"),
        ("back_bucket", "⬅ Back"),
    ])


def send_it_menu(phone):
    send_buttons(phone, "💻 IT & Infrastructure:", [
        ("cat_wifi", "WiFi"),
        ("cat_rec_centre", "Rec Centre Issues"),
        ("back_bucket", "⬅ Back"),
    ])


def send_room_specific_list(phone):
    # 2 options — list still works, keeps UI consistent
    send_list(
        phone,
        header="Room Specific Issues",
        body="Select the type of issue in your room:",
        button_label="Select Issue",
        sections=[
            {
                "title": "Room Issues",
                "rows": [
                    {"id": "cat_electrical_ac", "title": "Electrical / AC",     "description": "AC not working, wiring, switches"},
                    {"id": "cat_furniture",     "title": "Furniture",           "description": "Bed, table, chair, cupboard"},
                ],
            }
        ],
    )


def send_common_utilities_list(phone):
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
                    {"id": "cat_oven",       "title": "Oven",             "description": "Oven not working or safety concern"},
                    {"id": "cat_geyser",     "title": "Geyser",           "description": "No hot water or geyser fault"},
                    {"id": "cat_vending",    "title": "Vending Machine",  "description": "Stuck, not dispensing, payment issue"},
                    {"id": "cat_washing",    "title": "Washing Machine",  "description": "Not working or mid-cycle stop"},
                ],
            },
            {
                "title": "Infrastructure",
                "rows": [
                    {"id": "cat_elevator",   "title": "Elevator",         "description": "Not working, stuck, noise"},
                    {"id": "cat_washroom",   "title": "Washroom Issues",  "description": "Flush, hygiene, cleaning, drain"},
                ],
            },
        ],
    )


def send_building_list(phone):
    send_list(
        phone,
        header="Hostel Building",
        body="Which hostel block are you in?",
        button_label="Select Block",
        sections=[
            {
                "title": "Hostel Blocks",
                "rows": [
                    {"id": "bldg_a", "title": "Block A", "description": ""},
                    {"id": "bldg_b", "title": "Block B", "description": ""},
                    {"id": "bldg_c", "title": "Block C", "description": ""},
                    {"id": "bldg_d", "title": "Block D", "description": ""},
                    {"id": "bldg_e", "title": "Block E", "description": ""},
                ],
            }
        ],
    )


def send_emergency_contacts(phone):
    send_text(phone, """
🚨 Emergency Contacts

Campus Security: +91XXXXXXXXXX
Medical Emergency: +91XXXXXXXXXX
Hostel Warden: +91XXXXXXXXXX
Maintenance Emergency: +91XXXXXXXXXX

Type 'menu' anytime to return to main menu.
""")


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
                ]
            },
        },
    }
    send_whatsapp(data)


def send_list(phone, header, body, button_label, sections):
    """
    Sends a WhatsApp List Message — supports up to 10 rows across sections.
    Used when options exceed 3 (button limit).
    """
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
                                **({"description": r["description"]} if r.get("description") else {})
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
    priority_emoji = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(priority, "🟢")
    is_room = convo.get("is_room_specific", False)

    db.collection("tickets").document(ticket_id).set({
        "phone": phone,
        "name": convo.get("name", ""),
        "hostel_building": convo.get("hostel_building", ""),
        "bucket": convo.get("bucket", ""),
        "category": convo.get("category", ""),
        "category_label": convo.get("category_label", ""),
        "room": convo.get("room", "") if is_room else "",
        "available_slot": convo.get("available_slot", "") if is_room else "",
        "description": convo.get("description", ""),
        "priority": priority,
        "status": "Open",
        "assigned_to": "",
        "admin_comment": "",
        "created_at": datetime.utcnow(),
        "updated_at": None,
    })

    # Build confirmation — show slot only for room-specific issues
    slot_line = f"🕐 Available: {convo.get('available_slot', '')}\n" if is_room else ""

    send_text(phone, f"""
✅ Complaint Registered

Ticket ID: {ticket_id}
Name: {convo.get('name', '')}
Building: {convo.get('hostel_building', '')}
Category: {convo.get('category_label', '')}
{slot_line}Priority: {priority_emoji} {priority}

Our team will review your issue and update you on WhatsApp automatically.
""")


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

