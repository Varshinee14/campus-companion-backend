from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, HTMLResponse
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json
import requests
import uuid
from datetime import datetime

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Later restrict to your UI domain
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

# ================= DASHBOARD =================
@app.get("/", response_class=HTMLResponse)
def dashboard():
    tickets = (
        db.collection("tickets")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .stream()
    )

    html = "<h1>🏫 Campus Companion Admin</h1><hr>"

    tickets_list = list(tickets)

    if len(tickets_list) == 0:
        html += "<h3>No tickets found.</h3>"
    else:
        for ticket in tickets_list:
            data = ticket.to_dict()
            html += f"""
            <div style='border:1px solid #ccc;padding:10px;margin-bottom:10px'>
                <b>ID:</b> {ticket.id}<br>
                <b>Bucket:</b> {data.get('bucket', '')}<br>
                <b>Category:</b> {data.get('category', '')}<br>
                <b>Room:</b> {data.get('room', '')}<br>
                <b>Roll:</b> {data.get('roll_number', '')}<br>
                <b>Description:</b> {data.get('description', '')}<br>
                <b>Priority:</b> {data.get('priority', '')}<br>
                <b>Status:</b> {data.get('status', 'Open')}<br>
                <b>Assigned To:</b> {data.get('assigned_to', '')}<br>
            </div>
            """

    return html

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

# ================= UPDATE TICKET (NEW) =================
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

# ================= RECEIVE =================
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

            if selected == "raise":
                send_bucket_buttons(phone)

            elif selected == "enquire":
                convo_ref.set({"step": "waiting_ticket_lookup"}, merge=True)
                send_text(phone, "Enter Ticket ID:")

            elif selected == "hostel":
                convo_ref.set({"bucket": "Hostel"}, merge=True)
                send_hostel_main(phone)

            elif selected in ["ac", "geyser", "wash_mach"]:
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
        ("mess", "Mess")
    ])

def send_hostel_main(phone):
    send_buttons(phone, "Hostel Category:", [
        ("ac", "AC"),
        ("geyser", "Geyser"),
        ("wash_mach", "Wash Mach")
    ])

def send_priority_buttons(phone):
    send_buttons(phone, "Select Priority:", [
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low")
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
