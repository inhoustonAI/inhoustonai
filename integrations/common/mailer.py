import os, smtplib, pathlib
from typing import Any, Dict, List, Optional
from email.message import EmailMessage
from jinja2 import Environment, FileSystemLoader, select_autoescape

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", "info@inhoustontexas.us")  # header From (con nombre si quieres)

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parents[2] / "templates"
env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html","xml"]))

def render_email(template_name: str, context: Dict[str, Any]) -> str:
    return env.get_template(template_name).render(**context)

def send_email_html(subject: str, html: str, to: List[str], text_fallback: Optional[str] = None) -> Dict[str, Any]:
    # Validaciones mínimas
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_FROM):
        return {"ok": False, "reason": "SMTP env missing"}

    if not to:
        return {"ok": False, "reason": "No recipients"}

    # Construcción del mensaje
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM              # Header visible (puede tener Display Name)
    msg["To"] = ", ".join(to)
    if text_fallback:
        msg.set_content(text_fallback)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content("Este email requiere un cliente compatible con HTML.")
        msg.add_alternative(html, subtype="html")

    # Envelope sender: forzamos que sea SMTP_USER (Zoho lo exige)
    envelope_from = SMTP_USER

    # Envío
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.sendmail(envelope_from, to, msg.as_string())

    return {"ok": True, "sent_to": to, "envelope_from": envelope_from}
