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
                <b>Phone:</b> {data.get('phone')}<br>
                <b>Category:</b> {data.get('category')}<br>
                <b>Subcategory:</b> {data.get('subcategory')}<br>
                <b>Room:</b> {data.get('room')}<br>
                <b>Description:</b> {data.get('description')}<br>
                <b>Urgency:</b> {data.get('urgency')}<br>
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
    print("VERIFY PARAMS:", params)

    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge"))

    return PlainTextResponse("Verification failed", status_code=403)


# =========================
# 🔹 RECEIVE MESSAGES
# =========================
@app.post("/webhook")
async def receive(request: Request):
    body = await request.json()
    print("FULL BODY:", body)

    try:
        entry = body.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        messages = value.get("messages")

        if not messages:
            return {"status": "no message"}

        message = messages[0]
        phone = message.get("from")
        msg_type = message.get("type")

        convo_ref = db.collection("conversations").document(phone)
        convo = convo_ref.get().to_dict() or {}

        # =====================
        # TEXT INPUT HANDLING
        # =====================
        if msg_type == "text":
            text = message["text"]["body"]

            if convo.get("step") == "waiting_room":
                convo_ref.set({"room": text, "step": "waiting_description"}, merge=True)
                send_text(phone, "Describe the issue:")
                return {"status": "ok"}

            elif convo.get("step") == "waiting_description":
                convo_ref.set({"description": text, "step": "waiting_urgency"}, merge=True)
                send_urgency_buttons(phone)
                return {"status": "ok"}

            else:
                send_category_buttons(phone)
                return {"status": "ok"}

        # =====================
        # BUTTON HANDLING
        # =====================
        elif msg_type == "interactive":
            selected = message["interactive"]["button_reply"]["id"]

            if selected == "electrical":
                convo_ref.set({"category": "Electrical"}, merge=True)
                send_appliance_buttons(phone)

            elif selected in ["ac", "geyser", "washing"]:
                convo_ref.set({
                    "subcategory": selected,
                    "step": "waiting_room"
                }, merge=True)
                send_text(phone, "Enter Room Number:")

            elif selected in ["low", "medium", "high"]:
                complete_ticket(phone, selected)
                convo_ref.delete()

    except Exception as e:
        print("ERROR:", e)

    return {"status": "ok"}


# =========================
# 🔹 WHATSAPP HELPERS
# =========================

def send_category_buttons(phone):
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Select Category"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "electrical", "title": "Electrical"}},
                    {"type": "reply", "reply": {"id": "hygiene", "title": "Hygiene"}},
                    {"type": "reply", "reply": {"id": "food", "title": "Food"}}
                ]
            }
        }
    }
    send_whatsapp(data)


def send_appliance_buttons(phone):
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Select Appliance"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "ac", "title": "AC"}},
                    {"type": "reply", "reply": {"id": "geyser", "title": "Geyser"}},
                    {"type": "reply", "reply": {"id": "washing", "title": "Washing Machine"}}
                ]
            }
        }
    }
    send_whatsapp(data)

def send_urgency_buttons(phone):
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Select Urgency"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "low", "title": "Low"}},
                    {"type": "reply", "reply": {"id": "medium", "title": "Medium"}},
                    {"type": "reply", "reply": {"id": "high", "title": "High"}}
                ]
            }
        }
    }
    send_whatsapp(data)

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

    print("WHATSAPP RESPONSE:", response.status_code)
    print("WHATSAPP BODY:", response.text)


def save_temp(phone, data):
    db.collection("conversations").document(phone).set(data, merge=True)


def complete_ticket(phone, urgency):
    convo_ref = db.collection("conversations").document(phone)
    convo = convo_ref.get().to_dict()

    ticket_id = str(uuid.uuid4())[:8]

    db.collection("tickets").document(ticket_id).set({
        "phone": phone,
        "category": convo.get("category"),
        "subcategory": convo.get("subcategory"),
        "room": convo.get("room"),
        "description": convo.get("description"),
        "urgency": urgency,
        "status": "Open"
    })

    send_text(phone, f"Ticket {ticket_id} created successfully!")

    convo_ref.delete()
