import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    imap_host: str = os.getenv("IMAP_HOST", "")
    imap_port: int = int(os.getenv("IMAP_PORT", "993"))
    imap_user: str = os.getenv("IMAP_USER", "")
    imap_pass: str = os.getenv("IMAP_PASS", "")
    mailbox: str = os.getenv("MAILBOX", "INBOX")
    trash: str = os.getenv("TRASH", "Trash")

    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_pass: str = os.getenv("SMTP_PASS", "")

    poll_interval: int = int(os.getenv("POLL_INTERVAL", "60"))
    attachment_max_bytes: int = int(os.getenv("ATTACHMENT_MAX_BYTES", "5242880"))
    data_dir: str = os.getenv("DATA_DIR", "./data")
    tmp_dir: str = os.getenv("TMP_DIR", "./tmp")
    config_dir: str = os.getenv("CONFIG_DIR", "./config")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


def load_config() -> Config:
    # Ensure a config directory and a default .env exist, then load it.
    default_env = """# IMAP settings
IMAP_HOST="check-your-email-provider-settings-and-add-server-address-here" # e.g., imap.your-email-provider.com
IMAP_PORT=993 # usually 993 for IMAP over SSL but double-check with your email provider
IMAP_USER="this-frame@your-email-provider.com" # your full email address
IMAP_PASS="password-to-your-email-account" # your email account password
MAILBOX="Inbox" # Log in to your email provider's webmail and check the exact name of the inbox folder
TRASH="Trash" # Log in to your email provider's webmail and check the exact name of the trash folder

# SMTP settings
SMTP_HOST="check-your-email-provider-settings-and-add-server-address-here" # e.g., smtp.your-email-provider.com
SMTP_PORT=587 # usually 587 for SMTP with STARTTLS but double-check with your email provider
SMTP_USER="this-frame@your-email-provider.com" # your full email address
SMTP_PASS="password-to-your-email-account" # your email account password
"""
    # use same default as the Config dataclass for CONFIG_DIR
    config_dir = os.getenv("CONFIG_DIR", "./config")
    os.makedirs(config_dir, exist_ok=True)

    # Prefer descriptive filenames. Check in this order and use the first existing file:
    # 1) bilderrahmen.env
    # 2) bilderrahmen.config
    # 3) .env
    candidates = ["bilderrahmen.config", "bilderrahmen.env", ".env"]

    env_path = None
    for name in candidates:
        p = os.path.join(config_dir, name)
        if os.path.exists(p):
            env_path = p
            break

    # If no config file exists, create the preferred name `bilderrahmen.config` with defaults
    if env_path is None:
        env_path = os.path.join(config_dir, candidates[0])
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(default_env)

    # load the env file we found/created (do not override existing env vars)
    load_dotenv(dotenv_path=env_path, override=False)

    return Config()
