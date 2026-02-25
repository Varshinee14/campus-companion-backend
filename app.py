from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, HTMLResponse
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
    allow_origins=["*"],  # Restrict later to your admin UI domain
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

# ================= JSON API FOR ADMIN UI =================
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
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at")
        })

    return result


# ================= UPDATE TICKET =================
class TicketUpdate(BaseModel):
    ticket_id: str
    status: str | None = None
    assigned_to: str | None = None


@app.put("/update-ticket")
def update_ticket(data: TicketUpdate):
    ticket_ref = db.collection("tickets").document(data.ticket_id)

    update_data = {}

    if data.status:
        update_data["status"] = data.status

    if data.assigned_to is not None:
        update_data["assigned_to"] = data.assigned_to

    update_data["updated_at"] = datetime.utcnow()

    ticket_ref.update(update_data)

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

# ================= HEALTH CHECK =====================
@app.get("/health")
def health():
    return {"status": "ok"}

# ================= RECEIVE WHATSAPP =================
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
                convo_ref.set({"description": text, "step": "waiting_priority"}, merge=True)
                send_priority_buttons(phone)
                return {"status": "ok"}

            elif convo.get("step") == "waiting_ticket_lookup":
                fetch_ticket_status(phone, text)
                return {"status": "ok"}

            else:
                send_main_menu(phone)
                return {"status": "ok"}

        # ================= BUTTONS =================
        elif msg_type == "interactive":
            selected = message["interactive"]["button_reply"]["id"]

            # ---- GLOBAL BACK HANDLERS ----
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

            # ---- MAIN FLOW ----
            if selected == "raise":
                send_bucket_buttons(phone)

            elif selected == "enquire":
                convo_ref.set({"step": "waiting_ticket_lookup"}, merge=True)
                send_text(phone, "Enter Ticket ID:")

            elif selected == "hostel":
                convo_ref.set({"bucket": "Hostel"}, merge=True)
                send_hostel_main(phone)

            elif selected == "acad_fac":
                convo_ref.set({"bucket": "Acad & Fac"}, merge=True)
                send_acad_fac(phone)

            elif selected == "electrical":
                send_electrical_options(phone)

            elif selected == "utilities":
                send_utilities_options(phone)

            elif selected in ["ac", "geyser", "wash_mach",
                              "wifi", "water_disp", "cleaning"]:
                convo_ref.set({"category": selected, "step": "waiting_room"}, merge=True)
                send_text(phone, "Enter Room No:")

            elif selected in ["high", "medium", "low"]:
                complete_ticket(phone, selected)
                convo_ref.delete()

    except Exception as e:
        print("ERROR:", e)

    return {"status": "ok"}


# ================= MENUS =================
def send_main_menu(phone):
    send_buttons(phone, "Choose option:", [
        ("raise", "Raise Complaint"),
        ("enquire", "Enquire Ticket")
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


def send_priority_buttons(phone):
    send_buttons(phone, "Select Priority:", [
        ("high", "High"),
        ("medium", "Medium"),
        ("back_main", "⬅ Cancel")
    ])


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


# ================= TICKET CREATION =================
def complete_ticket(phone, priority):
    convo = db.collection("conversations").document(phone).get().to_dict()
    ticket_id = str(uuid.uuid4())[:8]

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
        "created_at": datetime.utcnow(),
        "updated_at": None
    })

    send_text(phone, f"Ticket {ticket_id} created successfully!")


def fetch_ticket_status(phone, ticket_id):
    doc = db.collection("tickets").document(ticket_id).get()

    if not doc.exists:
        send_text(phone, "Ticket not found.")
        return

    data = doc.to_dict()
    send_text(phone, f"Status: {data.get('status')}")


# ================= WHATSAPP =================
def send_text(phone, text):
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    send_whatsapp(data)


def send_whatsapp(data):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=data)

    print("WHATSAPP STATUS:", response.status_code)
    print("WHATSAPP RESPONSE:", response.text)
