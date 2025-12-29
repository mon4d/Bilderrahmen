"""Simple SMTP sender using smtplib to send confirmation/error replies."""
import logging
from email.message import EmailMessage
import smtplib

logger = logging.getLogger(__name__)


def send_reply(smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str, to_addr: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)
    logger.info("Sent reply to %s", to_addr)
