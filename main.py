"""Orchestrator: poll IMAP, process attachments, and send replies."""
import logging
import time
import email
from config import load_config
from storage import UIDStore
from imap_client import IMAPClientWrapper
from processor import process_message_bytes
from smtp_sender import send_reply
import os
import subprocess
import sys
from PIL import Image
import tempfile


def init_display(ask_user: bool = False):
    """Initialize and return an Inky display instance or None on failure."""
    try:
        from inky.auto import auto

        inky = auto(ask_user=ask_user, verbose=True)
        logging.info("Initialized Inky display: %s", getattr(inky, "name", "<unknown>"))
        return inky
    except Exception:
        logging.exception("Failed to initialize Inky display")
        return None


def _resize_and_crop(image: Image.Image, target_size: tuple) -> Image.Image:
    """Resize `image` to fill `target_size` while preserving aspect ratio,
    then center-crop any overflow so the result exactly matches `target_size`.

    This uses a 'cover' strategy: scale the image so that the smaller
    dimension fits, then crop the excess from the larger dimension.
    """
    target_w, target_h = target_size
    src_w, src_h = image.size
    if src_w == 0 or src_h == 0 or target_w == 0 or target_h == 0:
        return image.resize((target_w, target_h))

    src_ratio = src_w / src_h
    target_ratio = target_w / target_h

    # Determine scale factor to cover the target area
    if src_ratio > target_ratio:
        # source is wider -> scale by height
        scale = target_h / src_h
    else:
        # source is taller (or matching) -> scale by width
        scale = target_w / src_w

    new_w = int(src_w * scale + 0.5)
    new_h = int(src_h * scale + 0.5)

    # Use a high-quality down/upsampling filter
    resized = image.resize((new_w, new_h), Image.LANCZOS)

    # Center-crop to target size
    left = max(0, (new_w - target_w) // 2)
    top = max(0, (new_h - target_h) // 2)
    right = left + target_w
    bottom = top + target_h

    return resized.crop((left, top, right, bottom))


def show_image_on_display(inky, image_path: str, saturation: float = 0.5, tmp_dir: str | None = None):
    """Open `image_path`, resize to the display resolution and show it on `inky`.

    This mirrors the behavior previously in `show_image.py` but runs in-process
    so updates can happen without spawning a new Python process or re-initializing
    the display.
    """
    if inky is None:
        logging.warning("No Inky display available; skipping show_image call for %s", image_path)
        return None
    try:
        image = Image.open(image_path)
        resizedimage = _resize_and_crop(image, inky.resolution)
        try:
            inky.set_image(resizedimage, saturation=saturation)
        except TypeError:
            inky.set_image(resizedimage)
        inky.show()
        logging.info("Displayed image on Inky: %s", image_path)
        # Save a preview of what was shown (PNG) so we can email it back
        preview_path = None
        try:
            if tmp_dir:
                with tempfile.NamedTemporaryFile(prefix="preview-", suffix=".png", dir=tmp_dir, delete=False) as tf:
                    preview_path = tf.name
                resizedimage.save(preview_path, format="PNG")
        except Exception:
            logging.exception("Failed to write preview image for %s", image_path)
        return preview_path
    except Exception:
        logging.exception("Failed to display image %s", image_path)
        return None


def setup_logging(level: str):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")


def main():
    cfg = load_config()
    setup_logging(cfg.log_level)

    os.makedirs(cfg.data_dir, exist_ok=True)
    os.makedirs(cfg.tmp_dir, exist_ok=True)

    store = UIDStore(os.path.join(cfg.data_dir, "uid_state.json"))
    last_uid = store.get_last_uid() or 0

    # Initialize the display early so we can update the Inky without having
    # to initialize the IMAP connection first or spawn a separate process.
    inky = init_display(ask_user=False)

    imap = IMAPClientWrapper(cfg.imap_host, cfg.imap_port, cfg.imap_user, cfg.imap_pass, cfg.mailbox, cfg.trash)
    if not imap.connect():
        logging.exception("Failed to connect to IMAP server; aborting")
        return

    try:
        while True:
            try:
                uids = imap.get_all_messages_uids()
                logging.info("Found %d unprocessed messages", len(uids))
            except Exception as exc:
                logging.exception("IMAP search failed: %s", exc)
                time.sleep(cfg.poll_interval)
                continue

            for uid in sorted(uids):
                if uid <= last_uid:
                    continue
                try:
                    raw = imap.fetch_message_bytes(uid)
                    logging.info("Fetched UID %s (%d bytes)", uid, len(raw) if raw is not None else 0)

                    res = process_message_bytes(raw, cfg.tmp_dir, cfg.data_dir, cfg.attachment_max_bytes)
                    logging.info("Processing result for UID %s: %s", uid, res)

                    from_addr = email.message_from_bytes(raw).get('From')
                    logging.info("Message UID %s from: %s", uid, from_addr)

                    if res.get("ok"):
                        # Launch the show_image script for the first saved image (non-blocking)
                        preview_path = None
                        try:
                            paths = res.get("paths", []) or []
                            if paths:
                                image_path = paths[0]
                                preview_path = show_image_on_display(inky, image_path, tmp_dir=cfg.tmp_dir)
                                logging.info("Displayed image for UID %s: %s", uid, image_path)
                        except Exception:
                            logging.exception("Failed to launch show_image for UID %s", uid)

                        # Send success reply and include the preview if available
                        try:
                            if preview_path:
                                send_reply(cfg.smtp_host, cfg.smtp_port, cfg.smtp_user, cfg.smtp_pass, from_addr, "Image received", "Your image was received and stored.", attachments=[preview_path])
                            else:
                                send_reply(cfg.smtp_host, cfg.smtp_port, cfg.smtp_user, cfg.smtp_pass, from_addr, "Image received", "Your image was received and stored.")
                            logging.info("Sent success reply for UID %s to %s", uid, from_addr)
                        except Exception:
                            logging.exception("Failed to send success reply for UID %s to %s", uid, from_addr)
                        
                        # Cleanup: remove the temporary preview file
                        if preview_path:
                            try:
                                os.remove(preview_path)
                                logging.info("Removed preview file %s after sending reply for UID %s", preview_path, uid)
                            except Exception:
                                logging.exception("Failed to remove preview file %s for UID %s", preview_path, uid)
                    else:
                        send_reply(cfg.smtp_host, cfg.smtp_port, cfg.smtp_user, cfg.smtp_pass, from_addr, "Image processing failed", f"Reason: {res.get('reason')}")
                        logging.info("Sent failure reply for UID %s to %s (reason=%s)", uid, from_addr, res.get('reason'))

                    # delete message after processing
                    try:
                        imap.delete_message(uid)
                        logging.info("Deleted UID %s from mailbox", uid)
                    except Exception:
                        logging.exception("Failed to delete UID %s", uid)

                    try:
                        imap.empty_trash()
                        logging.info("Emptied trash mailbox '%s'", getattr(cfg, 'trash', None))
                    except Exception:
                        logging.warning("Failed to empty trash after deleting UID %s", uid)

                    last_uid = max(last_uid, uid)
                    store.set_last_uid(last_uid)
                except Exception:
                    logging.exception("Failed to process UID %s", uid)

            time.sleep(cfg.poll_interval)
    finally:
        imap.logout()


if __name__ == "__main__":
    main()
