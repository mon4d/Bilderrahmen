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


def setup_logging(level: str):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")


def main():
    cfg = load_config()
    setup_logging(cfg.log_level)

    os.makedirs(cfg.data_dir, exist_ok=True)
    os.makedirs(cfg.tmp_dir, exist_ok=True)

    store = UIDStore(os.path.join(cfg.data_dir, "uid_state.json"))
    last_uid = store.get_last_uid() or 0

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
                        send_reply(cfg.smtp_host, cfg.smtp_port, cfg.smtp_user, cfg.smtp_pass, from_addr, "Image received", "Your image was received and stored.")
                        logging.info("Sent success reply for UID %s to %s", uid, from_addr)
                        # Launch the show_image script for the first saved image (non-blocking)
                        try:
                            paths = res.get("paths", []) or []
                            if paths:
                                image_path = paths[0]
                                script_path = os.path.join(os.path.dirname(__file__), "show_image.py")
                                subprocess.Popen([sys.executable, script_path, "--file", image_path])
                                logging.info("Launched show_image for UID %s: %s", uid, image_path)
                        except Exception:
                            logging.exception("Failed to launch show_image for UID %s", uid)
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
                        logging.exception("Failed to empty trash after deleting UID %s", uid)

                    last_uid = max(last_uid, uid)
                    store.set_last_uid(last_uid)
                except Exception:
                    logging.exception("Failed to process UID %s", uid)

            time.sleep(cfg.poll_interval)
    finally:
        imap.logout()


if __name__ == "__main__":
    main()
