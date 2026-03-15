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
#   1. PRIMARY  — typeform/distilbart-mnli-12-3 (distilled, fast ~0.8s warm)
#   2. SECONDARY — facebook/bart-large-mnli (larger, ~2-4s warm, only if primary fails)
#   3. FALLBACK  — keyword matching (instant, fires if both APIs fail or no HF_TOKEN)
#
# Zero-shot NLI: model scores how well the description "entails" each candidate
# label. No training needed — works on paragraphs, indirect language, typos.
#
# LATENCY PROTECTION:
#   - 5 second timeout on all API calls — student never waits more than that
#   - If model is cold-starting, HF returns {"error": "loading"} — we catch and fallback
#   - keyword fallback is instant so worst case is always < 1 sec

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

# Two models tried in order — distilbart is 10x smaller and faster
HF_MODELS = [
    "typeform/distilbart-mnli-12-3",      # PRIMARY: ~0.8s warm, small
    "facebook/bart-large-mnli",           # SECONDARY: ~2-4s warm, larger
]

# Keyword fallback — instant, fires if both HF models fail or no token set
KEYWORD_RULES = [
    ("High", [
        "fire", "smoke", "burning", "flame",
        "flood", "flooding", "leaking", "water leak", "no water", "water not coming",
        "contaminated", "dirty water", "smell", "yellowish", "unsafe to drink",
        "flush", "flush not working", "toilet overflow", "sewage", "drain overflow",
        "lift stuck", "lift not working", "elevator stuck", "elevator not working",
        "short circuit", "electric shock", "no electricity", "power cut", "sparks", "blackening",
        "urgent", "emergency", "immediately", "asap",
    ]),
    ("Medium", [
        "wifi", "internet not working", "no internet",
        "water dispenser", "dispenser not working",
        "washing machine", "washer",
        "ac not working", "no cooling", "geyser not working", "no hot water",
        "fan not working", "lights not working", "switch not working",
        "mess food", "food quality", "cold food",
    ]),
]


def _call_hf_model(model: str, text: str) -> str:
    """
    Calls one HF model. Returns priority string or raises on any failure.
    Timeout = 5s so student never waits more than this per attempt.
    """
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}

    response = requests.post(
        url,
        headers=headers,
        json={
            "inputs": text,
            "parameters": {"candidate_labels": HF_CANDIDATE_LABELS},
        },
        timeout=5,
    )

    response.raise_for_status()
    result = response.json()

    # HF returns loading error as a dict with "error" key — not an HTTP error
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"HF model loading: {result['error']}")

    top_label = result["labels"][0]
    top_score = result["scores"][0]
    priority = LABEL_PRIORITY_MAP.get(top_label, "Low")

    print(f"HF [{model}] label={top_label!r} score={top_score:.2f} priority={priority}")
    return priority


def _classify_keywords(category: str, description: str) -> str:
    """Instant keyword fallback."""
    text = f"{category} {description}".lower()
    for priority, keywords in KEYWORD_RULES:
        for kw in keywords:
            if kw in text:
                print(f"KEYWORD FALLBACK: {kw!r} -> {priority}")
                return priority
    return "Low"


def classify_priority(category: str, description: str) -> str:
    """
    Main classifier entry point.
    Tries HF models in order, falls back to keywords on any failure.
    Always returns within ~5 seconds maximum.
    """
    if HF_TOKEN:
        text = f"{category}: {description}"
        for model in HF_MODELS:
            try:
                return _call_hf_model(model, text)
            except Exception as e:
                print(f"HF [{model}] failed: {e} — trying next")

    # Both models failed or no token — keyword fallback
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
            "bucket": data.get("bucket", ""),
            "category": data.get("category", ""),
            "room": data.get("room", ""),
            "roll_number": data.get("roll_number", ""),
            "description": data.get("description", ""),
            "priority": data.get("priority", ""),
            "status": data.get("status", "Open"),
            "assigned_to": data.get("assigned_to", ""),
            "admin_comment": data.get("admin_comment", ""),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at")
        })

    return result


# ================= UPDATE TICKET =================

class TicketUpdate(BaseModel):
    ticket_id: str
    status: str | None = None
    assigned_to: str | None = None
    admin_comment: str | None = None
    priority: str | None = None  # Admin can override auto-classified priority


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

    return {
        "message": "Ticket updated successfully",
        "updated_fields": update_data
    }


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

        # ================= TEXT =================
        if msg_type == "text":

            text = message["text"]["body"].strip().lower()

            if text in ["hi", "hello", "menu"]:
                convo_ref.delete()
                send_main_menu(phone)
                return {"status": "ok"}

            if convo.get("step") == "waiting_room":
                convo_ref.set({"room": text, "step": "waiting_roll"}, merge=True)
                send_text(phone, "Enter Roll No:")
                return {"status": "ok"}

            elif convo.get("step") == "waiting_roll":
                convo_ref.set({"roll_number": text, "step": "waiting_description"}, merge=True)
                send_text(phone, "Describe issue briefly:")
                return {"status": "ok"}

            elif convo.get("step") == "waiting_description":
                description = text
                category = convo.get("category", "")
                # Save description first, then classify and create ticket
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

            selected = message["interactive"]["button_reply"]["id"]

            if selected == "back_main":
                convo_ref.delete()
                send_main_menu(phone)
                return {"status": "ok"}

            if selected == "back_bucket":
                send_bucket_buttons(phone)
                return {"status": "ok"}

            if selected == "back_hostel":
                send_hostel_main(phone)
                return {"status": "ok"}

            if selected == "back_acad":
                send_acad_fac(phone)
                return {"status": "ok"}

            # START NEW COMPLAINT (RESET STATE)
            if selected == "raise":
                convo_ref.delete()
                send_bucket_buttons(phone)

            elif selected == "emergency":
                send_emergency_contacts(phone)

            elif selected == "hostel":
                convo_ref.set({
                    "bucket": "Hostel",
                    "category": None
                }, merge=True)
                send_hostel_main(phone)

            elif selected == "acad_fac":
                convo_ref.set({
                    "bucket": "Acad & Fac",
                    "category": None
                }, merge=True)
                send_acad_fac(phone)

            elif selected == "electrical":
                send_electrical_options(phone)

            elif selected == "utilities":
                send_utilities_options(phone)

            elif selected in ["ac", "geyser", "wash_mach", "wifi", "water_disp", "cleaning"]:
                convo_ref.set({
                    "category": selected,
                    "step": "waiting_room"
                }, merge=True)
                send_text(phone, "Enter Room No:")

    except Exception as e:
        print("ERROR:", e)

    return {"status": "ok"}


# ================= MENUS =================

def send_main_menu(phone):
    send_buttons(phone, "Choose option:", [
        ("raise", "Raise Complaint"),
        ("emergency", "Emergency Contacts")
    ])


def send_bucket_buttons(phone):
    send_buttons(phone, "Select Category:", [
        ("hostel", "Hostel"),
        ("acad_fac", "Acad & Fac"),
        ("back_main", "⬅ Main Menu")
    ])


def send_hostel_main(phone):
    send_buttons(phone, "Hostel Category:", [
        ("electrical", "Electrical"),
        ("utilities", "Utilities"),
        ("back_bucket", "⬅ Back")
    ])


def send_electrical_options(phone):
    send_buttons(phone, "Electrical Issue:", [
        ("ac", "AC"),
        ("geyser", "Geyser"),
        ("back_hostel", "⬅ Back")
    ])


def send_utilities_options(phone):
    send_buttons(phone, "Utility Issue:", [
        ("wifi", "WiFi"),
        ("water_disp", "Water Disp"),
        ("back_hostel", "⬅ Back")
    ])


def send_acad_fac(phone):
    send_buttons(phone, "Select Type:", [
        ("infra", "Infra Issues"),
        ("mess", "Mess Issues"),
        ("back_bucket", "⬅ Back")
    ])


def send_emergency_contacts(phone):

    send_text(phone, """
🚨 Emergency Contacts

Campus Security: +91XXXXXXXXXX
Medical Emergency: +91XXXXXXXXXX
Hostel Warden: +91XXXXXXXXXX
Maintenance Emergency: +91XXXXXXXXXX

Type 'menu' anytime to return to main menu.
""")


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
            }
        }
    }

    send_whatsapp(data)


# ================= CREATE TICKET =================

def complete_ticket(phone, priority):

    convo = db.collection("conversations").document(phone).get().to_dict() or {}

    ticket_id = str(uuid.uuid4())[:8]
    priority_emoji = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(priority, "🟢")

    db.collection("tickets").document(ticket_id).set({
        "phone": phone,
        "bucket": convo.get("bucket"),
        "category": convo.get("category"),
        "room": convo.get("room"),
        "roll_number": convo.get("roll_number"),
        "description": convo.get("description"),
        "priority": priority,
        "status": "Open",
        "assigned_to": "",
        "admin_comment": "",
        "created_at": datetime.utcnow(),
        "updated_at": None
    })

    send_text(phone, f"""
✅ Complaint Registered

Ticket ID: {ticket_id}
Priority: {priority_emoji} {priority}

Our team will review your issue and update you on WhatsApp automatically.
""")


# ================= SEND TEXT =================

def send_text(phone, text):

    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }

    send_whatsapp(data)


# ================= WHATSAPP API =================

def send_whatsapp(data):

    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=data)

    print("WHATSAPP STATUS:", response.status_code)
    print("WHATSAPP RESPONSE:", response.text)
