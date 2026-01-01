import os
import logging


# Default configuration values
DEFAULTS = {
    # IMAP settings
    "IMAP_HOST": "",
    "IMAP_PORT": "993",
    "IMAP_USER": "",
    "IMAP_PASS": "",
    "MAILBOX": "INBOX",
    "TRASH": "Trash",
    # SMTP settings
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASS": "",
    # Application settings
    "POLL_INTERVAL": "10",
    "ATTACHMENT_MAX_BYTES": "5242880",
    "DATA_DIR": "/mnt/usb/data",
    "TMP_DIR": "/mnt/usb/system/tmp",
    "ORIENTATION": "landscape",
    "LOG_LEVEL": "INFO",
}

# Config directory is hardcoded to /mnt/usb
CONFIG_DIR = "/mnt/usb"
CONFIG_FILE = "bilderrahmen.config"

# Keep track of the config file path
_config_file_path: str | None = None


def _get_config_file_path() -> str:
    """Return the path to the config file (creates directory if needed)."""
    global _config_file_path
    if _config_file_path:
        return _config_file_path
    
    os.makedirs(CONFIG_DIR, exist_ok=True)
    _config_file_path = os.path.join(CONFIG_DIR, CONFIG_FILE)
    return _config_file_path


def load_config() -> None:
    """Initialize config file with all default values for any missing keys.
    
    Creates the config file if it doesn't exist, and ensures all keys from
    DEFAULTS are present in the file. Prints a summary of loaded configuration.
    """
    path = _get_config_file_path()
    
    # Create config file with comprehensive template if it doesn't exist
    if not os.path.exists(path):
        template = """# Bilderrahmen Configuration File
# Edit the values below according to your email provider settings

# IMAP settings (for receiving emails with images)
IMAP_HOST="" # e.g., "imap.gmail.com" or "imap.your-provider.com"
IMAP_PORT="993" # Usually 993 for IMAP over SSL
IMAP_USER="" # Your full email address
IMAP_PASS="" # Your email password or app-specific password
MAILBOX="INBOX" # Mailbox folder name (check your provider's folder names)
TRASH="Trash" # Trash folder name (check your provider's folder names)

# SMTP settings (for sending confirmation/error emails)
SMTP_HOST="" # e.g., "smtp.gmail.com" or "smtp.your-provider.com"
SMTP_PORT="587" # Usually 587 for SMTP with STARTTLS
SMTP_USER="" # Your full email address
SMTP_PASS="" # Your email password or app-specific password

# Application settings
POLL_INTERVAL="10" # How often to check for new emails (seconds)
ATTACHMENT_MAX_BYTES="5242880" # Maximum attachment size: 5MB
DATA_DIR="/mnt/usb/data" # Directory for storing data
TMP_DIR="/mnt/usb/system/tmp" # Directory for temporary files
ORIENTATION="landscape" # Display orientation: "landscape" or "portrait"
LOG_LEVEL="INFO" # Logging level: DEBUG, INFO, WARNING, ERROR
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(template)
        print(f"[config] Created new config file: {path}")
    
    # Ensure all keys from DEFAULTS exist in the file (write missing ones)
    existing_keys = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key = line.split("=", 1)[0].strip()
                    existing_keys.add(key)
    except Exception:
        pass
    
    # Write any missing keys with their default values
    for key, default_value in DEFAULTS.items():
        if key not in existing_keys:
            write_setting(key, default_value)
            print(f"[config] Added missing key: {key}={default_value}")
    
    # Print configuration summary
    print(f"[config] Loaded config file: {path}")
    print("[config] Configuration summary:")
    for key in sorted(DEFAULTS.keys()):
        value = read_setting(key, DEFAULTS[key])
        # Mask sensitive values
        if "PASS" in key:
            if value and len(value) > 2:
                value = value[0] + "*" * (len(value) - 2) + value[-1]
            elif value:
                value = "*" * len(value)
            else:
                value = "<empty>"
        print(f"  {key}: {value}")


def read_setting_int(name: str, default: int) -> int:
    """Read a setting as an integer. Returns default if not present or invalid."""
    value = read_setting(name, str(default))
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


def read_setting(name: str, default: str | None = None) -> str | None:
    """Read a single setting from the config file. Returns `default` if not present."""
    path = _get_config_file_path()
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
    """Write or update a setting in the config file. The value is written
    quoted (double quotes) to be compatible with the default config format.
    """
    path = _get_config_file_path()
    try:
        lines = []
        found = False
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()

        out_lines = []
        for line in lines:
            if line.strip().startswith(name + "="):
                out_lines.append(f'{name}="{value}"\n')
                found = True
            else:
                out_lines.append(line)

        if not found:
            out_lines.append(f'\n# Added missing setting\n{name}="{value}"\n')

        with open(path, "w", encoding="utf-8") as f:
            f.writelines(out_lines) # can we make this more robust and atomic?
    except Exception:
        # best-effort: log via print because logging may not be configured
        print(f"[config] Failed to write setting {name} to {path}")

