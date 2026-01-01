"""Orchestrator for Bilderrahmen picture frame.

Poll IMAP inbox for new messages, process image attachments,
display on Inky e-paper display, and send email confirmations.
"""
# Standard library imports
import email
import glob
import logging
import os
import tempfile
import threading
import time
import traceback

# Third-party imports
from PIL import Image, ImageOps

# Local imports
import config
from imap_client import IMAPClientWrapper
from processor import process_message_bytes
from smtp_sender import send_reply, render_template, get_user_friendly_error
from storage import UIDStore


def init_display(ask_user: bool = False):
    """Initialize and return an Inky display instance or None on failure."""
    try:
        from inky.auto import auto

        inky = auto(ask_user=ask_user, verbose=True)
        logging.info("Initialized Inky display: %s", getattr(inky, "name", "<unknown>"))
        return inky
    except Exception as exc:
        logging.exception("Failed to initialize Inky display: %s", exc)
        return None


def _resize_and_crop(image: Image.Image, target_size: tuple) -> Image.Image:
    """Resize `image` to fill `target_size` while preserving aspect ratio.
    
    Center-crop any overflow so the result exactly matches `target_size`.
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


def prepare_image_for_display(inky, image_path: str):
    """Prepare an image for display: load, orient, resize, and create preview data.
    
    Returns:
        Tuple of (resized_image, preview_data, original_image, error_message)
        On success: (Image, bytes, Image, None)
        On failure: (None, None, None, error_string)
    """
    if inky is None:
        error_msg = "No Inky display available; cannot prepare image"
        logging.warning("%s for %s", error_msg, image_path)
        return None, None, None, error_msg
    
    try:
        import io
        
        image = Image.open(image_path)
        
        # Apply EXIF orientation with validation
        try:
            oriented_image = ImageOps.exif_transpose(image)
            # Validate that transpose didn't produce invalid dimensions
            if oriented_image.size[0] == 0 or oriented_image.size[1] == 0:
                logging.warning("EXIF transpose produced invalid dimensions, using original image")
                oriented_image = image
        except Exception as exc:
            logging.warning("EXIF transpose failed: %s, using original image", exc)
            oriented_image = image
        
        # If set to portrait mode rotate the image 90 degrees before applying display size
        try:
            if config.read_setting("ORIENTATION", "landscape") == "portrait":
                oriented_image = oriented_image.rotate(90, expand=True)
        except Exception as exc:
            logging.debug("Failed to apply orientation rotation: %s", exc)
        resized_image = _resize_and_crop(oriented_image, inky.resolution)
        
        # Create preview image data (PNG bytes) to email back - no disk write!
        preview_data = None
        try:
            preview_image = resized_image
            if config.read_setting("ORIENTATION", "landscape") == "portrait":
                preview_image = preview_image.rotate(-90, expand=True)
            
            # Save to in-memory buffer instead of file
            buffer = io.BytesIO()
            preview_image.save(buffer, format="PNG")
            preview_data = buffer.getvalue()
            logging.debug("Created preview image data: %d bytes", len(preview_data))
        except Exception as exc:
            logging.exception("Failed to create preview image data for %s: %s", image_path, exc)
        
        logging.info("Prepared image for display: %s", image_path)
        return resized_image, preview_data, image, None
    except Exception as exc:
        error_msg = f"Failed to prepare image: {type(exc).__name__}: {str(exc)}"
        logging.exception("Failed to prepare image %s: %s", image_path, exc)
        return None, None, None, error_msg


def display_image(inky, prepared_image: Image.Image, image_path: str, original_image: Image.Image | None = None, saturation: float = 0.5) -> bool:
    """Display a prepared image on the Inky display."""
    if inky is None:
        logging.warning("No Inky display available; skipping display for %s", image_path)
        return False
    
    if prepared_image is None:
        logging.warning("No prepared image provided; cannot display %s", image_path)
        return False
    
    try:
        try:
            inky.set_image(prepared_image, saturation=saturation)
        except TypeError:
            inky.set_image(prepared_image)
        inky.show()
        logging.info("Displayed image on Inky: %s", image_path)
        
        # Track the currently displayed image and its source path so other
        # parts of the program can rotate/reapply it when orientation changes.
        try:
            # store originals (not the resized image) for better-quality rotations
            global current_image, current_image_path
            if original_image is not None:
                current_image = original_image.copy()
            current_image_path = image_path
        except Exception as exc:
            logging.debug("Failed to set current_image globals: %s", exc)
        
        return True
    except Exception as exc:
        logging.exception("Failed to display image %s: %s", image_path, exc)
        return False


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


def toggle_orientation_and_apply(inky) -> None:
    """Toggle orientation setting and reapply the current image rotated."""
    try:
        old = config.read_setting("ORIENTATION", "landscape")
        new = "portrait" if (old or "landscape") == "landscape" else "landscape"
        config.write_setting("ORIENTATION", new)
        logging.info("Orientation toggled: %s -> %s", old, new)

        # Attempt to rotate the currently loaded image
        global current_image, current_image_path
        if current_image is not None:
            try:
                prepared, _, orig, error = prepare_image_for_display(inky, current_image_path)
                if prepared:
                    display_image(inky, prepared, current_image_path, orig)
                    logging.info("Reapplied current_image after orientation toggle")
                    return
                elif error:
                    logging.error("Failed to prepare current image: %s", error)
            except Exception as exc:
                logging.exception("Failed to rotate/reapply current_image: %s", exc)

        # If we don't have a current image, try to display the most recent one
        latest = _find_latest_image(config.read_setting("DATA_DIR", "/mnt/usb/data"))
        if latest:
            logging.info("No current image available; showing latest: %s", latest)
            prepared, _, orig, error = prepare_image_for_display(inky, latest)
            if prepared:
                display_image(inky, prepared, latest, orig)
                logging.info("Applied latest image after orientation toggle")
                return
            elif error:
                logging.error("Failed to prepare latest image: %s", error)
        else:
            logging.warning("No image available to show after orientation toggle")
    except Exception as exc:
        logging.exception("toggle_orientation_and_apply failed: %s", exc)


def _monitor_buttons_thread(inky) -> None:
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

        last_press_time = 0
        DEBOUNCE_SECONDS = 15  # Block button presses for roughly the amount of time the display takes to update

        def handle_button(event):
            nonlocal last_press_time
            try:
                # Check if enough time has passed since last button press
                now = time.time()
                if now - last_press_time < DEBOUNCE_SECONDS:
                    logging.debug("Button press ignored (debounce active, %.1fs remaining)", 
                                  DEBOUNCE_SECONDS - (now - last_press_time))
                    return
                
                last_press_time = now
                index = OFFSETS.index(event.line_offset)
                label = LABELS[index]
                logging.info("Button press detected: %s", label)
                if label == "A":
                    toggle_orientation_and_apply(inky)
            except Exception as exc:
                logging.exception("Error handling button event: %s", exc)

        while True:
            for event in request.read_edge_events():
                handle_button(event)
            time.sleep(0.01)
    except Exception as exc:
        logging.exception("Button monitor thread could not start (gpiod/gpiodevice may be unavailable): %s", exc)


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")


def process_uids(uids: list[int], last_uid: int, imap: IMAPClientWrapper, inky, store: UIDStore) -> int:
    """Process a list of message UIDs: fetch, process attachments, send replies, and display images."""
    for uid in sorted(uids):
        if uid <= last_uid:
            continue
        try:
            raw = imap.fetch_message_bytes(uid)
            logging.info("Fetched UID %s (%d bytes)", uid, len(raw) if raw is not None else 0)

            from_addr = email.message_from_bytes(raw).get('From')
            logging.info("Message UID %s from: %s", uid, from_addr)

            # Read SMTP configuration
            smtp_host = config.read_setting("SMTP_HOST", "")
            smtp_port = config.read_setting_int("SMTP_PORT", 587)
            smtp_user = config.read_setting("SMTP_USER", "")
            smtp_pass = config.read_setting("SMTP_PASS", "")
            device_name = config.read_setting("DEVICE_NAME", "Mein Bilderrahmen")

            res = process_message_bytes(
                raw,
                config.read_setting("TMP_DIR", "/mnt/usb/system/tmp"),
                config.read_setting("DATA_DIR", "/mnt/usb/data"),
                config.read_setting_int("ATTACHMENT_MAX_BYTES", 20971520)
            )
            logging.info("Processing result for UID %s: %s", uid, res)

            if res.get("ok"):
                # Step 1: Prepare the image for display (but don't show it yet)
                preview_data = None
                prepared_image = None
                original_image = None
                image_path = None
                image_preparation_failure_message = ""
                try:
                    paths = res.get("paths", []) or []
                    if paths:
                        image_path = paths[0]
                        prepared_image, preview_data, original_image, prep_error = prepare_image_for_display(inky, image_path)
                        if prep_error:
                            image_preparation_failure_message = prep_error
                        logging.info("Prepared image for UID %s: %s", uid, image_path)
                except Exception:
                    image_preparation_failure_message = f"Failed to prepare image for UID {uid} with exception:\n{traceback.format_exc()}"
                    logging.exception(image_preparation_failure_message)

                # Step 2: Send success/failure reply with preview before displaying
                try:
                    if preview_data:
                        # Pass in-memory image data as tuple (data, filename, mimetype)
                        html = render_template("email_success_with_preview.html", image_cid="preview_image", device_name=device_name)
                        send_reply(smtp_host, smtp_port, smtp_user, smtp_pass, from_addr,
                            f"{device_name}: Image received", "Your image was received and stored.",
                            attachments=[(preview_data, "preview.png", "image/png")],
                            html_body=html
                        )
                    else:
                        html = render_template("email_image_prep_failure.html", reason=image_preparation_failure_message, device_name=device_name)
                        send_reply(smtp_host, smtp_port, smtp_user, smtp_pass, from_addr,
                            f"{device_name}: Failed to prepare image", image_preparation_failure_message,
                            html_body=html
                        )
                    logging.info("Sent success reply for UID %s to %s", uid, from_addr)
                except Exception:
                    logging.exception("Failed to send success reply for UID %s to %s", uid, from_addr)
                
                # Step 3: Now display the image on the screen
                try:
                    if prepared_image and image_path:
                        display_image(inky, prepared_image, image_path, original_image)
                        logging.info("Displayed image for UID %s: %s", uid, image_path)
                except Exception:
                    logging.exception("Failed to display image for UID %s", uid)
            else:
                try:
                    error_code = res.get('reason')
                    error_message = get_user_friendly_error(error_code)
                    html = render_template("email_failure.html", error_message=error_message, device_name=device_name)
                    send_reply(smtp_host, smtp_port, smtp_user, smtp_pass, from_addr,
                        f"{device_name}: Image processing failed",
                        f"Reason: {error_message}",
                        html_body=html
                    )
                    logging.info("Sent failure reply for UID %s to %s (reason=%s)", uid, from_addr, res.get('reason'))
                except Exception:
                    logging.exception("Failed to send error reply for UID %s", uid)

            # Cleanup
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
    
    return last_uid


def main() -> None:
    config.load_config()
    setup_logging(config.read_setting("LOG_LEVEL", "INFO"))

    data_dir = config.read_setting("DATA_DIR", "/mnt/usb/data")
    tmp_dir = config.read_setting("TMP_DIR", "/mnt/usb/system/tmp")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    store = UIDStore(os.path.join(data_dir, "uid_state.json"))
    last_uid = store.get_last_uid() or 0

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
        config.read_setting("MAILBOX", "Inbox"),
        config.read_setting("TRASH", "Trash"),
    )
    if not imap.connect():
        logging.exception("Failed to connect to IMAP server; aborting") # We might want to show an error on the display here
        return

    try:
        # Check for any existing messages first
        try:
            uids = imap.get_all_messages_uids()
            logging.info("Found %d unprocessed messages on startup", len(uids))
        except Exception as exc:
            logging.exception("IMAP search failed: %s", exc)
            uids = []
        
        # Process existing messages
        last_uid = process_uids(uids, last_uid, imap, inky, store)
        
        # Main loop: wait for new messages using IDLE
        while True:
            try:
                # Wait for new mail notification (15 minute timeout)
                has_new_mail = imap.idle_wait(timeout=900)
                
                if not has_new_mail:
                    # Timeout reached - check for new mails manually once, then restart IDLE to keep connection alive
                    logging.debug("IDLE timeout, restarting...")
                
                # New mail arrived - fetch and process
                logging.info("New mail notification received")
                uids = imap.get_all_messages_uids()
                logging.info("Found %d messages after IDLE notification", len(uids))
            except Exception as exc:
                logging.exception("IDLE/search failed: %s", exc)
                # Fall back to polling on error
                time.sleep(config.read_setting_int("POLL_INTERVAL", 60))
                try:
                    uids = imap.get_all_messages_uids()
                except Exception:
                    logging.exception("Fallback search also failed")
                    continue

            # Process new messages
            last_uid = process_uids(uids, last_uid, imap, inky, store)

    finally:
        imap.logout()


if __name__ == "__main__":
    main()
