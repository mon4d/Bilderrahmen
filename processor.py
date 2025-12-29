"""Message processing: extract attachments, validate images, and store safely."""
import os
import tempfile
import logging
from email import message_from_bytes
from typing import Optional

import magic
from PIL import Image

logger = logging.getLogger(__name__)


def _is_image_mime(mime: str) -> bool:
    return mime.startswith("image/")


def save_attachment_bytes(data: bytes, tmp_dir: str, filename: str) -> str:
    os.makedirs(tmp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="attach-", suffix="-" + filename, dir=tmp_dir)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return path


def validate_and_sanitize_image(path: str) -> bool:
    # Content sniff
    mime = magic.from_file(path, mime=True)
    if not _is_image_mime(mime):
        logger.warning("Attachment %s is not image mime: %s", path, mime)
        return False

    try:
        with Image.open(path) as img:
            img.verify()  # verify integrity
    except Exception as exc:
        logger.exception("Image verification failed: %s", exc)
        return False
    return True


def process_message_bytes(msg_bytes: bytes, tmp_dir: str, data_dir: str, max_bytes: int) -> dict:
    msg = message_from_bytes(msg_bytes)
    saved_paths = []
    os.makedirs(data_dir, exist_ok=True)

    for part in msg.walk():
        content_disposition = part.get("Content-Disposition", "")
        if not content_disposition:
            continue
        if part.get_content_maintype() == 'multipart':
            continue
        filename = part.get_filename() or "attachment"
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > max_bytes:
            return {"ok": False, "reason": "attachment_too_large", "filename": filename}

        tmp_path = save_attachment_bytes(payload, tmp_dir, filename)
        ok = validate_and_sanitize_image(tmp_path)
        if not ok:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            continue

        final_name = os.path.basename(tmp_path)
        final_path = os.path.join(data_dir, final_name)
        os.replace(tmp_path, final_path)
        saved_paths.append(final_path)

    if not saved_paths:
        return {"ok": False, "reason": "no_valid_image"}
    return {"ok": True, "paths": saved_paths}
