"""Simple SMTP sender using smtplib to send confirmation/error replies."""
import logging
import os
from email.message import EmailMessage
import smtplib

logger = logging.getLogger(__name__)


def send_reply(smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str, to_addr: str, subject: str, body: str, attachments: list[str] | None = None) -> None:
    """Send a reply email. Optionally attach file paths from `attachments`.

    `attachments` is a list of filesystem paths. Files will be read and
    attached with guessed MIME types. If a file cannot be read or its MIME
    type cannot be guessed, it will be attached as 'application/octet-stream'.
    """
    import mimetypes

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    if attachments:
        for path in attachments:
            try:
                ctype, encoding = mimetypes.guess_type(path)
                if ctype is None:
                    maintype, subtype = "application", "octet-stream"
                else:
                    maintype, subtype = ctype.split("/", 1)

                with open(path, "rb") as f:
                    data = f.read()

                msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=os.path.basename(path))
            except Exception:
                logger.exception("Failed to attach file %s", path)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)
    logger.info("Sent reply to %s", to_addr)
