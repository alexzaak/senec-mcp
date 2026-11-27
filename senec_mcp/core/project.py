"""Snapshots: "project" files (captured device state)."""

import json
import os
import time

from senec_mcp.core import device

FORMAT = "senec/snapshot-v1"
DEFAULT_SECTIONS = ["ENERGY", "BMS", "PV1", "PM1OBJ1", "TEMPMEASURE", "FAN_SPEED",
                    "WIZARD", "FACTORY", "SYS_UPDATE", "RTC"]


def build_snapshot(raw, host, captured_at=None, names=None):
    decoded = device.decode_all(raw)
    snap = {"format": FORMAT, "host": host,
            "captured_at": captured_at if captured_at is not None else time.time(),
            "sections": sorted(raw), "raw": raw, "decoded": decoded}
    if "ENERGY" in decoded:
        snap["summary"] = device.summarize_status(decoded["ENERGY"], names)
    return snap


def capture(conn, sections=None, names=None):
    sections = [s.upper() for s in (sections or DEFAULT_SECTIONS)]
    raw, _ = device.fetch(sections, conn)
    return build_snapshot(raw, conn["host"], names=names)


def save(snap, path, overwrite=False):
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(f"{path} exists (use --overwrite)")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(snap, f, indent=2)
    return {"path": os.path.abspath(path), "size": os.path.getsize(path)}


def load(path):
    with open(path) as f:
        snap = json.load(f)
    if snap.get("format") != FORMAT:
        raise ValueError(f"{path} is not a SENEC snapshot (format={snap.get('format')!r})")
    return snap


def info(snap):
    return {"host": snap["host"], "captured_at": snap["captured_at"],
            "sections": {s: len(v) for s, v in snap["raw"].items()},
            "variables": sum(len(v) for v in snap["raw"].values()),
            "summary": snap.get("summary")}


def flatten(decoded):
    """{SECTION: {VAR: val|[..]}} → {"SECTION.VAR[i]": val}"""
    flat = {}
    for s, vars_ in decoded.items():
        for k, v in vars_.items():
            if isinstance(v, list):
                for i, x in enumerate(v):
                    flat[f"{s}.{k}[{i}]"] = x
            else:
                flat[f"{s}.{k}"] = v
    return flat


def diff(a, b, tolerance=0.0):
    fa, fb = flatten(a["decoded"]), flatten(b["decoded"])
    changes = []
    for key in sorted(set(fa) | set(fb)):
        va, vb = fa.get(key), fb.get(key)
        if va == vb:
            continue
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            if abs(vb - va) <= tolerance:
                continue
            changes.append({"key": key, "old": va, "new": vb, "delta": round(vb - va, 4)})
        else:
            changes.append({"key": key, "old": va, "new": vb})
    return {"from": a["captured_at"], "to": b["captured_at"],
            "changed": len(changes), "changes": changes}
