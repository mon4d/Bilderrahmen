"""UID persistence storage for Bilderrahmen picture frame.

Provides UIDStore class for atomically saving and loading
processed email UID state using JSON.
"""
# Standard library imports
import json
import os
from typing import Optional


class UIDStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def get_last_uid(self) -> Optional[int]:
        data = self.load()
        return data.get("last_uid")

    def set_last_uid(self, uid: int) -> None:
        data = self.load()
        data["last_uid"] = uid
        self.save(data)
