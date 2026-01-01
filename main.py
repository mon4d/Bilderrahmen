"""Orchestrator: poll IMAP, process attachments, and send replies."""
import logging
import time
import email
import config
from storage import UIDStore
from imap_client import IMAPClientWrapper
from processor import process_message_bytes
from smtp_sender import send_reply
import os
from PIL import Image, ImageOps
import tempfile
import threading
import glob
import traceback


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
        # If set to portrait mode rotate the image 90 degrees before applying display size
        orientedImage = ImageOps.exif_transpose(image)
        try:
            if config.read_setting("ORIENTATION", "landscape") == "portrait":
                orientedImage = orientedImage.rotate(90, expand=True)
        except Exception:
            pass
        resizedimage = _resize_and_crop(orientedImage, inky.resolution)
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
                previewimage = resizedimage
                if config.read_setting("ORIENTATION", "landscape") == "portrait":
                    previewimage = previewimage.rotate(-90, expand=True)
                previewimage.save(preview_path, format="PNG")
        except Exception:
            logging.exception("Failed to write preview image for %s", image_path)
        # Track the currently displayed image and its source path so other
        # parts of the program can rotate/reapply it when orientation changes.
        try:
            # store originals (not the resized image) for better-quality rotations
            global current_image, current_image_path
            current_image = image.copy()
            current_image_path = image_path
        except Exception:
            logging.debug("Failed to set current_image globals")
        return preview_path
    except Exception:
        logging.exception("Failed to display image %s", image_path)
        return None


current_image: Image.Image | None = None
current_image_path: str | None = None


def _find_latest_image(data_dir: str) -> str | None:
    patterns = ["*.jpg", "*.jpeg", "*.png", "*.bmp", "*.gif"]
    files = []
    for pat in patterns:
        files.extend(glob.glob(os.path.join(data_dir, pat)))
    if not files:
        return None
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[0]


def toggle_orientation_and_apply(inky):
    """Toggle orientation setting, persist it, and reapply the current image rotated.

    This writes the new `ORIENTATION` value into the config file and then
    attempts to rotate the currently displayed image; if none is known it
    loads the latest image from the data directory and displays that.
    """
    try:
        old = config.read_setting("ORIENTATION", "landscape")
        new = "portrait" if (old or "landscape") == "landscape" else "landscape"
        config.write_setting("ORIENTATION", new)
        logging.info("Orientation toggled: %s -> %s", old, new)

        # Attempt to rotate the currently loaded image
        global current_image, current_image_path
        if current_image is not None:
            try:
                show_image_on_display(inky, current_image_path)
                logging.info("Reapplied current_image after orientation toggle")
                return
            except Exception:
                logging.exception("Failed to rotate/reapply current_image")

        # If we don't have a current image, try to display the most recent one
        latest = _find_latest_image(config.read_setting("DATA_DIR", "/mnt/usb/data"))
        if latest:
            logging.info("No current image available; showing latest: %s", latest)
            show_image_on_display(
                inky,
                latest,
                tmp_dir=config.read_setting("TMP_DIR", "/mnt/usb/system/tmp")
            )
            logging.info("Applied latest image after orientation toggle")
            return
        else:
            logging.warning("No image available to show after orientation toggle")
    except Exception:
        logging.exception("toggle_orientation_and_apply failed: %s", traceback.format_exc())


def _monitor_buttons_thread(inky):
    try:
        import gpiod
        import gpiodevice
        from gpiod.line import Bias, Direction, Edge

        SW_A = 5
        SW_B = 6
        SW_C = 16
        SW_D = 24
        BUTTONS = [SW_A, SW_B, SW_C, SW_D]
        LABELS = ["A", "B", "C", "D"]

        INPUT = gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP, edge_detection=Edge.FALLING)

        chip = gpiodevice.find_chip_by_platform()
        OFFSETS = [chip.line_offset_from_id(id) for id in BUTTONS]
        line_config = dict.fromkeys(OFFSETS, INPUT)
        request = chip.request_lines(consumer="bilderrahmen-buttons", config=line_config)

        def handle_button(event):
            try:
                index = OFFSETS.index(event.line_offset)
                label = LABELS[index]
                logging.info("Button press detected: %s", label)
                if label == "A":
                    toggle_orientation_and_apply(inky)
            except Exception:
                logging.exception("Error handling button event")

        while True:
            for event in request.read_edge_events():
                handle_button(event) # Does this call multiple times if button held? If yes debounce needed?
            time.sleep(0.01)
    except Exception:
        logging.exception("Button monitor thread could not start (gpiod/gpiodevice may be unavailable)")


def setup_logging(level: str):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")


def main():
    config.load_config()
    setup_logging(config.read_setting("LOG_LEVEL", "INFO"))

    data_dir = config.read_setting("DATA_DIR", "/mnt/usb/data")
    tmp_dir = config.read_setting("TMP_DIR", "/mnt/usb/system/tmp")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    store = UIDStore(os.path.join(data_dir, "uid_state.json"))
    last_uid = store.get_last_uid() or 0

    # Initialize the display early so we can update the Inky without having
    # to initialize the IMAP connection first or spawn a separate process.
    inky = init_display(ask_user=False)
    # Start the button-monitor thread (best-effort: will log if gpiod unavailable)
    try:
        t = threading.Thread(target=_monitor_buttons_thread, args=(inky,), daemon=True)
        t.start()
        logging.info("Started button monitor thread")
    except Exception:
        logging.exception("Failed to start button monitor thread")

    imap = IMAPClientWrapper(
        config.read_setting("IMAP_HOST", ""),
        config.read_setting_int("IMAP_PORT", 993),
        config.read_setting("IMAP_USER", ""),
        config.read_setting("IMAP_PASS", ""),
        config.read_setting("MAILBOX", "INBOX"),
        config.read_setting("TRASH", "Trash")
    )
    if not imap.connect():
        logging.exception("Failed to connect to IMAP server; aborting") # We might want to show an error on the display here
        return

    try:
        while True:
            time.sleep(config.read_setting_int("POLL_INTERVAL", 10))
            try:
                uids = imap.get_all_messages_uids()
                # Might make sense to use a push notification mechanism instead of polling
                logging.info("Found %d unprocessed messages", len(uids))
            except Exception as exc:
                logging.exception("IMAP search failed: %s", exc)
                continue

            for uid in sorted(uids):
                if uid <= last_uid:
                    continue
                try:
                    raw = imap.fetch_message_bytes(uid)
                    logging.info("Fetched UID %s (%d bytes)", uid, len(raw) if raw is not None else 0)

                    res = process_message_bytes(
                        raw,
                        config.read_setting("TMP_DIR", "/mnt/usb/system/tmp"),
                        config.read_setting("DATA_DIR", "/mnt/usb/data"),
                        config.read_setting_int("ATTACHMENT_MAX_BYTES", 5242880)
                    )
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
                                preview_path = show_image_on_display(
                                    inky,
                                    image_path,
                                    tmp_dir=config.read_setting("TMP_DIR", "/mnt/usb/system/tmp")
                                )
                                logging.info("Displayed image for UID %s: %s", uid, image_path)
                        except Exception:
                            logging.exception("Failed to launch show_image for UID %s", uid)

                        # Send success reply and include the preview if available
                        try:
                            smtp_host = config.read_setting("SMTP_HOST", "")
                            smtp_port = config.read_setting_int("SMTP_PORT", 587)
                            smtp_user = config.read_setting("SMTP_USER", "")
                            smtp_pass = config.read_setting("SMTP_PASS", "")
                            if preview_path:
                                send_reply(smtp_host, smtp_port, smtp_user, smtp_pass, from_addr, "Image received", "Your image was received and stored.", attachments=[preview_path])
                            else:
                                send_reply(smtp_host, smtp_port, smtp_user, smtp_pass, from_addr, "Image received", "Your image was received and stored.")
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
                        send_reply(
                            config.read_setting("SMTP_HOST", ""),
                            config.read_setting_int("SMTP_PORT", 587),
                            config.read_setting("SMTP_USER", ""),
                            config.read_setting("SMTP_PASS", ""),
                            from_addr,
                            "Image processing failed",
                            f"Reason: {res.get('reason')}"
                        )
                        logging.info("Sent failure reply for UID %s to %s (reason=%s)", uid, from_addr, res.get('reason'))

                    # delete message after processing
                    try:
                        imap.delete_message(uid)
                        logging.info("Deleted UID %s from mailbox", uid)
                    except Exception:
                        logging.exception("Failed to delete UID %s", uid)

                    try:
                        imap.empty_trash()
                        logging.info("Emptied trash mailbox '%s'", config.read_setting("TRASH", "Trash"))
                    except Exception:
                        logging.warning("Failed to empty trash after deleting UID %s", uid)

                    last_uid = max(last_uid, uid)
                    store.set_last_uid(last_uid)
                except Exception:
                    logging.exception("Failed to process UID %s", uid)

    finally:
        imap.logout()


if __name__ == "__main__":
    main()
