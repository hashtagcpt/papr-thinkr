"""Local settings: API keys for the cloud backends.

Keys are kept in a per-user file *outside* the repository, so they can never
be committed by accident:

  Windows   %APPDATA%\\papr-thinkr\\keys.json
  macOS     ~/Library/Application Support/papr-thinkr/keys.json
  Linux     ~/.config/papr-thinkr/keys.json      (or PAPR_CONFIG_DIR)

At startup they are copied into the process environment (an environment
variable you set yourself always wins). The web UI only ever sees masked
values. See docs/api-keys-and-logins.md."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

KEYS = {
    "ANTHROPIC_API_KEY": "Anthropic API key (anthropic-api backend) -- console.anthropic.com",
    "OPENAI_API_KEY": "OpenAI API key (openai-api backend) -- platform.openai.com/api-keys",
    "GEMINI_API_KEY": "Google Gemini API key (gemini-api backend) -- aistudio.google.com/apikey",
    "OPENAI_BASE_URL": "Optional: send openai-api to a local OpenAI-compatible server, e.g. http://127.0.0.1:1234/v1",
}

_from_file: set[str] = set()


def config_dir() -> Path:
    if os.environ.get("PAPR_CONFIG_DIR"):
        return Path(os.environ["PAPR_CONFIG_DIR"])
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "papr-thinkr"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "papr-thinkr"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "papr-thinkr"


def keys_path() -> Path:
    return config_dir() / "keys.json"


def load_keys() -> dict[str, str]:
    p = keys_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: str(v) for k, v in data.items() if k in KEYS and v}


def save_key(name: str, value: str | None) -> None:
    """Store (or, with an empty value, remove) one key and apply it now."""
    if name not in KEYS:
        raise ValueError(f"Unknown key '{name}'. Known: {', '.join(KEYS)}")
    value = (value or "").strip()
    keys = load_keys()
    if value:
        keys[name] = value
    else:
        keys.pop(name, None)
    p = keys_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"keys.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    if value:
        os.environ[name] = value
        _from_file.add(name)
    elif name in _from_file:
        os.environ.pop(name, None)
        _from_file.discard(name)


def apply_keys_to_env() -> None:
    """Copy stored keys into the environment, without overriding variables
    the user set themselves."""
    for k, v in load_keys().items():
        if k not in os.environ or k in _from_file:
            os.environ[k] = v
            _from_file.add(k)


def mask(value: str) -> str:
    if value.startswith("http"):
        return value
    return f"{value[:4]}…{value[-4:]}" if len(value) > 12 else "set"


def key_status() -> list[dict]:
    stored = load_keys()
    out = []
    for name, desc in KEYS.items():
        val = os.environ.get(name)
        source = None
        if val:
            source = "papr-thinkr settings" if name in _from_file or stored.get(name) == val else "environment"
        out.append({"name": name, "description": desc, "set": bool(val), "source": source,
                    "masked": mask(val) if val else None})
    return out
