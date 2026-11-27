"""Persistent session: device host config, command history, undo/redo."""

import copy
import json
import os
import time

from cli_anything.senec.utils.senec_backend import DEFAULT_HOST, DEFAULT_TIMEOUT

DEFAULT_PATH = os.path.join(
    os.environ.get("CLI_ANYTHING_SENEC_HOME", os.path.expanduser("~/.cli-anything-senec")),
    "session.json",
)
MAX_HISTORY = 200


def _locked_save_json(path, data, **dump_kwargs):
    try:
        f = open(path, "r+")
    except FileNotFoundError:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        f = open(path, "w")
    with f:
        locked = False
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError):
            pass
        try:
            f.seek(0)
            f.truncate()
            json.dump(data, f, **dump_kwargs)
            f.flush()
        finally:
            if locked:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class Session:
    CONFIG_KEYS = ("host", "verify_tls", "timeout", "lang", "cloud_system")

    def __init__(self, path=None):
        self.path = path or DEFAULT_PATH
        self.config = {"host": DEFAULT_HOST, "verify_tls": False,
                       "timeout": DEFAULT_TIMEOUT, "lang": "DE-de", "cloud_system": None}
        self.history = []
        self.undo_stack = []
        self.redo_stack = []
        self.last_snapshot = None
        if os.path.exists(self.path):
            self.load()

    def load(self):
        with open(self.path) as f:
            data = json.load(f)
        self.config.update(data.get("config", {}))
        self.history = data.get("history", [])
        self.undo_stack = data.get("undo_stack", [])
        self.redo_stack = data.get("redo_stack", [])
        self.last_snapshot = data.get("last_snapshot")

    def save(self):
        _locked_save_json(self.path, self.to_dict(), indent=2)

    def to_dict(self):
        return {"config": self.config, "history": self.history[-MAX_HISTORY:],
                "undo_stack": self.undo_stack, "redo_stack": self.redo_stack,
                "last_snapshot": self.last_snapshot}

    def record(self, command, **meta):
        self.history.append({"ts": time.time(), "command": command, **meta})
        self.history = self.history[-MAX_HISTORY:]

    def set_config(self, key, value):
        if key not in self.CONFIG_KEYS:
            raise ValueError(f"Unknown config key {key!r}; valid: {', '.join(self.CONFIG_KEYS)}")
        if key == "verify_tls" and isinstance(value, str):
            value = value.lower() in ("1", "true", "yes", "on")
        if key == "cloud_system" and str(value).lower() in ("", "none", "auto"):
            value = None
        if key == "timeout":
            value = float(value)
            if value <= 0:
                raise ValueError("timeout must be > 0")
        if key == "host":
            value = str(value).rstrip("/")
            if not value.startswith(("http://", "https://")):
                value = "https://" + value
        self.undo_stack.append(copy.deepcopy(self.config))
        self.redo_stack.clear()
        self.config[key] = value
        self.record(f"set {key}", value=value)
        return self.config

    def undo(self):
        if not self.undo_stack:
            raise ValueError("Nothing to undo")
        self.redo_stack.append(copy.deepcopy(self.config))
        self.config = self.undo_stack.pop()
        self.record("undo")
        return self.config

    def redo(self):
        if not self.redo_stack:
            raise ValueError("Nothing to redo")
        self.undo_stack.append(copy.deepcopy(self.config))
        self.config = self.redo_stack.pop()
        self.record("redo")
        return self.config

    def conn(self):
        return {"host": self.config["host"], "verify": bool(self.config["verify_tls"]),
                "timeout": float(self.config["timeout"])}
