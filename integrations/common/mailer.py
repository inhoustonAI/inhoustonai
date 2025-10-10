import os
import smtplib
from typing import Any, Dict, List, Optional
from email.message import EmailMessage
from jinja2 import Environment, FileSystemLoader, select_autoescape
import pathlib

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", "no-reply@example.com")

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parents[2] / "templates"
env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"])
)

def render_email(template_name: str, context: Dict[str, Any]) -> str:
    tpl = env.get_template(template_name)
    return tpl.render(**context)

def send_email_html(subject: str, html: str, to: List[str], text_fallback: Optional[str] = None) -> Dict[str, Any]:
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS or not MAIL_FROM:
        return {"ok": False, "reason": "SMTP env missing"}
    if not to:
        return {"ok": False, "reason": "No recipients"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(to)
    if text_fallback:
        msg.set_content(text_fallback)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content("Este email requiere un cliente compatible con HTML.")
        msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)

    return {"ok": True, "sent_to": to}
