"""Simple SMTP sender for confirmation and error emails.

Provides send_reply function for sending emails with optional
file or in-memory attachments and HTML content.
"""
# Standard library imports
import logging
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)


# User-friendly error message mapping
ERROR_MESSAGES = {
    "attachment_too_large": "The attachment was too large. Please send an image smaller than 20 MB.",
    "no_valid_image": "No valid image was found in your email. Please attach a JPEG, PNG, GIF, or BMP image.",
    "no_attachments": "Your email did not contain any attachments. Please attach an image file.",
    "invalid_mime_type": "The file type is not supported. Please send a JPEG, PNG, GIF, or BMP image.",
    "pil_verification_failed": "The image file appears to be corrupted or invalid. Please try a different image.",
}


def get_user_friendly_error(error_code: str) -> str:
    """Convert technical error codes to user-friendly messages.
    
    Args:
        error_code: Technical error code from processing
        
    Returns:
        User-friendly error message
    """
    return ERROR_MESSAGES.get(error_code, f"An error occurred: {error_code}")


def render_template(template_name: str, **kwargs) -> str:
    """Load and render an HTML email template.
    
    Args:
        template_name: Name of the template file (e.g., 'email_success.html')
        **kwargs: Variables to substitute in the template
        
    Returns:
        Rendered HTML string
    """
    template_dir = Path(__file__).parent / "templates"
    template_path = template_dir / template_name
    
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()
        return template_content.format(**kwargs)
    except FileNotFoundError:
        logger.error("Template not found: %s", template_path)
        raise
    except KeyError as e:
        logger.error("Missing template variable: %s", e)
        raise


def send_reply(smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str, to_addr: str, subject: str, body: str, attachments: list[str | tuple] | None = None, html_body: str | None = None) -> None:
    """Send a reply email with optional file or in-memory attachments and HTML content.

    Args:
        smtp_host: SMTP server hostname
        smtp_port: SMTP server port
        smtp_user: SMTP username
        smtp_pass: SMTP password
        to_addr: Recipient email address
        subject: Email subject line
        body: Email body text (plain text fallback)
        attachments: Optional list of file paths (str) or tuples (data, filename, mimetype)
        html_body: Optional HTML version of the email body
    
    attachments can be:
    - A list of filesystem paths (str): Files will be read and attached
    - A list of tuples (data: bytes, filename: str, mimetype: str): In-memory data will be attached
    - A mix of both
    
    When html_body is provided and attachments contain image tuples, the first image
    will be embedded inline with a CID reference for use in the HTML template.
    
    Example: [(image_bytes, "preview.png", "image/png")]
    """

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    
    # Set plain text content as fallback
    msg.set_content(body)
    
    # Add HTML version if provided
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    # Track if we've embedded an image inline for HTML
    inline_image_added = False
    
    if attachments:
        for item in attachments:
            try:
                # Check if it's an in-memory attachment (tuple)
                if isinstance(item, tuple):
                    data, filename, mimetype = item
                    maintype, subtype = mimetype.split("/", 1)
                    
                    # If HTML is provided and this is the first image, embed it inline
                    if html_body and maintype == "image" and not inline_image_added:
                        msg.add_attachment(data, maintype=maintype, subtype=subtype, 
                                         filename=filename, cid="preview_image")
                        inline_image_added = True
                    else:
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
