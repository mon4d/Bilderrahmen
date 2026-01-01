"""Simple SMTP sender for confirmation and error emails.

Provides send_reply function for sending emails with optional
file or in-memory attachments.
"""
# Standard library imports
import logging
import mimetypes
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def send_reply(smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str, to_addr: str, subject: str, body: str, attachments: list[str | tuple] | None = None) -> None:
    """Send a reply email with optional file or in-memory attachments.

    Args:
        smtp_host: SMTP server hostname
        smtp_port: SMTP server port
        smtp_user: SMTP username
        smtp_pass: SMTP password
        to_addr: Recipient email address
        subject: Email subject line
        body: Email body text
        attachments: Optional list of file paths (str) or tuples (data, filename, mimetype)
    
    attachments can be:
    - A list of filesystem paths (str): Files will be read and attached
    - A list of tuples (data: bytes, filename: str, mimetype: str): In-memory data will be attached
    - A mix of both
    
    Example: [(image_bytes, "preview.png", "image/png")]
    """

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    if attachments:
        for item in attachments:
            try:
                # Check if it's an in-memory attachment (tuple)
                if isinstance(item, tuple):
                    data, filename, mimetype = item
                    maintype, subtype = mimetype.split("/", 1)
                    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
                # Otherwise it's a file path (str)
                else:
                    path = item
                    ctype, encoding = mimetypes.guess_type(path)
                    if ctype is None:
                        maintype, subtype = "application", "octet-stream"
                    else:
                        maintype, subtype = ctype.split("/", 1)

                    with open(path, "rb") as f:
                        data = f.read()

                    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=os.path.basename(path))
            except Exception:
                logger.exception("Failed to attach %s", item)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)
    logger.info("Sent reply to %s", to_addr)
