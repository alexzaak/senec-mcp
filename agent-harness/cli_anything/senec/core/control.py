"""Write operations (guarded). Always build a plan first; apply only when confirmed."""

from cli_anything.senec.utils import senec_backend as be

SAFE_CHARGE = {"on": ("ENERGY", "SAFE_CHARGE_FORCE", "u8_01"),
               "off": ("ENERGY", "SAFE_CHARGE_PROHIBIT", "u8_01")}


def plan_set(section, name, value, type_prefix=None, current_raw=None):
    section, name = section.upper(), name.upper()
    if type_prefix is None:
        type_prefix = be.value_type(current_raw)
        if type_prefix is None:
            raise be.SenecError(
                f"Cannot infer type of {section}.{name} (current={current_raw!r}); pass --type")
    typed = be.encode_value(type_prefix, value)
    return {"section": section, "variable": name, "value": value, "typed": typed,
            "previous_raw": current_raw,
            "request": {section: {name: typed}}}


def plan_safe_charge(mode):
    if mode not in SAFE_CHARGE:
        raise ValueError("mode must be 'on' or 'off'")
    s, n, t = SAFE_CHARGE[mode]
    return {"section": s, "variable": n, "value": 1, "typed": t, "mode": mode,
            "request": {s: {n: t}}}


def current_raw(section, name, conn):
    resp = be.read_vars(section.upper(), [name.upper()], **conn)
    v = resp.get(section.upper(), {}).get(name.upper())
    if v in be.ERROR_MARKERS or v is None:
        raise be.SenecError(f"{section}.{name}: {v}")
    return v


def apply(plan, conn):
    resp = be.write_var(plan["section"], plan["variable"], plan["typed"], **conn)
    return {**plan, "applied": True, "response": resp}
