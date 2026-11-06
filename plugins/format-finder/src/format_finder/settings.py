"""Settings: defaults/settings.json overlaid with the user's ~/.format-finder/settings.json."""
import json
import re
from pathlib import Path

from .common import FF_HOME, log

DEFAULTS_PATH = Path(__file__).resolve().parents[2] / "defaults" / "settings.json"
USER_PATH = FF_HOME / "settings.json"
SECRETS = ("dtk_api_key",)
KINDS = {"integer": (int,), "number": (int, float), "text": (str,), "boolean": (bool,)}
_warned = set()


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not readable JSON ({exc}). Fix it, or delete it to use the defaults.") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a JSON object of setting names to values. Fix it, or delete it.")
    return data


def _kind(entry):
    """boolean, integer, number or text: the default's own type, or its "type" when the default is null."""
    value = entry["value"]
    if entry.get("type"):
        return entry["type"]
    if value is None:
        return None
    if isinstance(value, bool):
        return "boolean"
    return "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "text"


def _fits(value, entry):
    if value is None:
        return entry["value"] is None
    kind = _kind(entry)
    if kind is None:
        return True
    if kind == "boolean":
        return isinstance(value, bool)
    return not isinstance(value, bool) and isinstance(value, KINDS[kind])


def _user_value(raw):
    return raw["value"] if isinstance(raw, dict) and "value" in raw else raw


def check():
    """(errors, warnings) about the user's settings file. Errors stop every command; warnings do not."""
    defaults = _load(DEFAULTS_PATH)
    try:
        user = _load(USER_PATH)
    except ValueError as exc:
        return [str(exc)], []
    errors = [f"{USER_PATH}: {key} must be {_kind(defaults[key]) or 'the default type'}"
              f"{' or null' if defaults[key]['value'] is None else ''}, got {_user_value(raw)!r}"
              for key, raw in user.items() if key in defaults and not _fits(_user_value(raw), defaults[key])]
    warnings = [f"{USER_PATH}: unknown setting {key!r} is ignored (see format-finder settings for the names)"
                for key in user if key not in defaults]
    return errors, warnings


def effective():
    """{key: {"value", "note", "overridden"}} for every setting. Raises ValueError when the user file is wrong."""
    errors, warnings = check()
    if errors:
        raise ValueError("; ".join(errors))
    for message in warnings:
        if message not in _warned:
            _warned.add(message)
            log(f"[settings] {message}")
    merged = {}
    user = _load(USER_PATH)
    for key, entry in _load(DEFAULTS_PATH).items():
        merged[key] = {"value": entry["value"], "note": entry["note"], "overridden": False}
        if key in user:
            value = _user_value(user[key])
            if value != entry["value"]:
                merged[key].update(value=value, overridden=True)
    return merged


def mask(key, value):
    """A setting as it may be shown: API keys hidden, the password in a proxy URL hidden."""
    if value in (None, ""):
        return value
    if key in SECRETS:
        return "(set, hidden)"
    if isinstance(value, str):
        return re.sub(r"(://[^:/@]*:)[^@/]*@", r"\1***@", value)
    return value


def shown():
    """effective() with secrets masked, for printing."""
    return {k: dict(v, value=mask(k, v["value"])) for k, v in effective().items()}


def get(key):
    return effective()[key]["value"]
