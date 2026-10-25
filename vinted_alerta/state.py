"""Estado persistente, guardado encriptado (o repositório pode ser público)."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_CONFIG = {
    "likes": 5,            # likes ganhos...
    "window_min": 60,      # ...dentro desta janela (minutos)
    "cooldown_h": 6,       # não repetir alerta do mesmo artigo durante X horas
    "price_alerts": True,  # avisar descidas de preço
    "reserved_alerts": True,  # avisar quando fica reservado / vendido
    "paused": False,
    "check_min": 10,       # intervalo entre leituras da Vinted
}

LIMITS = {
    "likes": (1, 200),
    "window_min": (10, 1440),
    "cooldown_h": (1, 72),
    "check_min": (5, 120),
}

MAX_FOLLOWED_ITEMS = 20
MAX_FOLLOWED_SEARCHES = 25


def default_state() -> dict:
    return {
        "version": 1,
        "config": dict(DEFAULT_CONFIG),
        "items": {},
        "follows": {"items": {}, "searches": [], "imported": []},
        "tg": {"offset": 0, "chat_id": None, "commands_set": 0, "last_search": None},
        "vinted": {"cookies": None, "env_hash": None, "user_id": None},
        "notices": {},
        "meta": {"last_check": None, "last_ok": None, "checks": 0, "had_error": False},
    }


def _merge(base: dict, loaded: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "items":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _fernet(passphrase: str) -> Fernet:
    if not passphrase:
        raise ValueError("Falta STATE_KEY (frase secreta para encriptar o estado).")
    key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode("utf-8")).digest())
    return Fernet(key)


def load(path: str, passphrase: str) -> tuple[dict, str | None]:
    """Devolve (estado, aviso). Se não conseguir ler, começa do zero com aviso."""
    if not os.path.exists(path):
        return default_state(), None
    try:
        with open(path, "rb") as fh:
            raw = _fernet(passphrase).decrypt(fh.read())
        return _merge(default_state(), json.loads(raw)), None
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValueError) and "STATE_KEY" in str(exc):
            raise
        return default_state(), "Não consegui ler o estado anterior (a STATE_KEY mudou?). Recomecei do zero."


def save(path: str, state: dict, passphrase: str) -> None:
    data = _fernet(passphrase).encrypt(json.dumps(state, ensure_ascii=False).encode("utf-8"))
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def clamp(key: str, value: int) -> int:
    lo, hi = LIMITS[key]
    return max(lo, min(hi, int(value)))
