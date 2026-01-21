"""Orchestrator for Bilderrahmen picture frame.

Poll IMAP inbox for new messages, process image attachments,
display on Inky e-paper display, and send email confirmations.
"""
# Standard library imports
import email
import glob
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import socket
import subprocess
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

REBOOT_MIN_UPTIME_SECONDS = 60 * 60
VERSION = "0.5"

def get_system_uptime_seconds() -> float:
    """Return system uptime in seconds.

    On Raspberry Pi/Linux, `/proc/uptime` is the most direct. We fall back to
    boot/monotonic clocks for non-Linux environments.
    """
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except Exception:
        # This shouldn't happen on the target platform, but just in case it does
        # we assume the system has been up long enough to allow reboots.
        return REBOOT_MIN_UPTIME_SECONDS + 1.0


def perform_reboot(reason: str) -> None:
    """Best-effort system reboot.

    Runs as root in this deployment, so we can directly invoke reboot.
    """
    try:
        logging.error("Rebooting system due to: %s", reason)
        subprocess.run(["/usr/bin/systemctl", "reboot", "--no-block"], check=False)
    except Exception:
        logging.exception("systemctl reboot failed")


# Temp!!
def run_overlayfs_once() -> None:
    """Run `raspi-config nonint do_overlayfs 1` exactly once.

    Uses an atomic lock and a persistent marker file under `config.TMP_DIR`
    to ensure the command only runs a single time across reboots.
    """
    try:
        tmp_dir = getattr(config, "TMP_DIR", "/mnt/usb")
        marker = os.path.join(tmp_dir, ".fixed_overlayfs")

        # Quick exit if already applied
        if os.path.exists(marker):
            logging.info("OverlayFS marker present at %s; skipping setup", marker)
            return

        # Must be root to run raspi-config non-interactively
        if getattr(os, "geteuid", lambda: 1)() != 0:
            logging.warning("Not running as root; cannot run raspi-config. Skipping overlayfs setup.")
            return

        with open(marker, "w", encoding="utf-8") as f:
            f.write("ok\n")
            f.flush()
            os.fsync(f.fileno())
        logging.info("Created one-time overlayfs marker at %s", marker)

        # Locate raspi-config
        rc_path = "/usr/bin/raspi-config"
        cmd = [rc_path, "nonint", "do_overlayfs", "1"]
        logging.info("Running one-time overlayfs command: %s", " ".join(cmd))
        try:
            subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            logging.error("raspi-config not found at %s; skipping", rc_path)
            return

    except Exception:
        logging.exception("One-time overlayfs setup failed")


def _nameservers_from_resolv_conf(path: str = "/etc/resolv.conf") -> list[str]:
    """Best-effort parsing of system DNS servers.

    Returns a list of IP strings (may be empty).
    """
    servers: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == "nameserver":
                    servers.append(parts[1])
    except Exception:
        return []
    return servers


def check_internet_connectivity(timeout_seconds: float = 5.0) -> bool:
    """Return True if we can reach a DNS server, else False.

    This is a lightweight connectivity check: attempt a TCP connection to a
    nameserver on port 53. If at least one server is reachable within
    `timeout_seconds`, we consider the internet connection up.
    """
    # Prefer the system's configured DNS (common in LAN setups), then fall back
    # to well-known public resolvers.
    candidate_servers = _nameservers_from_resolv_conf() + [
        "1.1.1.1",  # Cloudflare
        "8.8.8.8",  # Google
        "9.9.9.9",  # Quad9
    ]

    # De-duplicate while preserving order
    seen: set[str] = set()
    servers: list[str] = []
    for ip in candidate_servers:
        if ip and ip not in seen:
            seen.add(ip)
            servers.append(ip)

    for ip in servers:
        try:
            with socket.create_connection((ip, 53), timeout=timeout_seconds):
                return True
        except OSError:
            continue

    return False


def init_display(ask_user: bool = False):
    """Initialize and return an Inky display instance or None on failure."""
    try:
        from inky.auto import auto

        inky = auto(verbose=True)
        
        # Validate that essential attributes exist and are valid
        if not hasattr(inky, 'resolution') or not hasattr(inky, 'colour'):
            logging.error("Display initialization failed - missing essential attributes")
            return None
        
        if inky.resolution is None or len(inky.resolution) != 2:
            logging.error("Display initialization failed - invalid resolution: %s", inky.resolution)
            return None
        
        logging.info("Initialized Inky display - resolution: %s, colour: %s, module: %s", 
                     inky.resolution, inky.colour, type(inky).__module__)
        
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


def _apply_exif_orientation(image: Image.Image) -> tuple[Image.Image, bool]:
    """Apply EXIF orientation transformation to an image.
    
    Reads only the orientation tag (274) and applies the appropriate rotation/flip,
    ignoring potentially corrupt size metadata that can cause exceptions.
    
    Args:
        image: PIL Image to orient
        
    Returns:
        Tuple of (oriented_image, exif_failed)
        - oriented_image: The transformed image (or original if no orientation data)
        - exif_failed: True if EXIF orientation could not be applied
    """
    try:
        # Try to get EXIF data
        exif = image.getexif()
        if exif is None:
            logging.debug("No EXIF data found in image")
            return image, False
        
        # EXIF orientation tag is 274 (0x0112)
        orientation = exif.get(274)
        if orientation is None:
            logging.debug("No EXIF orientation tag found")
            return image, False
        
        logging.debug("EXIF orientation tag detected: %d", orientation)
        
        # Map EXIF orientation values to PIL transpose operations
        # Reference: https://exif.org/Exif2-2.PDF (page 37)
        orientation_transforms = {
            1: None,  # Normal - no transformation needed
            2: Image.FLIP_LEFT_RIGHT,  # Mirrored horizontally
            3: Image.ROTATE_180,  # Rotated 180°
            4: Image.FLIP_TOP_BOTTOM,  # Mirrored vertically
            5: Image.TRANSPOSE,  # Mirrored horizontally + rotated 270° CCW
            6: Image.ROTATE_270,  # Rotated 90° CW (270° CCW)
            7: Image.TRANSVERSE,  # Mirrored horizontally + rotated 90° CCW
            8: Image.ROTATE_90,  # Rotated 270° CW (90° CCW)
        }
        
        transform = orientation_transforms.get(orientation)
        if transform is None:
            if orientation == 1:
                logging.debug("Image has normal orientation, no transformation needed")
                return image, False
            else:
                logging.warning("Unknown EXIF orientation value: %d", orientation)
                return image, True
        
        # Apply the transformation
        oriented_image = image.transpose(transform)
        logging.debug("Applied EXIF orientation transformation: %d", orientation)
        return oriented_image, False
        
    except Exception as exc:
        logging.warning("Failed to read or apply EXIF orientation: %s", exc)
        return image, True


def prepare_image_for_display(inky, image_path: str):
    """Prepare an image for display: load, orient, resize, and create preview data.
    
    Returns:
        Tuple of (resized_image, preview_data, original_image, error_message, warnings_list)
        On success: (Image, bytes, Image, None, [warnings])
        On failure: (None, None, None, error_string, [])
    """
    if inky is None:
        error_msg = "No Inky display available; cannot prepare image"
        logging.warning("%s for %s", error_msg, image_path)
        return None, None, None, error_msg, []
    
    try:
        import io
        
        warnings_list = []
        image = Image.open(image_path)
        
        # Apply EXIF orientation using custom logic that ignores corrupt size metadata
        oriented_image, exif_failed = _apply_exif_orientation(image)
        
        if exif_failed:
            warnings_list.append("EXIF orientation data could not be applied. Please check the preview to verify your image displays correctly.")
        
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
        return resized_image, preview_data, image, None, warnings_list
    except Exception as exc:
        error_msg = f"Failed to prepare image: {type(exc).__name__}: {str(exc)}"
        logging.exception("Failed to prepare image %s: %s", image_path, exc)
        return None, None, None, error_msg, []


def display_image(inky, prepared_image: Image.Image, image_path: str, original_image: Image.Image | None = None, saturation: float = 0.5) -> bool:
    """Display a prepared image on the Inky display."""
    if inky is None:
        logging.warning("No Inky display available; skipping display for %s", image_path)
        return False
    
    if prepared_image is None:
        logging.warning("No prepared image provided; cannot display %s", image_path)
        return False
    
    # Log inky display state for crash diagnosis
    logging.info("Inky display diagnostics - resolution: %s, colour: %s, module: %s", 
                 getattr(inky, "resolution", "<no resolution>"),
                 getattr(inky, "colour", "<no colour>"),
                 type(inky).__module__)
    
    # Log prepared image details
    logging.info("Prepared image diagnostics - size: %s, mode: %s, format: %s, filename: %s",
                 prepared_image.size,
                 prepared_image.mode,
                 getattr(prepared_image, "format", "<no format>"),
                 image_path)
    
    try:
        try:
            logging.info("Calling inky.set_image() with saturation=%s", saturation)
            inky.set_image(prepared_image, saturation=saturation)
            logging.info("inky.set_image() completed successfully")
        except TypeError:
            logging.info("Calling inky.set_image() without saturation parameter (TypeError fallback)")
            inky.set_image(prepared_image)
            logging.info("inky.set_image() completed successfully (no saturation)")
        
        # Defensive exception handling around show() (may not catch hardware crashes)
        try:
            logging.info("About to call inky.show()")
            inky.show()
            logging.info("inky.show() completed successfully")
        except Exception as exc:
            logging.exception("CAUGHT EXCEPTION in inky.show(): %s (type: %s)", exc, type(exc).__name__)
            raise
        
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

def get_saturation() -> float:
    """Get the saturation setting from config, defaulting to 0.5 on error."""
    try:
        saturation_config_value = config.read_setting("SATURATION", "0.5")
        # Convert config value to float and check for value error
        try:
            saturation_float = float(saturation_config_value)
        except ValueError:
            logging.warning("Invalid SATURATION config value: %s; defaulting to 0.5", saturation_config_value)
            return 0.5
        saturation_clamped = max(0.0, min(1.0, saturation_float))
        logging.debug("Using SATURATION value: %f", saturation_clamped)
        return saturation_clamped
    except Exception:
        logging.exception("Error reading SATURATION config; defaulting to 0.5")
        return 0.5

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
                prepared, _, orig, error, _ = prepare_image_for_display(inky, current_image_path)
                if prepared:
                    display_image(inky, prepared, current_image_path, orig, get_saturation())
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
            prepared, _, orig, error, _ = prepare_image_for_display(inky, latest)
            if prepared:
                display_image(inky, prepared, latest, orig, get_saturation())
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
        # Default for most devices
        SW_C_DEFAULT = 16
        # 13.3" Inky (module EL133UF1) needs GPIO25
        SW_C_13INCH = 25
        SW_D = 24

        # Attempt to detect the inky module string to choose the correct SW_C
        detected_str = "unknown"
        try:
            detected_str = type(inky).__module__
            if "EL133UF1" in detected_str.upper():
                SW_C = SW_C_13INCH
            else:
                SW_C = SW_C_DEFAULT
        except Exception:
            SW_C = SW_C_DEFAULT

        BUTTONS = [SW_A, SW_B, SW_C, SW_D]
        LABELS = ["A", "B", "C", "D"]

        logging.info("Button GPIO mapping: A=%d B=%d C=%d D=%d (detected_inky_module=%s)", SW_A, SW_B, SW_C, SW_D, detected_str)

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
    """Configure logging with console and optional file output.
    
    Enables file logging when LOG_TO_FILE config is set to "true".
    Creates rotating daily log files in LOG_DIR directory.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    log_format = '%(asctime)s %(levelname)s [%(name)s] %(message)s'
    
    # Create root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(console_handler)
    
    # Check if file logging is enabled
    log_to_file = config.read_setting("LOG_TO_FILE", "none")
    if log_to_file == "true":
        try:
            log_dir = config.read_setting("LOG_DIR", "/mnt/usb/system/logs")
            os.makedirs(log_dir, exist_ok=True)
            
            log_file_path = os.path.join(log_dir, "bilderrahmen.log")
            
            # Create file handler with daily rotation
            file_handler = TimedRotatingFileHandler(
                filename=log_file_path,
                when='midnight',
                interval=1,
                backupCount=3,
                encoding='utf-8'
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(logging.Formatter(log_format))
            root_logger.addHandler(file_handler)
            
            logging.info("File logging enabled: %s", log_file_path)
        except Exception as exc:
            logging.warning("Failed to enable file logging: %s", exc)


# Temporary tool to fix broken git update in startup script
def run_git_update(repo_path: str) -> None:
    """Best-effort git update for the repo at `repo_path`.

    Fixes repository ownership to match the running user (root), then performs
    a fast-forward pull. Failures are logged and the program continues.
    """
    try:
        if not os.path.isdir(repo_path):
            logging.warning("Git update skipped; repo path not found: %s", repo_path)
            return

        logging.info("Updating git repo at %s", repo_path)

        # Fix ownership to root (since this app runs as root)
        chown_cmd = ["chown", "-R", "root:root", repo_path]
        chown_res = subprocess.run(chown_cmd, capture_output=True, text=True, check=False)
        if chown_res.returncode != 0:
            logging.warning(
                "Failed to fix repo ownership (code %s): %s%s",
                chown_res.returncode,
                chown_res.stdout or "",
                chown_res.stderr or "",
            )
        else:
            logging.info("Fixed repository ownership to root:root")

        # Perform git pull
        pull_cmd = ["git", "-C", repo_path, "pull", "--ff-only"]
        pull_res = subprocess.run(pull_cmd, capture_output=True, text=True, check=False)
        if pull_res.returncode != 0:
            logging.warning(
                "Git pull failed (code %s): %s%s",
                pull_res.returncode,
                pull_res.stdout or "",
                pull_res.stderr or "",
            )
        else:
            logging.info("Git pull succeeded: %s", (pull_res.stdout or "").strip())
    except Exception:
        logging.exception("Failed to update repo at %s", repo_path)


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

            prepared_image = None
            original_image = None
            image_path = None

            if res.get("ok"):
                # Step 1: Prepare the image for display (but don't show it yet)
                preview_data = None
                image_preparation_failure_message = ""
                warnings = []
                try:
                    paths = res.get("paths", []) or []
                    if paths:
                        image_path = paths[0]
                        prepared_image, preview_data, original_image, prep_error, warnings = prepare_image_for_display(inky, image_path)
                        if prep_error:
                            image_preparation_failure_message = prep_error
                        logging.info("Prepared image for UID %s: %s", uid, image_path)
                except Exception:
                    image_preparation_failure_message = f"Failed to prepare image for UID {uid} with exception:\n{traceback.format_exc()}"
                    logging.exception(image_preparation_failure_message)

                # Step 2A: Send success/failure reply with preview before displaying
                try:
                    if preview_data:
                        # Build warning HTML if there are warnings
                        warning_html = ""
                        if warnings:
                            warning_items = "".join([f"<li>{w}</li>" for w in warnings])
                            warning_html = f'''<div class="warning-box">
                                    <p><strong>⚠️ Notice:</strong></p>
                                    <ul>
                                        {warning_items}
                                    </ul>
                                </div>'''
                        
                        # Pass in-memory image data as tuple (data, filename, mimetype)
                        html = render_template("email_success_with_preview.html", image_cid="preview_image", device_name=device_name, warning_html=warning_html)
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
            else:
                # Step 2B: Send failure reply
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

            # Step 3: Cleanup
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

            # Step 4: Now display the image on the screen
            try:
                if prepared_image and image_path:
                    display_image(inky, prepared_image, image_path, original_image, get_saturation())
                    logging.info("Displayed image for UID %s: %s", uid, image_path)
            except Exception:
                logging.exception("Failed to display image for UID %s", uid)

            last_uid = max(last_uid, uid)
            store.set_last_uid(last_uid)
        except Exception:
            logging.exception("Failed to process UID %s", uid)
    
    return last_uid


def main() -> None:
    config.load_config()
    setup_logging(config.read_setting("LOG_LEVEL", "INFO"))

    logging.info("Starting Bilderrahmen main loop with version %s", VERSION)

    # Temporary git update to fix broken startup script
    run_git_update("/home/bilderrahmen/HeadlessPI/")

    data_dir = config.read_setting("DATA_DIR", "/mnt/usb/data")
    tmp_dir = config.read_setting("TMP_DIR", "/mnt/usb/system/tmp")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    # One-time system configuration: enable OverlayFS if not already done
    try:
        run_overlayfs_once()
    except Exception:
        logging.exception("run_overlayfs_once() execution failed")

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
        
        # Main loop with fallback
        while True:
            try:
                # Actual main loop
                # Fall back to polling on error or IDLE unsupported
                pollinterval_conf = config.read_setting_int("POLL_INTERVAL", 60)
                try:
                    # Wait for new mail notification (15 minute timeout)
                    has_new_mail = imap.idle_wait(timeout=900, pollintervall=pollinterval_conf)
                    
                    if has_new_mail:
                        # New mail arrived
                        logging.debug("IDLE reported new mail available")
                    else:
                        # Timeout reached - check for new mails manually once, then restart IDLE to keep connection alive
                        logging.debug("IDLE timeout or polling interval reached, checking for new mail manually...")
                    
                    uids = imap.get_all_messages_uids()
                    logging.info("Found %d messages", len(uids))
                    time.sleep(1)  # brief pause before the next check
                except Exception as exc:
                    logging.exception("IDLE/search failed: %s", exc)
                    time.sleep(pollinterval_conf)
                    try:
                        uids = imap.get_all_messages_uids()
                    except Exception:
                        logging.exception("Fallback search also failed")
                        continue

                # Process new messages
                last_uid = process_uids(uids, last_uid, imap, inky, store)

                # Check if we should consider rebooting due to connection issues
                try:
                    if not check_internet_connectivity():
                        logging.exception("No internet connectivity detected")
                        try:
                            uptime = get_system_uptime_seconds()
                            if uptime < REBOOT_MIN_UPTIME_SECONDS:
                                logging.warning("Reboot suppressed (system uptime less than %s); continuing without reboot", REBOOT_MIN_UPTIME_SECONDS)
                            else:
                                perform_reboot("Unable to connect to IMAP server")
                        except Exception:
                            # This is highly unlikely, but just in case so we will find something in the logs...
                            logging.exception("Tried to check for reboot but failed")
                except Exception:
                    logging.exception("Failed to check internet connectivity for reboot logic")

            except Exception:
                # Catch-all with sleep to prevent main loop from exiting
                logging.exception("Error in main loop")
                time.sleep(config.read_setting_int("POLL_INTERVAL", 60))

    finally:
        imap.logout()


if __name__ == "__main__":
    main()
