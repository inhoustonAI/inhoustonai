import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.post("/webhooks/eleven/post-call")
def post_call():
    """
    Webhook de ElevenLabs: recibe datos de cada llamada.
    Ahora detecta dinámicamente el número del cliente desde el JSON
    y le envía su link por SMS o WhatsApp automáticamente.
    """
    data = request.get_json(silent=True) or {}
    print("Webhook recibido de ElevenLabs:", data)

    # 🔹 Intentamos extraer el número del cliente desde el JSON (estructura variable)
    caller_number = (
        data.get("from") or
        data.get("caller") or
        data.get("phone") or
        data.get("caller_number") or
        data.get("source") or ""
    )

    # Normalizamos el número para formato E.164 (+1XXXXXXXXXX)
    import re
    match = re.search(r'(\d{10})', caller_number)
    if match:
        caller_number = match.group(1)
    else:
        print("⚠️ No se encontró número válido en JSON:", caller_number)
        return jsonify({"ok": True, "msg": "No se envió SMS (número no detectado)"}), 200

    try:
        # 🔹 Llamamos a tu endpoint de envío (ya configurado)
        resp = requests.post(
            "https://multi-bot-inteligente-v1.onrender.com/actions/send-link",
            json={
                "bot": "whatsapp:+18326213202",  # Bot matriz (Houston School)
                "phone": caller_number,          # Dinámico desde el webhook
                "channel": "sms",                # o 'wa' si quieres WhatsApp
                "link": "https://calendly.com/houston-school/30min"
            },
            timeout=8
        )
        print("Resultado envío SMS:", resp.status_code, resp.text)

    except Exception as e:
        print("❌ Error enviando SMS automático:", e)

    # Siempre respondemos 200 para no afectar a ElevenLabs
    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
