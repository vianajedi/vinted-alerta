"""Regista a contagem de likes de cada artigo seguido e decide quando alertar."""
from __future__ import annotations

from dataclasses import dataclass, field

KEEP_HISTORY_S = 6 * 3600       # histórico mínimo de amostras guardado
MAX_SAMPLES = 120
FORGET_AFTER_S = 3 * 24 * 3600  # esquecer artigos que deixaram de aparecer


@dataclass
class Alert:
    kind: str                 # "likes" | "price" | "reserved" | "sold"
    item: dict = field(default_factory=dict)
    gain: int = 0
    minutes: int = 0
    old_price: float | None = None


def window_gain(samples: list, now: float, window_s: float) -> tuple[int, int]:
    """Maior subida de likes até agora, dentro da janela. Devolve (ganho, minutos)."""
    if not samples:
        return 0, 0
    current = samples[-1][1]
    start = now - window_s
    candidates = [(ts, c) for ts, c in samples if ts >= start]
    # Se as verificações forem espaçadas, aceita a última amostra antes da janela
    # desde que não esteja demasiado longe (até 25% além da janela).
    before = [(ts, c) for ts, c in samples if ts < start]
    if before and now - before[-1][0] <= window_s * 1.25:
        candidates.append(before[-1])
    if not candidates:
        return 0, 0
    base_ts, base_c = min(candidates, key=lambda s: (s[1], s[0]))
    gain = current - base_c
    return max(0, gain), max(1, round((now - base_ts) / 60)) if gain > 0 else 0


def record_and_detect(items_state: dict, items: list[dict], cfg: dict, now: float) -> list[Alert]:
    window_s = cfg["window_min"] * 60
    cooldown_s = cfg["cooldown_h"] * 3600
    alerts: list[Alert] = []
    seen = set()

    for it in items:
        iid = str(it["id"])
        seen.add(iid)
        prev = items_state.get(iid)
        if prev is None:
            items_state[iid] = {
                **_public(it),
                "samples": [[now, it["count"]]],
                "last_alert_ts": 0,
                "last_seen": now,
            }
            continue

        samples = prev.setdefault("samples", [])
        samples.append([now, it["count"]])
        cutoff = now - max(KEEP_HISTORY_S, window_s * 2)
        samples[:] = [s for s in samples if s[0] >= cutoff][-MAX_SAMPLES:]

        if it.get("has_count", True):
            gain, minutes = window_gain(samples, now, window_s)
            if gain >= cfg["likes"] and now - prev.get("last_alert_ts", 0) >= cooldown_s:
                alerts.append(Alert("likes", dict(it), gain=gain, minutes=minutes))
                prev["last_alert_ts"] = now

        watched = not str(it.get("source") or "").startswith("search")  # preço/reserva só p/ favoritos e seguidos
        if watched and cfg.get("price_alerts") and prev.get("price") and it.get("price") is not None:
            if it["price"] < prev["price"] - 0.005:
                alerts.append(Alert("price", dict(it), old_price=prev["price"]))

        if watched and cfg.get("reserved_alerts"):
            if it.get("closed") and not prev.get("closed"):
                alerts.append(Alert("sold", dict(it)))
            elif it.get("reserved") and not prev.get("reserved"):
                alerts.append(Alert("reserved", dict(it)))

        prev.update(_public(it))
        prev["last_seen"] = now

    for iid in list(items_state):
        if iid not in seen and now - items_state[iid].get("last_seen", now) > FORGET_AFTER_S:
            del items_state[iid]
    return alerts


def trending(items_state: dict, now: float, window_s: float, limit: int = 5) -> list[tuple[int, dict]]:
    out = []
    for st in items_state.values():
        if now - st.get("last_seen", 0) > 3 * 3600:
            continue
        gain, _ = window_gain(st.get("samples", []), now, window_s)
        if gain > 0:
            out.append((gain, st))
    out.sort(key=lambda x: (-x[0], -x[1].get("count", 0)))
    return out[:limit]


def _public(it: dict) -> dict:
    keys = ("title", "url", "photo", "price", "currency", "count", "size", "brand", "reserved", "closed", "source")
    return {k: it.get(k) for k in keys}
