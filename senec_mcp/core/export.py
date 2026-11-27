"""Export snapshots / time-series to JSON or CSV; live sampling (monitor)."""

import csv
import json
import os
import time

from senec_mcp.core import device, project

FORMATS = ("json", "csv")
SAMPLE_FIELDS = ["timestamp", "state_code", "state", "soc_percent", "pv_w", "house_w",
                 "grid_w", "battery_w", "battery_voltage_v", "battery_current_a"]


def _check(path, overwrite):
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(f"{path} exists (use --overwrite)")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def export_snapshot(snap, path, fmt="csv", overwrite=False):
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format {fmt!r}; choose from {FORMATS}")
    _check(path, overwrite)
    flat = project.flatten(snap["decoded"])
    if fmt == "json":
        with open(path, "w") as f:
            json.dump({"captured_at": snap["captured_at"], "host": snap["host"],
                       "values": flat}, f, indent=2)
    else:
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["key", "value"])
            for k, v in flat.items():
                w.writerow([k, v])
    return {"output": os.path.abspath(path), "format": fmt, "rows": len(flat),
            "file_size": os.path.getsize(path)}


def write_samples(samples, path, fmt="csv", overwrite=False):
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format {fmt!r}; choose from {FORMATS}")
    _check(path, overwrite)
    if fmt == "json":
        with open(path, "w") as f:
            json.dump(samples, f, indent=2)
    else:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=SAMPLE_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(samples)
    return {"output": os.path.abspath(path), "format": fmt, "samples": len(samples),
            "file_size": os.path.getsize(path)}


def sample(conn, count=5, interval=2.0, lang="DE-de", on_sample=None, sleep=time.sleep):
    if count < 1:
        raise ValueError("count must be >= 1")
    if interval < 0:
        raise ValueError("interval must be >= 0")
    out = []
    for i in range(count):
        s = device.get_status(conn, lang)
        out.append(s)
        if on_sample:
            on_sample(i + 1, count, s)
        if i < count - 1:
            sleep(interval)
    return out


def stats(samples):
    keys = ["soc_percent", "pv_w", "house_w", "grid_w", "battery_w"]
    res = {}
    for k in keys:
        vals = [s[k] for s in samples if isinstance(s.get(k), (int, float))]
        if vals:
            res[k] = {"min": min(vals), "max": max(vals),
                      "avg": round(sum(vals) / len(vals), 2)}
    return {"samples": len(samples), "stats": res}
