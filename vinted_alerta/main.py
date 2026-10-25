"""Ponto de entrada: lê a Vinted, envia alertas e responde ao bot."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from zoneinfo import ZoneInfo

from . import bot, detector, state as st
from .telegram import Telegram
from .vinted import AuthError, BlockedError, NotFound, VintedClient, VintedError, refine, search_key

log = logging.getLogger("vinted_alerta")

AUTH_HELP = (
    "⚠️ <b>A sessão da Vinted expirou.</b> (Só afeta os ❤️ favoritos; pesquisas e artigos seguidos continuam.)\n\n"
    "No PC: entra em vinted.pt → F12 → separador <i>Rede</i> → recarrega → clica num pedido "
    "<code>/api/v2/…</code> → copia o valor de <code>cookie</code> e envia-me:\n"
    "<code>/cookie</code> + espaço + valor colado.\n(A mensagem é apagada logo a seguir.)"
)


class Env:
    def __init__(self, environ=os.environ):
        g = lambda k, d=None: (environ.get(k) or d)  # noqa: E731
        self.tg_token = g("TELEGRAM_TOKEN")
        self.tg_chat = g("TELEGRAM_CHAT_ID")
        self.cookie = g("VINTED_COOKIE", "")
        self.user_id = g("VINTED_USER_ID")
        self.domain = g("VINTED_DOMAIN", "www.vinted.pt")
        self.state_key = g("STATE_KEY")
        self.state_file = g("STATE_FILE", "state.enc")
        self.proxy = g("PROXY_URL")
        self.tz = ZoneInfo(g("TZ_NAME", "Europe/Lisbon"))


def notify_once(ctx: bot.Ctx, key: str, text: str, every_h: float, now: float) -> None:
    last = ctx.state["notices"].get(key, 0)
    if ctx.chat_id and now - last >= every_h * 3600:
        ctx.tg.send(ctx.chat_id, text)
        ctx.state["notices"][key] = now


def effective_cookie(env: Env, state: dict) -> str:
    """O cookie mais recente ganha; se mudares o secret no GitHub, esse passa à frente."""
    v = state["vinted"]
    h = hashlib.sha256((env.cookie or "").encode()).hexdigest()
    if v.get("env_hash") != h:
        v["env_hash"] = h
        if env.cookie:
            v["cookies"] = None
            v["user_id"] = None
    return v.get("cookies") or env.cookie or ""


class Sources:
    """Junta favoritos + pesquisas seguidas + artigos seguidos, isolando falhas de cada fonte."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.errors: list[Exception] = []
        self.ok = 0

    def add(self, items: list[dict], source: str) -> None:
        self.ok += 1
        for it in items:
            # um artigo pode vir de várias fontes: favorito/seguido têm prioridade sobre pesquisa
            prev = self.items.get(it["id"])
            if prev and not str(prev.get("source", "")).startswith("search"):
                continue
            self.items[it["id"]] = {**it, "source": source}


def check_vinted(env: Env, ctx: bot.Ctx, client_factory=VintedClient, now: float | None = None) -> None:
    now = now or time.time()
    s = ctx.state
    s["meta"]["last_check"] = now
    cookie = effective_cookie(env, s)
    fol = s["follows"]
    has_targets = bool(cookie or fol["items"] or fol["searches"])
    if not has_targets:
        return

    try:
        client = client_factory(env.domain, cookie, env.proxy)
    except Exception as exc:
        log.exception("cliente")
        notify_once(ctx, "error", f"⚠️ Erro a preparar a ligação: <code>{type(exc).__name__}</code>", 6, now)
        return

    src = Sources()
    auth_failed = False

    # 1) favoritos (precisa de sessão)
    if cookie:
        try:
            uid = env.user_id or s["vinted"].get("user_id") or client.current_user_id()
            s["vinted"]["user_id"] = str(uid)
            src.add(client.favourites(str(uid)), "fav")
            new_cookie = client.cookie_header()
            if new_cookie and new_cookie != cookie:
                s["vinted"]["cookies"] = new_cookie  # sessão renovada automaticamente
        except AuthError:
            auth_failed = True
        except Exception as exc:
            src.errors.append(exc)

    # 2) pesquisas seguidas
    for sr in list(fol["searches"]):
        try:
            src.add(refine(client.search(sr["q"], "new", sr.get("filters")), sr["q"])[0], "search:" + sr["q"])
        except Exception as exc:
            src.errors.append(exc)
        time.sleep(1.0)

    # 3) artigos seguidos
    for iid, meta in list(fol["items"].items()):
        try:
            it = client.item(iid)
            meta.update(title=it["title"], url=it["url"])
            src.add([it], "item")
        except NotFound:
            gone = {**meta, "id": iid, "closed": True, "count": s["items"].get(iid, {}).get("count", 0),
                    "has_count": False, "price": s["items"].get(iid, {}).get("price")}
            src.add([gone], "item")
            fol["items"].pop(iid, None)
            if ctx.chat_id:
                ctx.tg.send(ctx.chat_id, f"🗑️ Deixei de seguir {bot.link(meta)} — o anúncio foi removido.")
        except Exception as exc:
            src.errors.append(exc)
        time.sleep(0.8)

    # ---- avisos de erro ----
    if auth_failed:
        notify_once(ctx, "auth", AUTH_HELP, 12, now)
    blocked = [e for e in src.errors if isinstance(e, BlockedError)]
    if src.errors:
        log.warning("Erros: %s", [repr(e) for e in src.errors])
    if src.ok == 0 and not auth_failed:
        s["meta"]["had_error"] = True
        if blocked:
            notify_once(ctx, "blocked", "🚧 A Vinted bloqueou os pedidos (proteção anti-bot). Volto a tentar "
                                        "sozinho; se persistir, vê a secção «Bloqueios» do README.", 6, now)
        else:
            e = src.errors[0] if src.errors else VintedError("?")
            msg = str(e)[:150] if isinstance(e, VintedError) else type(e).__name__
            notify_once(ctx, "error", f"⚠️ Erro ao ler a Vinted: <code>{bot.esc(msg)}</code>", 6, now)
        return
    if src.ok == 0:
        return

    items = list(src.items.values())
    alerts = detector.record_and_detect(s["items"], items, s["config"], now)

    if s["meta"].get("had_error"):
        s["meta"]["had_error"] = False
        for k in ("blocked", "error"):
            if s["notices"].pop(k, None) and ctx.chat_id:
                ctx.tg.send(ctx.chat_id, "✅ Ligação à Vinted reposta.")
                break
    s["meta"]["last_ok"] = now
    s["meta"]["checks"] = s["meta"].get("checks", 0) + 1

    if ctx.chat_id and cookie and not auth_failed and not s["meta"].get("welcomed"):
        s["meta"]["welcomed"] = True
        nfav = sum(1 for i in items if i["source"] == "fav")
        ctx.tg.send(ctx.chat_id, f"✅ Sessão da Vinted OK! A seguir <b>{nfav}</b> favoritos.", bot.MAIN_KB)
    if ctx.chat_id and not s["config"]["paused"]:
        for a in alerts:
            bot.send_alert(ctx.tg, ctx.chat_id, a)


def import_searches(ctx: bot.Ctx, path: str = "seguir.json") -> int:
    """Junta às pesquisas seguidas as que estão em seguir.json (uma vez cada; se as removeres no bot, não voltam)."""
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            entries = json.load(fh)
    except (OSError, ValueError):
        log.warning("seguir.json inválido")
        return 0
    fol = ctx.state["follows"]
    imported = set(fol.setdefault("imported", []))
    current = {search_key(x["q"], x.get("filters")) for x in fol["searches"]}
    added, full = 0, False
    for e in entries:
        q = str(e.get("q") or "").strip()
        if not q:
            continue
        key = search_key(q, e.get("filters"))
        if key in imported:
            continue
        if key not in current:
            if len(fol["searches"]) >= st.MAX_FOLLOWED_SEARCHES:
                full = True
                break
            fol["searches"].append({"q": q, "filters": e.get("filters") or {}, "label": e.get("label") or "",
                                    "added": time.time()})
            current.add(key)
            added += 1
        imported.add(key)
    fol["imported"] = sorted(imported)
    if (added or full) and ctx.chat_id:
        msg = f"📥 Importei <b>{added}</b> pesquisas guardadas da Vinted para 📌 Seguidos."
        if full:
            msg += f"\n⚠️ Atingi o limite de {st.MAX_FOLLOWED_SEARCHES}; as restantes ficaram de fora."
        ctx.tg.send(ctx.chat_id, msg, bot.MAIN_KB)
    if added:
        ctx.force_check = True
    return added


def process_updates(ctx: bot.Ctx, timeout: int) -> int:
    try:
        updates = ctx.tg.get_updates(ctx.state["tg"]["offset"], timeout)
    except Exception as exc:  # rede/Telegram em baixo: não deita a verificação abaixo
        log.warning("getUpdates falhou: %s", exc)
        time.sleep(min(timeout, 5))
        return 0
    for u in updates:
        ctx.state["tg"]["offset"] = u["update_id"] + 1
        try:
            bot.handle_update(u, ctx)
        except Exception:
            log.exception("Erro ao tratar mensagem")
    return len(updates)


def due(ctx: bot.Ctx, now: float) -> bool:
    last = ctx.state["meta"].get("last_check") or 0
    return now - last >= ctx.state["config"]["check_min"] * 60 - 30


def run_cycle(env: Env, ctx: bot.Ctx, poll_seconds: int, client_factory=VintedClient) -> None:
    deadline = time.time() + poll_seconds
    ctx.domain = env.domain
    shared = {}

    def vinted():  # um cliente por ciclo, partilhado pela pesquisa do bot
        if "c" not in shared:
            shared["c"] = client_factory(env.domain, "", env.proxy)
        return shared["c"]

    ctx.vinted = vinted
    if ctx.state["tg"].get("commands_set") != bot.COMMANDS_VERSION:
        if ctx.tg.safe("setMyCommands", commands=bot.COMMANDS) is not None:
            ctx.state["tg"]["commands_set"] = bot.COMMANDS_VERSION

    process_updates(ctx, 0)
    if ctx.force_check or due(ctx, time.time()):
        ctx.force_check = False
        check_vinted(env, ctx, client_factory)

    while (remaining := deadline - time.time()) > 1:
        process_updates(ctx, int(min(50, remaining)))
        if ctx.force_check or due(ctx, time.time()):
            ctx.force_check = False
            check_vinted(env, ctx, client_factory)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Vinted Alerta")
    p.add_argument("--once", action="store_true", help="um ciclo e termina (GitHub Actions)")
    p.add_argument("--poll-seconds", type=int, default=240, help="tempo a ouvir o Telegram por ciclo")
    p.add_argument("--loop", action="store_true", help="correr sem parar (PC / Raspberry Pi)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    env = Env()
    tg = Telegram(env.tg_token)
    state, warn = st.load(env.state_file, env.state_key)
    ctx = bot.Ctx(tg=tg, state=state, env_chat_id=env.tg_chat, tz=env.tz, domain=env.domain)
    if warn and ctx.chat_id:
        tg.send(ctx.chat_id, "ℹ️ " + warn)
    import_searches(ctx)

    try:
        if a.loop:
            while True:
                run_cycle(env, ctx, 600)
                st.save(env.state_file, ctx.state, env.state_key)
        else:
            run_cycle(env, ctx, a.poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        st.save(env.state_file, ctx.state, env.state_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
