import os
import logging
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    imap_host: str
    imap_port: int
    imap_user: str
    imap_pass: str
    mailbox: str
    trash: str

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str

    poll_interval: int
    attachment_max_bytes: int
    data_dir: str
    tmp_dir: str
    config_dir: str
    orientation: str
    log_level: str


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

    # load the env file we found/created (override so file values take precedence)
    load_dotenv(dotenv_path=env_path, override=True)

    # Read values from the environment now that the file has been loaded
    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except Exception:
            return default

    imap_host = os.getenv("IMAP_HOST", "")
    imap_port = _int_env("IMAP_PORT", 993)
    imap_user = os.getenv("IMAP_USER", "")
    imap_pass = os.getenv("IMAP_PASS", "")
    mailbox = os.getenv("MAILBOX", "INBOX")
    trash = os.getenv("TRASH", "Trash")

    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = _int_env("SMTP_PORT", 587)
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")

    poll_interval = _int_env("POLL_INTERVAL", 10)
    attachment_max_bytes = _int_env("ATTACHMENT_MAX_BYTES", 5242880)
    data_dir = os.getenv("DATA_DIR", "/mnt/usb/data")
    tmp_dir = os.getenv("TMP_DIR", "/mnt/usb/system/tmp")
    config_dir = os.getenv("CONFIG_DIR", "/mnt/usb")
    log_level = os.getenv("LOG_LEVEL", "INFO")
    orientation = os.getenv("ORIENTATION", "landscape")

    # Create Config instance from the freshly-read environment
    cfg = Config(
        imap_host=imap_host,
        imap_port=imap_port,
        imap_user=imap_user,
        imap_pass=imap_pass,
        mailbox=mailbox,
        trash=trash,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        poll_interval=poll_interval,
        attachment_max_bytes=attachment_max_bytes,
        data_dir=data_dir,
        tmp_dir=tmp_dir,
        config_dir=config_dir,
        orientation=orientation,
        log_level=log_level,
    )

    # Helper to mask sensitive values for logging
    def _mask_secret(s: str) -> str:
        if not s:
            return "<empty>"
        if len(s) <= 2:
            return "*" * len(s)
        return s[0] + "*" * (len(s) - 2) + s[-1]

    # Prepare a summary of important config keys (mask passwords)
    summary = {
        "env_file": env_path,
        "IMAP_HOST": cfg.imap_host or "<empty>",
        "IMAP_PORT": cfg.imap_port,
        "IMAP_USER": cfg.imap_user or "<empty>",
        "IMAP_PASS": _mask_secret(cfg.imap_pass),
        "MAILBOX": cfg.mailbox,
        "TRASH": cfg.trash,
        "SMTP_HOST": cfg.smtp_host or "<empty>",
        "SMTP_PORT": cfg.smtp_port,
        "SMTP_USER": cfg.smtp_user or "<empty>",
        "SMTP_PASS": _mask_secret(cfg.smtp_pass),
        "POLL_INTERVAL": cfg.poll_interval,
        "ATTACHMENT_MAX_BYTES": cfg.attachment_max_bytes,
        "DATA_DIR": cfg.data_dir,
        "TMP_DIR": cfg.tmp_dir,
        "CONFIG_DIR": config_dir,
        "ORIENTATION": cfg.orientation,
        "LOG_LEVEL": cfg.log_level,
    }

    # Print immediately so it's visible even before logging is configured by caller
    print(f"[config] Loaded env file: {summary['env_file']}")
    print("[config] Config summary:")
    for k, v in summary.items():
        if k == "env_file":
            continue
        print(f"  {k}: {v}")

    # Also emit logging messages (may be visible if caller configures logging early)
    logger = logging.getLogger(__name__)
    logger.debug("Config loaded from: %s", summary["env_file"])
    for k, v in summary.items():
        if k == "env_file":
            continue
        logger.debug("%s=%s", k, v)

    return cfg


# Keep track of the env file path that was loaded so callers can persist settings.
_env_file_path: str | None = None


def _get_env_file_path() -> str:
    """Return the path to the environment/config file (creates default if needed).

    This uses the same discovery order as `load_config` so callers can write
    back to the same file that was loaded.
    """
    global _env_file_path
    if _env_file_path:
        return _env_file_path

    config_dir = os.getenv("CONFIG_DIR", "./config")
    candidates = ["bilderrahmen.config", "bilderrahmen.env", ".env"]
    for name in candidates:
        p = os.path.join(config_dir, name)
        if os.path.exists(p):
            _env_file_path = p
            return _env_file_path

    # Not found: create the preferred file
    p = os.path.join(config_dir, candidates[0])
    # create with an empty file so callers can append
    os.makedirs(config_dir, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("")
    _env_file_path = p
    return _env_file_path


def read_setting(name: str, default: str | None = None) -> str | None:
    """Read a single setting from the env/config file. Returns `default` if not present."""
    path = _get_env_file_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(name + "="):
                    # naive parsing: split at first = and strip quotes
                    _, rhs = line.split("=", 1)
                    rhs = rhs.strip()
                    if rhs.startswith('"') and rhs.endswith('"'):
                        return rhs[1:-1]
                    if rhs.startswith("'") and rhs.endswith("'"):
                        return rhs[1:-1]
                    return rhs
    except Exception:
        pass
    return default


def write_setting(name: str, value: str) -> None:
    """Write or update a setting in the env/config file. The value is written
    quoted (double quotes) to be compatible with the default config format.
    """
    path = _get_env_file_path()
    try:
        lines = []
        found = False
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()

        key_prefix = name + "="
        out_lines = []
        for line in lines:
            if line.strip().startswith(name + "="):
                out_lines.append(f'{name}="{value}"\n')
                found = True
            else:
                out_lines.append(line)

        if not found:
            out_lines.append(f'{name}="{value}"\n')

        with open(path, "w", encoding="utf-8") as f:
            f.writelines(out_lines)
    except Exception:
        # best-effort: log via print because logging may not be configured
        print(f"[config] Failed to write setting {name} to {path}")
