from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, HTMLResponse
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json
import requests
import uuid

app = FastAPI()

# 🔐 Load Firebase
firebase_key = json.loads(os.environ["FIREBASE_KEY"])

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key)
    firebase_admin.initialize_app(cred)

db = firestore.client()

VERIFY_TOKEN = "campusbot"
PHONE_NUMBER_ID = "946946368512302"
ACCESS_TOKEN = os.environ["WHATSAPP_TOKEN"]

# =========================
# 🔹 ADMIN DASHBOARD
# =========================
@app.get("/", response_class=HTMLResponse)
def dashboard():
    tickets = list(db.collection("tickets").stream())

    html = """
    <html>
    <head>
        <title>Campus Companion Admin</title>
        <style>
            body { font-family: Arial; margin: 40px; }
            .ticket { border: 1px solid #ccc; padding: 15px; margin-bottom: 15px; }
        </style>
    </head>
    <body>
        <h1>🏫 Campus Companion - Admin Dashboard</h1>
        <hr>
    """

    if len(tickets) == 0:
        html += "<h3>No tickets found.</h3>"
    else:
        for ticket in tickets:
            data = ticket.to_dict()
            html += f"""
            <div class="ticket">
                <b>Ticket ID:</b> {ticket.id}<br>
                <b>Bucket:</b> {data.get('bucket')}<br>
                <b>Category:</b> {data.get('category')}<br>
                <b>Room:</b> {data.get('room')}<br>
                <b>Roll:</b> {data.get('roll_number')}<br>
                <b>Description:</b> {data.get('description')}<br>
                <b>Priority:</b> {data.get('priority')}<br>
                <b>Status:</b> {data.get('status')}<br>
            </div>
            """

    html += "</body></html>"
    return html

# =========================
# 🔹 WEBHOOK VERIFICATION
# =========================
@app.get("/webhook")
def verify(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge"))
    return PlainTextResponse("Verification failed", status_code=403)

# =========================
# 🔹 RECEIVE MESSAGES
# =========================
@app.post("/webhook")
async def receive(request: Request):
    body = await request.json()

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

            # 🔁 Allow restart anytime
            if text in ["hi", "hello", "menu"]:
                convo_ref.delete()
                send_main_menu(phone)
                return {"status": "ok"}

            if convo.get("step") == "waiting_room":
                convo_ref.set({"room": text, "step": "waiting_roll"}, merge=True)
                send_text(phone, "Enter Roll Number:")
                return {"status": "ok"}

            elif convo.get("step") == "waiting_roll":
                convo_ref.set({"roll_number": text, "step": "waiting_description"}, merge=True)
                send_text(phone, "Briefly describe the issue:")
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
                send_text(phone, "Enter your Ticket ID:")

            elif selected in ["academic", "hostel", "mess"]:
                convo_ref.set({"bucket": selected}, merge=True)

                if selected == "hostel":
                    send_hostel_options(phone)
                elif selected == "mess":
                    convo_ref.set({"category": "Food Quality", "step": "waiting_room"}, merge=True)
                    send_text(phone, "Enter Room Number:")
                elif selected == "academic":
                    send_academic_options(phone)

            elif selected in ["ac", "wifi", "water_dispenser", "geyser", "washing_machine", "fridge", "oven", "cleaning"]:
                convo_ref.set({"category": selected, "step": "waiting_room"}, merge=True)
                send_text(phone, "Enter Room Number:")

            elif selected in ["it_help", "room_booking", "recreation"]:
                if selected == "recreation":
                    send_recreation_options(phone)
                else:
                    convo_ref.set({"category": selected, "step": "waiting_roll"}, merge=True)
                    send_text(phone, "Enter Roll Number:")

            elif selected in ["gym", "terrace", "yoga"]:
                convo_ref.set({"category": selected, "step": "waiting_roll"}, merge=True)
                send_text(phone, "Enter Roll Number:")

            elif selected in ["high", "medium", "low"]:
                complete_ticket(phone, selected)
                convo_ref.delete()

    except Exception as e:
        print("ERROR:", e)

    return {"status": "ok"}

# =========================
# 🔹 MENU FUNCTIONS
# =========================
def send_main_menu(phone):
    send_buttons(phone, "Choose an option:", [
        ("raise", "Raise Complaint"),
        ("enquire", "Enquire Ticket")
    ])

def send_bucket_buttons(phone):
    send_buttons(phone, "Select Bucket:", [
        ("academic", "Academic"),
        ("hostel", "Hostel"),
        ("mess", "Mess")
    ])

def send_hostel_options(phone):
    send_buttons(phone, "Select Hostel Issue:", [
        ("ac", "AC"),
        ("wifi", "WiFi"),
        ("water_dispenser", "Water Dispenser")
    ])

def send_academic_options(phone):
    send_buttons(phone, "Select Academic Issue:", [
        ("it_help", "IT Help"),
        ("room_booking", "Room Booking"),
        ("recreation", "Recreation Centre")
    ])

def send_recreation_options(phone):
    send_buttons(phone, "Select Recreation Option:", [
        ("gym", "Gym"),
        ("terrace", "Terrace"),
        ("yoga", "Yoga Room")
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

# =========================
# 🔹 TICKET FUNCTIONS
# =========================
def complete_ticket(phone, priority):
    convo = db.collection("conversations").document(phone).get().to_dict()
    ticket_id = str(uuid.uuid4())[:8]

    sla_map = {"high": 2, "medium": 6, "low": 24}

    db.collection("tickets").document(ticket_id).set({
        "phone": phone,
        "bucket": convo.get("bucket"),
        "category": convo.get("category"),
        "room": convo.get("room"),
        "roll_number": convo.get("roll_number"),
        "description": convo.get("description"),
        "priority": priority,
        "sla_hours": sla_map[priority],
        "status": "Open"
    })

    send_text(phone, f"Ticket {ticket_id} created successfully!")

def fetch_ticket_status(phone, ticket_id):
    doc = db.collection("tickets").document(ticket_id).get()

    if not doc.exists:
        send_text(phone, "Ticket not found.")
        return

    data = doc.to_dict()
    send_text(
        phone,
        f"Status: {data.get('status')}\nPriority: {data.get('priority')}\nSLA: {data.get('sla_hours')} hrs"
    )

# =========================
# 🔹 WHATSAPP API
# =========================
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
    requests.post(url, headers=headers, json=data)
