"""Configuration management for Bilderrahmen picture frame.

Provides functions to load, read, and write configuration settings
with file-based storage and atomic writes.
"""
# Standard library imports
import logging
import os
import shutil
import tempfile

# Default configuration values
DEFAULTS_IMAP = {
    # IMAP settings
    "IMAP_HOST": "",
    "IMAP_PORT": "993",
    "IMAP_USER": "",
    "IMAP_PASS": "",
    "MAILBOX": "Inbox",
    "TRASH": "Trash",
}
DEFAULTS_SMTP = {
    # SMTP settings
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASS": "",
}
DEFAULTS_APPLICATION = {
    # Application settings
    "DEVICE_NAME": "Mein Bilderrahmen",
    "ORIENTATION": "landscape",
    "SATURATION": "0.5",
    "ATTACHMENT_MAX_BYTES": "20971520",
    "POLL_INTERVAL": "60",
}
DEFAULTS_DEVELOPMENT = {
    # Development settings
    "DATA_DIR": "/mnt/usb/data",
    "TMP_DIR": "/mnt/usb/system/tmp",
    "LOG_LEVEL": "INFO",
    "LOG_TO_FILE": "none",
    "LOG_DIR": "/mnt/usb/system/logs",
}

# Combine all defaults into a single dictionary
DEFAULTS = {**DEFAULTS_IMAP, **DEFAULTS_SMTP, **DEFAULTS_APPLICATION, **DEFAULTS_DEVELOPMENT}

# Config directory is hardcoded to /mnt/usb
CONFIG_DIR = "/mnt/usb"
CONFIG_FILE = "bilderrahmen.config"

# Keep track of the config file path
_config_file_path: str | None = None

# Template for new config files with descriptions
template = "\n# Bilderrahmen Configuration File\n"
template += "\n"
template += "\n# IMAP settings (for receiving emails with images) - Check your email provider's documentation for these values\n"
template += "\n".join(f"{key}=\"{value}\"" for key, value in DEFAULTS_IMAP.items())
template += "\n"
template += "\n# SMTP settings (for sending confirmation/error emails) - Check your email provider's documentation for these values\n"
template += "\n".join(f"{key}=\"{value}\"" for key, value in DEFAULTS_SMTP.items())
template += "\n"
template += "\n# Application settings - tweak these to customize the frame behavior\n"
template += "\n".join(f"{key}=\"{value}\"" for key, value in DEFAULTS_APPLICATION.items())
template += "\n"
template += "\n# Development settings - only change if you know what you're doing\n"
template += "\n".join(f"{key}=\"{value}\"" for key, value in DEFAULTS_DEVELOPMENT.items())
template += "\n"

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
    except Exception as exc:
        logging.warning("Failed to read existing config keys: %s", exc)
    
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
    except Exception as exc:
        logging.debug("Failed to read setting %s: %s", name, exc)
    return default


def write_setting(name: str, value: str) -> None:
    """Write or update a setting in the config file atomically.
    
    The value is written quoted (double quotes) to be compatible
    with the default config format.
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

        # Write to temporary file in same directory (same filesystem for atomic rename)
        dir_path = os.path.dirname(path)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', 
                                         dir=dir_path, delete=False) as tmp:
            tmp.writelines(out_lines)
            tmp.flush()
            os.fsync(tmp.fileno())  # Force write to disk
            tmp_path = tmp.name
        
        # Atomic rename (replaces old file only after new one is fully written, ensuring file integrity)
        shutil.move(tmp_path, path)
        
    except Exception as e:
        # Clean up temp file if it exists
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception as exc:
                logging.debug("Failed to clean up temp file %s: %s", tmp_path, exc)
        # best-effort: log via print because logging may not be configured
        print(f"[config] Failed to write setting {name} to {path}: {e}")

