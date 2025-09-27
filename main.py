import telebot
from telebot import types
import json
import psycopg
import requests
import io
import os
from pydub import AudioSegment
import uuid
from datetime import datetime
import schedule
import time
import threading

# ================== CONFIG ==================
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

BOT_TOKEN = config["TELEGRAM_TOKEN"]
ELEVEN_API_KEY = config["ELEVEN_API_KEY"]
NOWPAYMENTS_API_KEY = config["NOWPAYMENTS_API_KEY"]

# Use existing database config for VPS deployment
DB_CONF = {
    "host": config.get("DB_HOST", "127.0.0.1"),
    "dbname": config.get("DB_NAME", "vlonetonydb"),
    "user": config.get("DB_USER", "postgres"),
    "password": config.get("DB_PASS", "vlonetonydb")
}
ADMINS = config["ADMINS"]

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ================== PAYMENT PACKAGES ==================
PAYMENT_PACKAGES = {
    "package_2000": {"chars": 2000, "price_eur": 15, "badge": ""},
    "package_6000": {"chars": 6000, "price_eur": 25, "badge": "🔥 Più venduto"},
    "package_9000": {"chars": 9000, "price_eur": 35, "badge": "💎 Miglior rapporto qualità/prezzo"},
    "package_20000": {"chars": 20000, "price_eur": 55, "badge": "🆕 Offerta speciale"}
}

# ================== DATABASE ==================
def db_conn():
    return psycopg.connect(
        host=DB_CONF["host"],
        dbname=DB_CONF["dbname"],
        user=DB_CONF["user"],
        password=DB_CONF["password"],
        autocommit=True
    )

def get_user_data(user_id: int):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, quota, usage FROM utenti WHERE id = %s;", (user_id,))
        row = cur.fetchone()
        if row:
            return {"id": row[0], "quota": int(row[1] or 0), "usati": int(row[2] or 0)}
        cur.execute(
            "INSERT INTO utenti (id, usage, quota) VALUES (%s, %s, %s) RETURNING id, quota, usage;",
            (user_id, 0, 0)
        )
        new_row = cur.fetchone()
        if new_row:
            return {"id": new_row[0], "quota": int(new_row[1] or 0), "usati": int(new_row[2] or 0)}
        return {"id": user_id, "quota": 0, "usati": 0}

def get_existing_user(user_id: int):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, quota, usage FROM utenti WHERE id = %s;", (user_id,))
        row = cur.fetchone()
        if row:
            return {"id": row[0], "quota": int(row[1] or 0), "usati": int(row[2] or 0)}
        return None

def update_usage(user_id: int, chars: int):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE utenti SET usage = COALESCE(usage,0) + %s WHERE id = %s;", (chars, user_id))

def set_quota(user_id: int, quota: int):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE utenti SET quota = %s, usage = 0 WHERE id = %s;",
            (quota, user_id)
        )

def add_quota(user_id: int, chars: int):
    """Add characters to user's quota without resetting usage"""
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE utenti SET quota = COALESCE(quota,0) + %s WHERE id = %s;",
            (chars, user_id)
        )

# ================== PAYMENT DATABASE FUNCTIONS ==================
def create_payment(user_id: int, package_id: str, amount_eur: float, amount_ltc: float):
    """Create a new payment record"""
    payment_id = str(uuid.uuid4())
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO payments (id, user_id, package_id, amount_eur, amount_ltc, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (payment_id, user_id, package_id, amount_eur, amount_ltc, "pending", datetime.now()))
        return payment_id

def get_payment(payment_id: str):
    """Get payment details"""
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, user_id, package_id, amount_eur, amount_ltc, status, nowpayments_id,
                   nowpayments_address, created_at, completed_at
            FROM payments WHERE id = %s;
        """, (payment_id,))
        row = cur.fetchone()
        if row:
            return {
                "id": row[0], "user_id": row[1], "package_id": row[2],
                "amount_eur": float(row[3]), "amount_ltc": float(row[4]),
                "status": row[5], "nowpayments_id": row[6],
                "nowpayments_address": row[7], "created_at": row[8], "completed_at": row[9]
            }
        return None

def update_payment_nowpayments(payment_id: str, nowpayments_id: str, address: str):
    """Update payment with NOWPayments details"""
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE payments SET nowpayments_id = %s, nowpayments_address = %s
            WHERE id = %s;
        """, (nowpayments_id, address, payment_id))

def complete_payment(payment_id: str):
    """Mark payment as completed and add characters to user"""
    with db_conn() as conn, conn.cursor() as cur:
        # Get payment details
        payment = get_payment(payment_id)
        if not payment or payment["status"] != "pending":
            return False

        # Get package details
        package = PAYMENT_PACKAGES.get(payment["package_id"])
        if not package:
            return False

        # Mark payment as completed
        cur.execute("""
            UPDATE payments SET status = %s, completed_at = %s WHERE id = %s;
        """, ("completed", datetime.now(), payment_id))

        # Add characters to user
        add_quota(payment["user_id"], package["chars"])
        return True

def get_pending_payments():
    """Get all pending payments for monitoring"""
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, user_id, nowpayments_id FROM payments
            WHERE status = 'pending' AND nowpayments_id IS NOT NULL;
        """)
        return cur.fetchall()

# ================== NOWPAYMENTS API FUNCTIONS ==================
def get_ltc_rate_from_eur():
    """Get LTC exchange rate from EUR using NOWPayments API"""
    try:
        # Use the correct estimate endpoint
        response = requests.get(
            f"https://api.nowpayments.io/v1/estimate?amount=1&currency_from=eur&currency_to=ltc",
            headers={"x-api-key": NOWPAYMENTS_API_KEY},
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            return float(data.get("estimated_amount", 0))
        else:
            print(f"API Error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Error getting LTC rate: {e}")
        return None

def create_nowpayments_payment(payment_id: str, amount_eur: float):
    """Create payment through NOWPayments API"""
    try:
        # Get LTC rate
        ltc_rate = get_ltc_rate_from_eur()
        if not ltc_rate:
            print("Failed to get LTC rate")
            return None

        ltc_amount = ltc_rate * amount_eur

        payload = {
            "price_amount": amount_eur,
            "price_currency": "eur",
            "pay_currency": "ltc",
            "order_id": payment_id,
            "order_description": f"Ewhoring Audio Bot - {amount_eur} EUR"
        }

        print(f"Creating payment: {payload}")  # Debug log

        response = requests.post("https://api.nowpayments.io/v1/payment",
                               json=payload,
                               headers={
                                   "x-api-key": NOWPAYMENTS_API_KEY,
                                   "Content-Type": "application/json"
                               }, timeout=30)

        print(f"NOWPayments response: {response.status_code} - {response.text}")  # Debug log

        if response.status_code == 201:
            data = response.json()
            return {
                "payment_id": data.get("payment_id"),
                "pay_address": data.get("pay_address"),
                "pay_amount": data.get("pay_amount"),
                "payment_status": data.get("payment_status")
            }
        else:
            print(f"NOWPayments error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Error creating NOWPayments payment: {e}")
        return None

def check_payment_status(nowpayments_id: str):
    """Check payment status with NOWPayments"""
    try:
        print(f"🔍 Checking payment status for ID: {nowpayments_id}")
        response = requests.get(f"https://api.nowpayments.io/v1/payment/{nowpayments_id}",
                              headers={"x-api-key": NOWPAYMENTS_API_KEY}, timeout=10)

        print(f"📡 API Response Status: {response.status_code}")
        print(f"📊 Full API Response: {response.text}")

        if response.status_code == 200:
            data = response.json()
            payment_status = data.get("payment_status")
            print(f"💰 Extracted Payment Status: '{payment_status}'")

            # Additional debug info
            payment_amount = data.get("payment_amount")
            actually_paid = data.get("actually_paid")
            print(f"💵 Payment amount expected: {payment_amount}")
            print(f"💰 Actually paid: {actually_paid}")

            return payment_status
        else:
            print(f"❌ API Error: {response.status_code}")
            return "error"
    except Exception as e:
        print(f"🚨 Exception checking payment status: {e}")
        return "error"

def get_payment_info(nowpayments_id: str):
    """Get detailed payment info from NOWPayments"""
    try:
        response = requests.get(f"https://api.nowpayments.io/v1/payment/{nowpayments_id}",
                              headers={"x-api-key": NOWPAYMENTS_API_KEY}, timeout=10)

        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        print(f"Error getting payment info: {e}")
        return None

# ================== BOT BUTTONS ==================
def main_buttons(user_data=None):
    kb = types.InlineKeyboardMarkup()

    # Add recharge button if user exists
    if user_data:
        kb.row(types.InlineKeyboardButton("💰 Ricarica Caratteri", callback_data="recharge_menu"))
        kb.row(types.InlineKeyboardButton("📊 Le Mie Statistiche", callback_data="user_stats"))

    kb.row(
        types.InlineKeyboardButton("📝 Guida al bot", url="https://telegra.ph/GUIDA-ALLUSO-DI-EWHORINGAUDIOBOT-09-11"),
        types.InlineKeyboardButton("📨 Contattami", url="https://t.me/stabbato")
    )

    # Add voice preview link
    kb.row(types.InlineKeyboardButton("🎙 Ascolta tutte le voci", url="https://t.me/+nlTWw8WmkPQ2NThh"))

    return kb

def payment_packages_buttons():
    """Create inline keyboard for payment packages"""
    kb = types.InlineKeyboardMarkup()

    for package_id, package in PAYMENT_PACKAGES.items():
        chars = package["chars"]
        price = package["price_eur"]
        badge = package["badge"]

        button_text = f"💎 {chars:,} caratteri — {price}€"
        if badge:
            button_text += f" {badge}"

        kb.row(types.InlineKeyboardButton(button_text, callback_data=f"buy_package|{package_id}"))

    kb.row(types.InlineKeyboardButton("◀️ Torna al Menu", callback_data="back_to_main"))
    return kb

def payment_confirmation_buttons(payment_id: str):
    """Create buttons for payment confirmation"""
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("✅ Ho Effettuato il Pagamento", callback_data=f"check_payment|{payment_id}"))
    kb.row(types.InlineKeyboardButton("❌ Annulla Pagamento", callback_data=f"cancel_payment|{payment_id}"))
    kb.row(types.InlineKeyboardButton("◀️ Torna ai Pacchetti", callback_data="recharge_menu"))
    return kb

# ================== ELEVENLABS VOICES ==================
VOICES = {
    "🎙 Francesca": {"id": "BUMv41bukuQg8aLm77pp", "stability": "normal"},
    "🎙 Jessica": {"id": "Kq9pDHHIMmJsG9PEqOtv", "stability": "normal"},
    "🎙 Cristina": {"id": "8ftlfIEYnEkYY6iLanUO", "stability": "normal"},
    "🎙 Sara": {"id": "IOyj8WtBHdke2FjQgGAr", "stability": "normal"},
    "🎙 Bella": {"id": "xctasy8XvGp2cVO9HL9k", "stability": "normal"},
    "🎙 Lily": {"id": "wtpyr4iAv11inNKbNVQg", "stability": "normal"},
    "🎙 Elli": {"id": "rZR7NRayxfYHWmxVR2R5", "stability": "normal"},
    "🎙 Simona": {"id": "qFkfTOxH5FWOo39H0lIH", "stability": "normal"},
    "🎙 Irene": {"id": "loBVMhavpA1MvAK4c6UY", "stability": "normal"}
}

user_texts = {}

# ================== GENERAZIONE AUDIO ==================
def generate_audio(voice_name: str, text: str) -> bytes:
    v = VOICES[voice_name]
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{v['id']}"
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "model_id": "eleven_v3",
        "stability": v.get("stability", "normal")
    }
    try:
        with requests.post(url, headers=headers, json=payload, stream=True, timeout=(10, 300)) as r:
            r.raise_for_status()
            audio_bytes = b"".join(r.iter_content(chunk_size=8192))
            return audio_bytes
    except requests.exceptions.Timeout:
        raise RuntimeError("Timeout nella generazione dell'audio (troppo lento)")
    except Exception as e:
        raise RuntimeError(f"Errore: {e}")

def mp3_to_ogg(mp3_bytes: bytes) -> io.BytesIO:
    audio = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
    ogg_io = io.BytesIO()
    audio.export(ogg_io, format="ogg", codec="libopus")
    ogg_io.name = "voice.ogg"
    ogg_io.seek(0)
    return ogg_io

# ================== BOT HANDLERS PER TESTO E VOCI ==================
@bot.message_handler(func=lambda m: not m.text.startswith("/"))
def on_text(message):
    u = get_user_data(message.from_user.id)
    text = message.text.strip()
    chars = len(text)
    remaining = u["quota"] - u["usati"]
    if remaining <= 0 or chars > remaining:
        bot.send_message(message.chat.id, "⚠️ <b>Caratteri insufficienti</b>")
        return

    user_texts[message.from_user.id] = text

    kb = types.InlineKeyboardMarkup()
    voices = list(VOICES.keys())
    for i in range(0, len(voices), 2):
        if i + 1 < len(voices):
            kb.row(
                types.InlineKeyboardButton(voices[i], callback_data=f"voice|{voices[i]}|{message.from_user.id}|{chars}"),
                types.InlineKeyboardButton(voices[i+1], callback_data=f"voice|{voices[i+1]}|{message.from_user.id}|{chars}")
            )
        else:
            kb.row(
                types.InlineKeyboardButton(voices[i], callback_data=f"voice|{voices[i]}|{message.from_user.id}|{chars}")
            )

    bot.send_message(
        message.chat.id,
        f"Questo audio ti costerà <b>{chars}</b> caratteri.\nScegli una voce:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("voice|"))
def on_voice_choice(call):
    try:
        _, voice_name, user_id_str, chars_str = call.data.split("|")
        user_id = int(user_id_str)
        chars = int(chars_str)
        testo = user_texts.get(user_id)
        if not testo:
            bot.answer_callback_query(call.id, "❌ Testo non trovato")
            return
    except Exception:
        bot.answer_callback_query(call.id, "❌ Parametro non valido")
        return

    u = get_user_data(call.from_user.id)
    remaining = u["quota"] - u["usati"]
    if chars > remaining:
        bot.answer_callback_query(call.id, "⚠️ Caratteri insufficienti")
        return

    loading_msg = bot.send_message(call.message.chat.id, "🔄 Caricamento...")

    try:
        audio_bytes = generate_audio(voice_name, testo)
        voice_file = mp3_to_ogg(audio_bytes)
    except Exception as e:
        bot.delete_message(call.message.chat.id, loading_msg.message_id)
        bot.send_message(call.message.chat.id, f"❌ Errore durante la generazione dell'audio: <code>{e}</code>")
        return

    bot.delete_message(call.message.chat.id, loading_msg.message_id)
    bot.delete_message(call.message.chat.id, call.message.message_id)

    bot.send_voice(call.message.chat.id, voice_file)

    update_usage(call.from_user.id, chars)
    u2 = get_user_data(call.from_user.id)
    bot.send_message(
        call.message.chat.id,
        f"✍️ <b>Testo</b>: <i>{testo}</i>\n"
        f"🔤 <b>Costo</b>: <code>{chars}</code> caratteri\n"
        f"🎙 <b>Voce</b>: {voice_name}\n\n"
        f"✨ Ti rimangono <b>{u2['quota'] - u2['usati']}/{u2['quota']}</b> caratteri"
    )

# ================== PAYMENT CALLBACK HANDLERS ==================
@bot.callback_query_handler(func=lambda c: c.data == "recharge_menu")
def on_recharge_menu(call):
    """Show payment packages menu"""
    bot.answer_callback_query(call.id)

    message_text = """🗒 <b>LISTA PREZZI — @EWHORINGAUDIOBOT</b>

———————————————————————————————————————————————

💎 <b>Scegli il tuo pacchetto di caratteri:</b>

▫️ <b>2,000 caratteri</b> — 15€

▫️ <b>6,000 caratteri</b> — 25€
🔥 <i>Più venduto</i>

▫️ <b>9,000 caratteri</b> — 35€
💎 <i>Miglior rapporto qualità/prezzo</i>

🆕 <b>OFFERTA SPECIALE:</b>
🔥 <b>20,000 caratteri</b> a 55€ <s>(invece di 70€)</s>

———————————————————————————————————————————————

💰 <b>Pagamento sicuro in Litecoin (LTC)</b>
⚡️ <b>Accredito automatico istantaneo</b>
🎙 <b>Senti tutte le voci:</b> https://t.me/+nlTWw8WmkPQ2NThh

👉 <b>Problemi? Contattami:</b> @stabbato"""

    kb = payment_packages_buttons()

    try:
        bot.edit_message_text(message_text, call.message.chat.id, call.message.message_id,
                            reply_markup=kb, parse_mode="HTML")
    except:
        bot.send_message(call.message.chat.id, message_text, reply_markup=kb, parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_package|"))
def on_buy_package(call):
    """Handle package purchase"""
    bot.answer_callback_query(call.id)

    try:
        _, package_id = call.data.split("|")
        package = PAYMENT_PACKAGES.get(package_id)
        if not package:
            bot.send_message(call.message.chat.id, "❌ Pacchetto non valido")
            return

        user_id = call.from_user.id

        # Create payment record
        payment_id = create_payment(user_id, package_id, package["price_eur"], 0)

        # Create NOWPayments payment
        loading_msg = bot.send_message(call.message.chat.id, "🔄 Creazione pagamento in corso...")

        nowpayment = create_nowpayments_payment(payment_id, package["price_eur"])

        bot.delete_message(call.message.chat.id, loading_msg.message_id)

        if not nowpayment:
            bot.send_message(call.message.chat.id, "❌ Errore nella creazione del pagamento. Riprova più tardi.")
            return

        # Update payment with NOWPayments details
        update_payment_nowpayments(payment_id, nowpayment["payment_id"], nowpayment["pay_address"])

        # Send payment instructions
        payment_text = f"""💰 <b>PAGAMENTO CREATO</b>

📦 <b>Pacchetto:</b> {package['chars']:,} caratteri
💶 <b>Prezzo:</b> {package['price_eur']}€
🪙 <b>Importo LTC:</b> <code>{nowpayment['pay_amount']}</code>

💳 <b>Indirizzo di pagamento:</b>
<code>{nowpayment['pay_address']}</code>

⚠️ <b>IMPORTANTE:</b>
• Invia ESATTAMENTE <code>{nowpayment['pay_amount']}</code> LTC
• All'indirizzo sopra indicato
• I caratteri saranno accreditati automaticamente dopo la conferma

⏰ <b>Hai 1 ora di tempo per completare il pagamento</b>

🔄 <b>Stato:</b> In attesa di pagamento..."""

        kb = payment_confirmation_buttons(payment_id)

        try:
            bot.edit_message_text(payment_text, call.message.chat.id, call.message.message_id,
                                reply_markup=kb, parse_mode="HTML")
        except:
            bot.send_message(call.message.chat.id, payment_text, reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        print(f"Error in buy_package: {e}")
        bot.send_message(call.message.chat.id, "❌ Errore nell'elaborazione del pagamento. Riprova.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("check_payment|"))
def on_check_payment(call):
    """Check payment status"""
    bot.answer_callback_query(call.id, "🔄 Controllo pagamento...")

    try:
        _, payment_id = call.data.split("|")
        payment = get_payment(payment_id)

        if not payment:
            bot.send_message(call.message.chat.id, "❌ Pagamento non trovato")
            return

        if payment["status"] == "completed":
            bot.send_message(call.message.chat.id, "✅ <b>Pagamento già completato!</b>")
            return

        if not payment["nowpayments_id"]:
            bot.send_message(call.message.chat.id, "❌ Errore nel sistema di pagamento")
            return

        # Check with NOWPayments
        status = check_payment_status(payment["nowpayments_id"])

        print(f"Payment status for {payment_id}: {status}")  # Debug log

        if status in ["finished", "confirmed"]:
            # Complete payment
            if complete_payment(payment_id):
                package = PAYMENT_PACKAGES[payment["package_id"]]

                success_text = f"""✅ <b>PAGAMENTO COMPLETATO!</b>

🎉 <b>I tuoi {package['chars']:,} caratteri sono stati accreditati!</b>

📊 <b>Dettagli transazione:</b>
• ID Pagamento: <code>{payment_id[:8]}...</code>
• Importo: {payment['amount_eur']}€ ({payment['amount_ltc']:.6f} LTC)
• Caratteri aggiunti: {package['chars']:,}

🎙 <b>Ora puoi generare audio!</b> Scrivi un messaggio per iniziare.

Grazie per aver scelto @EwhoringAudioBot! 🚀"""

                bot.edit_message_text(success_text, call.message.chat.id, call.message.message_id,
                                    parse_mode="HTML")

                # Notify user with /start to see updated balance
                u = get_user_data(payment["user_id"])
                kb = main_buttons(u)
                bot.send_message(call.message.chat.id,
                               f"💫 <b>Saldo aggiornato:</b> {u['quota'] - u['usati']:,}/{u['quota']:,} caratteri disponibili",
                               reply_markup=kb)
            else:
                bot.send_message(call.message.chat.id, "❌ Errore nell'accredito. Contatta @stabbato")

        elif status in ["partially_paid", "confirming"]:
            bot.send_message(call.message.chat.id,
                           "⏳ <b>Pagamento ricevuto!</b> In attesa di conferme blockchain...")

        elif status == "waiting":
            # Check if there's actually money received
            payment_info = get_payment_info(payment["nowpayments_id"])
            if payment_info and payment_info.get("actually_paid", 0) > 0:
                bot.send_message(call.message.chat.id,
                               "⏳ <b>Pagamento parziale ricevuto!</b> In attesa del saldo completo...")
            else:
                bot.send_message(call.message.chat.id,
                               f"🔄 <b>Nessun pagamento ricevuto</b>\n\n"
                               f"💡 <b>Importante:</b> Invia ESATTAMENTE <code>{payment['amount_ltc']:.6f}</code> LTC all'indirizzo fornito.\n"
                               f"⏱ <b>Tempo rimasto:</b> Controlla che il pagamento non sia scaduto.")

        elif status == "expired":
            bot.send_message(call.message.chat.id,
                           "⏰ <b>Pagamento scaduto.</b> Crea un nuovo pagamento.")

        elif status in ["failed", "refunded"]:
            bot.send_message(call.message.chat.id,
                           "❌ <b>Pagamento non riuscito.</b> Crea un nuovo pagamento.")

        elif status in ["created", "sending"]:
            bot.send_message(call.message.chat.id,
                           f"🔄 <b>Nessun pagamento ricevuto</b>\n\n"
                           f"💡 <b>Importante:</b> Invia ESATTAMENTE <code>{payment['amount_ltc']:.6f}</code> LTC all'indirizzo fornito.\n"
                           f"⏱ <b>Tempo rimasto:</b> Controlla che il pagamento non sia scaduto.")

        else:
            # Unknown status or no payment detected
            bot.send_message(call.message.chat.id,
                           f"🔄 <b>Stato sconosciuto:</b> {status or 'N/A'}\n\n"
                           f"💡 Assicurati di aver inviato il pagamento all'indirizzo corretto.\n"
                           f"📞 Se il problema persiste, contatta @stabbato")

    except Exception as e:
        print(f"Error checking payment: {e}")
        bot.send_message(call.message.chat.id, "❌ Errore nel controllo del pagamento")

@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_payment|"))
def on_cancel_payment(call):
    """Cancel payment"""
    bot.answer_callback_query(call.id, "❌ Pagamento annullato")

    try:
        bot.edit_message_text("❌ <b>Pagamento annullato</b>\n\nPuoi creare un nuovo pagamento quando vuoi.",
                            call.message.chat.id, call.message.message_id, parse_mode="HTML")
    except:
        bot.send_message(call.message.chat.id, "❌ Pagamento annullato")

@bot.callback_query_handler(func=lambda c: c.data == "user_stats")
def on_user_stats(call):
    """Show user statistics"""
    bot.answer_callback_query(call.id)

    user_id = call.from_user.id
    u = get_user_data(user_id)
    name = call.from_user.first_name or "Utente"

    # Get payment history
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), COALESCE(SUM(amount_eur), 0)
                FROM payments
                WHERE user_id = %s AND status = 'completed';
            """, (user_id,))
            result = cur.fetchone()
            completed_payments = result[0] if result else 0
            total_spent = float(result[1]) if result else 0.0
    except:
        completed_payments = 0
        total_spent = 0.0

    stats_text = f"""📊 <b>LE TUE STATISTICHE</b>

👤 <b>Utente:</b> {name} (<code>{user_id}</code>)

🔤 <b>Caratteri:</b>
• Disponibili: <b>{u['quota'] - u['usati']:,}</b>
• Totali: <b>{u['quota']:,}</b>
• Usati: <b>{u['usati']:,}</b>

💰 <b>Pagamenti:</b>
• Completati: <b>{completed_payments}</b>
• Totale speso: <b>{total_spent:.2f}€</b>

⚡️ <b>Stato account:</b> {'🟢 Attivo' if u['quota'] > u['usati'] else '🔴 Esaurito'}"""

    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("💰 Ricarica Caratteri", callback_data="recharge_menu"))
    kb.row(types.InlineKeyboardButton("◀️ Torna al Menu", callback_data="back_to_main"))

    try:
        bot.edit_message_text(stats_text, call.message.chat.id, call.message.message_id,
                            reply_markup=kb, parse_mode="HTML")
    except:
        bot.send_message(call.message.chat.id, stats_text, reply_markup=kb, parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data == "back_to_main")
def on_back_to_main(call):
    """Return to main menu"""
    bot.answer_callback_query(call.id)

    user_id = call.from_user.id
    name = call.from_user.first_name or "Utente"
    u = get_user_data(user_id)
    rimanenti = u["quota"] - u["usati"]
    kb = main_buttons(u)

    if rimanenti > 0:
        text = (f"👋 Benvenuto in <b>Ewhoring Audio Bot</b> <i>{name}</i> (<code>{user_id}</code>)\n\n"
                f"🔤 Ti rimangono <b>{rimanenti:,}/{u['quota']:,}</b> caratteri.\n"
                f"🎙 Per generare l'audio scrivi il testo in un messaggio.\n\n"
                f"💰 Usa il bottone \"Ricarica Caratteri\" per acquistare più caratteri in Litecoin! Oppure se vuoi pagare con PayPal contattami @stabbato")
    else:
        text = (f"👋 Benvenuto in <b>Ewhoring Audio Bot</b> <i>{name}</i> (<code>{user_id}</code>)\n\n"
                f"⚠️ <b>Non hai caratteri disponibili</b> ({u['usati']:,}/{u['quota']:,}).\n\n"
                f"💰 Usa il bottone \"Ricarica Caratteri\" per acquistare caratteri in Litecoin!")

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                            reply_markup=kb, parse_mode="HTML")
    except:
        bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")

# ================== BOT HANDLERS START ==================
@bot.message_handler(commands=["start"])
def on_start(message):
    user_id = message.from_user.id
    name = message.from_user.first_name or "Utente"

    u = get_user_data(user_id)
    rimanenti = u["quota"] - u["usati"]
    kb = main_buttons(u)

    if rimanenti > 0:
        bot.send_message(
            message.chat.id,
            f"👋 Benvenuto in <b>Ewhoring Audio Bot</b> <i>{name}</i> (<code>{user_id}</code>)\n\n"
            f"🔤 Ti rimangono <b>{rimanenti:,}/{u['quota']:,}</b> caratteri.\n"
            f"🎙 Per generare l'audio scrivi il testo in un messaggio.\n\n"
            f"💰 Usa il bottone \"Ricarica Caratteri\" per acquistare più caratteri in Litecoin!",
            reply_markup=kb
        )
    else:
        bot.send_message(
            message.chat.id,
            f"👋 Benvenuto in <b>Ewhoring Audio Bot</b> <i>{name}</i> (<code>{user_id}</code>)\n\n"
            f"⚠️ <b>Non hai caratteri disponibili</b> ({u['usati']:,}/{u['quota']:,}).\n\n"
            f"💰 Usa il bottone \"Ricarica Caratteri\" per acquistare caratteri in Litecoin!",
            reply_markup=kb
        )

# ================== ADMIN COMMANDS ==================
def is_admin(uid: int):
    return uid in ADMINS

@bot.message_handler(commands=["setquota"])
def cmd_setquota(message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid_str, quota_str = message.text.split()
        uid, quota = int(uid_str), int(quota_str)
        u_target = get_existing_user(uid)
        if not u_target:
            return
        set_quota(uid, quota)
        bot.send_message(message.chat.id, f"✅ Quota impostata per <code>{uid}</code> a <b>{quota}</b> caratteri")
        bot.send_message(uid, f"✅ Ti sono stati caricati <b>{quota}</b> caratteri")
    except:
        bot.send_message(message.chat.id, "Uso corretto: <code>/setquota &lt;id&gt; &lt;quota&gt;</code>")

@bot.message_handler(commands=["getinfo"])
def cmd_getinfo(message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid_str = message.text.split()
        uid = int(uid_str)
        u_target = get_existing_user(uid)
        if not u_target:
            return
        bot.send_message(message.chat.id, f"👤 ID: <code>{u_target['id']}</code>\n📊 Usage: <b>{u_target['usati']}</b>\n🎯 Quota: <b>{u_target['quota']}</b>")
    except:
        bot.send_message(message.chat.id, "Uso corretto: <code>/getinfo &lt;id&gt;</code>")

@bot.message_handler(commands=["tokensADMIN"])
def cmd_tokens(message):
    if not is_admin(message.from_user.id):
        return
    r = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": ELEVEN_API_KEY})
    if r.status_code == 200:
        data = r.json()
        remaining = int(data.get("character_limit", 0)) - int(data.get("character_count", 0))
        bot.send_message(message.chat.id, f"🔑 Caratteri rimanenti: <b>{remaining}</b>")
    else:
        bot.send_message(message.chat.id, "❌ Errore nel recupero dei token")

@bot.message_handler(commands=["users"])
def cmd_users(message):
    if not is_admin(message.from_user.id):
        return
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM utenti;")
            result = cur.fetchone()
            count = result[0] if result else 0
        bot.send_message(message.chat.id, f"👥 Utenti registrati: <b>{count}</b>")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Errore nel conteggio utenti: <code>{e}</code>")

@bot.message_handler(commands=["payments"])
def cmd_payments(message):
    """Admin command to view recent payments"""
    if not is_admin(message.from_user.id):
        return
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as total,
                       COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed,
                       COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending,
                       COALESCE(SUM(CASE WHEN status = 'completed' THEN amount_eur END), 0) as total_eur
                FROM payments
                WHERE created_at > CURRENT_DATE - INTERVAL '7 days';
            """)
            result = cur.fetchone()
            if result:
                total, completed, pending, total_eur = result
                text = f"""💰 <b>REPORT PAGAMENTI (ultimi 7 giorni)</b>

📊 <b>Statistiche:</b>
• Totali: <b>{total}</b>
• Completati: <b>{completed}</b> ✅
• In attesa: <b>{pending}</b> ⏳
• Totale incassato: <b>{float(total_eur):.2f}€</b>

🔄 Usa /pending per vedere i pagamenti in attesa"""
            else:
                text = "📊 Nessun pagamento negli ultimi 7 giorni"

        bot.send_message(message.chat.id, text)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Errore: <code>{e}</code>")

@bot.message_handler(commands=["pending"])
def cmd_pending(message):
    """Admin command to check pending payments"""
    if not is_admin(message.from_user.id):
        return
    try:
        pending_payments = get_pending_payments()
        if not pending_payments:
            bot.send_message(message.chat.id, "✅ Nessun pagamento in attesa")
            return

        text = "⏳ <b>PAGAMENTI IN ATTESA:</b>\n\n"
        for payment_id, user_id, nowpayments_id in pending_payments:
            # Check status
            status = check_payment_status(nowpayments_id) if nowpayments_id else "unknown"
            text += f"• ID: <code>{payment_id[:8]}...</code>\n"
            text += f"  Utente: <code>{user_id}</code>\n"
            text += f"  Stato: <b>{status}</b>\n\n"

        bot.send_message(message.chat.id, text)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Errore: <code>{e}</code>")

# ================== PAYMENT MONITORING ==================
def monitor_payments():
    """Monitor pending payments and complete them automatically"""
    try:
        pending_payments = get_pending_payments()
        for payment_id, user_id, nowpayments_id in pending_payments:
            if nowpayments_id:
                status = check_payment_status(nowpayments_id)
                if status in ["finished", "confirmed"]:
                    if complete_payment(payment_id):
                        # Notify user
                        payment = get_payment(payment_id)
                        if payment:
                            package = PAYMENT_PACKAGES.get(payment["package_id"])
                            if package:
                                try:
                                    bot.send_message(user_id,
                                                   f"🎉 <b>Pagamento completato!</b>\n\n"
                                                   f"✅ {package['chars']:,} caratteri sono stati aggiunti al tuo account!\n"
                                                   f"💰 Importo: {payment['amount_eur']}€\n\n"
                                                   f"🎙 Ora puoi generare audio! Scrivi /start per vedere il saldo aggiornato.")
                                except:
                                    pass  # User may have blocked the bot
                        print(f"✅ Payment {payment_id} completed automatically")

    except Exception as e:
        print(f"Error monitoring payments: {e}")

def run_payment_monitor():
    """Run payment monitoring in background"""
    schedule.every(30).seconds.do(monitor_payments)  # Check every 30 seconds

    while True:
        schedule.run_pending()
        time.sleep(1)

# ================== MAIN ==================
if __name__ == "__main__":
    # Initialize database tables
    try:
        with db_conn() as conn, conn.cursor() as cur:
            # Create payments table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id VARCHAR(36) PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    package_id VARCHAR(50) NOT NULL,
                    amount_eur DECIMAL(10,2) NOT NULL,
                    amount_ltc DECIMAL(18,8) DEFAULT 0,
                    status VARCHAR(20) DEFAULT 'pending',
                    nowpayments_id VARCHAR(100),
                    nowpayments_address VARCHAR(200),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP
                );
            """)
            print("✅ Database tables initialized")
    except Exception as e:
        print(f"❌ Database initialization error: {e}")

    # Start payment monitoring in background thread
    monitor_thread = threading.Thread(target=run_payment_monitor, daemon=True)
    monitor_thread.start()
    print("🔄 Payment monitoring started")

    print("✅ Bot avviato…")
    bot.infinity_polling(skip_pending=True, timeout=60)
