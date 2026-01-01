"""Simple IMAP client wrapper using imapclient for polling messages.

Provides IMAPClientWrapper class for managing IMAP connections,
message retrieval, and IMAP IDLE support.
"""
# Standard library imports
import imaplib
import logging
import sys
from typing import List, Optional

# Workaround for Python 3.14+ where imaplib.IMAP4.file is a read-only property.
# imapclient (older versions) tries to assign to .file and raises:
#   AttributeError: property 'file' of 'IMAP4_TLS' object has no setter
# Provide a property with a setter that stores the underlying file as _file.
if sys.version_info >= (3, 14):
    try:
        def _file_get(self):
            return getattr(self, "_file", None)
        def _file_set(self, value):
            # store underlying file-like object in private attr
            object.__setattr__(self, "_file", value)
        imaplib.IMAP4.file = property(_file_get, _file_set)
        logging.getLogger(__name__).info("Applied imaplib.IMAP4.file setter shim for Python %s", ".".join(map(str, sys.version_info[:3])))
    except Exception:
        logging.getLogger(__name__).exception("Failed to apply imaplib.IMAP4.file shim")

from imapclient import IMAPClient


logger = logging.getLogger(__name__)


class IMAPClientWrapper:
    def __init__(self, host: str, port: int, user: str, password: str, mailbox: str = "INBOX", trash_mailbox: str = "Trash"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.mailbox = mailbox
        self.trash_mailbox = trash_mailbox
        self.client: Optional[IMAPClient] = None

    def connect(self) -> bool:
        logger.info("Connecting to IMAP %s:%s", self.host, self.port)
        try:
            self.client = IMAPClient(self.host, port=self.port, use_uid=True, ssl=True)
            self.client.login(self.user, self.password)
            logger.info("Connected to IMAP %s:%s", self.host, self.port)
        except Exception as exc:
            logger.exception("IMAP connect failed: %s", exc)
            self.logout()
            return False
        logger.info("Selecting folder %s on IMAP %s:%s", self.mailbox, self.host, self.port)
        try:
            self.client.select_folder(self.mailbox)
            logger.info("Selected folder %s on IMAP %s:%s", self.mailbox, self.host, self.port)
            return True
        except Exception as exc:
            logger.exception("IMAP select folder %s failed: %s", self.mailbox, exc)
            self.logout()
            return False

    def logout(self):
        try:
            self.client.logout()
        except Exception as exc:
            logger.debug("Logout exception (may be expected): %s", exc)
        self.client = None

    def get_all_messages_uids(self) -> List[int]:
        """Return all message UIDs in the currently selected folder.
        
        Processed messages are expected to be deleted by the caller, so we
        intentionally retrieve every message rather than only UNSEEN.
        """
        if not self.client:
            if not self.connect():
                logger.error("get_all_messages_uids: not connected to IMAP")
                return []
        # Using UID search ensures stability; use 'ALL' to match every message
        try:
            uids = self.client.search(['ALL'])
        except Exception as exc:
            logger.exception("Failed to search for messages: %s", exc)
            return []
        return uids

    def fetch_message_bytes(self, uid: int) -> bytes:
        if not self.client:
            if not self.connect():
                raise ConnectionError("Failed to connect to IMAP server")
        data = self.client.fetch([uid], ['RFC822'])
        msg = data[uid][b'RFC822']
        return msg
    
    def delete_message(self, uid: int) -> None:
        if not self.client:
            if not self.connect():
                return
        try:
            self.client.add_flags([uid], [b'\\Deleted'])
            self.client.expunge()
        except Exception as exc:
            logger.exception("Failed to delete UID %s: %s", uid, exc)

    def empty_trash(self) -> None:
        if not self.client:
            if not self.connect():
                return
        if not self.trash_mailbox:
            logger.info("No trash_mailbox configured; skipping empty_trash")
            return
        try:
            self.client.select_folder(f"INBOX.{self.trash_mailbox}")
            self.client.expunge()
            logger.info("Emptied trash mailbox %s", self.trash_mailbox)
        except Exception as exc:
            logger.warning("Failed to expunge trash mailbox %s: %s", self.trash_mailbox, exc)
        finally:
            try:
                self.client.select_folder(self.mailbox)
            except Exception as exc:
                # best-effort restore
                logger.debug("Failed to restore mailbox selection: %s", exc)

    def idle_wait(self, timeout: int = 900) -> bool:
        """Wait for new messages using IMAP IDLE. Returns True if new mail arrived.
        
        Timeout should be < 29 minutes (1740s) as servers disconnect after 30min.
        Default is 15 minutes (900s).
        """
        if not self.client:
            if not self.connect():
                logger.error("idle_wait: not connected to IMAP")
                return False
        
        try:
            # Check if server supports IDLE
            if b'IDLE' not in self.client.capabilities():
                logger.warning("IMAP server does not support IDLE extension")
                return False
            
            logger.debug("Starting IDLE, waiting for new messages (timeout: %ds)...", timeout)
            self.client.idle()
            
            # Wait for notification or timeout
            responses = self.client.idle_check(timeout=timeout)
            self.client.idle_done()
            
            # Check if any response indicates new mail
            for response in responses:
                if b'EXISTS' in response or b'RECENT' in response:
                    logger.info("IDLE notification: new mail arrived")
                    return True
            
            logger.debug("IDLE timeout reached, no new mail")
            return False
            
        except Exception as e:
            logger.exception("IDLE failed: %s", e)
            # Try to clean up IDLE state
            try:
                self.client.idle_done()
            except Exception:
                pass
            return False
