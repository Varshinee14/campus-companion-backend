from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, HTMLResponse
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json
import requests
import uuid

app = FastAPI()

# 🔐 Firebase
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
    tickets = list(db.collection("tickets").stream())

    html = "<h1>🏫 Campus Companion Admin</h1><hr>"

    if len(tickets) == 0:
        html += "<h3>No tickets found.</h3>"
    else:
        for ticket in tickets:
            data = ticket.to_dict()
            html += f"""
            <div style='border:1px solid #ccc;padding:10px;margin-bottom:10px'>
                <b>ID:</b> {ticket.id}<br>
                <b>Bucket:</b> {data.get('bucket')}<br>
                <b>Category:</b> {data.get('category')}<br>
                <b>Room:</b> {data.get('room')}<br>
                <b>Roll:</b> {data.get('roll_number')}<br>
                <b>Description:</b> {data.get('description')}<br>
                <b>Priority:</b> {data.get('priority')}<br>
                <b>Status:</b> {data.get('status')}<br>
            </div>
            """

    return html

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

            # Always reset
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

            # BACK TO MAIN
            if selected == "back_main":
                convo_ref.delete()
                send_main_menu(phone)
                return {"status": "ok"}

            # BACK TO BUCKET
            if selected == "back_bucket":
                send_bucket_buttons(phone)
                return {"status": "ok"}

            # MAIN
            if selected == "raise":
                send_bucket_buttons(phone)

            elif selected == "enquire":
                convo_ref.set({"step": "waiting_ticket_lookup"}, merge=True)
                send_text(phone, "Enter your Ticket ID:")

            # LEVEL 1
            elif selected == "hostel":
                convo_ref.set({"bucket": "Hostel"}, merge=True)
                send_hostel_main(phone)

            elif selected == "academics_facilities":
                convo_ref.set({"bucket": "Academics & Facilities"}, merge=True)
                send_academics_facilities(phone)

            # HOSTEL LEVEL 2
            elif selected == "electrical":
                send_electrical_options(phone)

            elif selected == "utilities":
                send_utilities_options(phone)

            # ACADEMICS LEVEL 2
            elif selected == "infra":
                send_infra_options(phone)

            elif selected == "mess":
                send_mess_options(phone)

            elif selected == "recreation":
                send_recreation_options(phone)

            # FINAL CATEGORY SELECTION
            elif selected in [
                "ac", "geyser", "washing_machine",
                "wifi", "water_dispenser", "cleaning",
                "it_help", "room_booking",
                "gym", "terrace", "yoga",
                "food_quality", "mess_hygiene"
            ]:
                convo_ref.set({"category": selected, "step": "waiting_room"}, merge=True)
                send_text(phone, "Enter Room Number:")

            elif selected in ["high", "medium", "low"]:
                complete_ticket(phone, selected)
                convo_ref.delete()

            elif selected == "stop":
                convo_ref.delete()
                send_text(phone, "Thank you! 😊")

    except Exception as e:
        print("ERROR:", e)

    return {"status": "ok"}

# ================= MENUS =================
def send_main_menu(phone):
    send_buttons(phone, "Choose an option:", [
        ("raise", "Raise Complaint"),
        ("enquire", "Enquire Ticket")
    ])

def send_bucket_buttons(phone):
    send_buttons(phone, "Select Complaint Category:", [
        ("hostel", "Hostel"),
        ("academics_facilities", "Academics & Facilities"),
        ("back_main", "⬅ Back")
    ])

def send_hostel_main(phone):
    send_buttons(phone, "Select Hostel Category:", [
        ("electrical", "Electrical"),
        ("utilities", "Utilities"),
        ("back_bucket", "⬅ Back")
    ])

def send_electrical_options(phone):
    send_buttons(phone, "Select Electrical Issue:", [
        ("ac", "AC"),
        ("geyser", "Geyser"),
        ("washing_machine", "Washing Machine")
    ])

def send_utilities_options(phone):
    send_buttons(phone, "Select Utility Issue:", [
        ("wifi", "WiFi"),
        ("water_dispenser", "Water Dispenser"),
        ("cleaning", "Cleaning")
    ])

def send_academics_facilities(phone):
    send_buttons(phone, "Select Category:", [
        ("infra", "Infra Issues"),
        ("mess", "Mess Issues"),
        ("back_bucket", "⬅ Back")
    ])

def send_infra_options(phone):
    send_buttons(phone, "Select Infra Issue:", [
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

def send_mess_options(phone):
    send_buttons(phone, "Select Mess Issue:", [
        ("food_quality", "Food Quality"),
        ("mess_hygiene", "Hygiene"),
        ("back_bucket", "⬅ Back")
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

# ================= TICKETS =================
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

    send_buttons(
        phone,
        f"✅ Ticket {ticket_id} created!\nRaise another complaint?",
        [
            ("raise", "Raise Another"),
            ("stop", "Stop"),
            ("back_main", "⬅ Menu")
        ]
    )

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
    print("SENDING TO WHATSAPP:", data)

    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json=data)

    print("WHATSAPP STATUS:", response.status_code)
    print("WHATSAPP RESPONSE:", response.text)
