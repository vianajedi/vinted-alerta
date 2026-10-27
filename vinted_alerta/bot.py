"""Interface no Telegram: barra de pesquisa, seguir artigos/pesquisas, regras com botão Editar e alertas."""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from . import detector
from .state import MAX_FOLLOWED_ITEMS, MAX_FOLLOWED_SEARCHES, clamp
from .vinted import BlockedError, VintedError, item_id_from_text, refine, search_url

log = logging.getLogger("vinted_alerta")

BTN_STATUS = "📊 Estado"
BTN_FOLLOWS = "📌 Seguidos"
BTN_RULES = "⚙️ Regras"
BTN_HELP = "❓ Ajuda"
BUTTONS = {BTN_STATUS, BTN_FOLLOWS, BTN_RULES, BTN_HELP, "🧪 Teste"}

SEARCH_PLACEHOLDER = "🔎 Pesquisar na Vinted…"
RESULTS_SHOWN = 8

MAIN_KB = {
    "keyboard": [[{"text": BTN_STATUS}, {"text": BTN_FOLLOWS}], [{"text": BTN_RULES}, {"text": BTN_HELP}]],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": SEARCH_PLACEHOLDER,
}

COMMANDS_VERSION = 2
COMMANDS = [
    {"command": "pesquisar", "description": "Ex.: /pesquisar casaco levis (ou escreve só o texto)"},
    {"command": "seguidos", "description": "Artigos e pesquisas que estou a seguir"},
    {"command": "estado", "description": "O que está a subir agora"},
    {"command": "regras", "description": "Ver e editar a regra de alerta"},
    {"command": "pausa", "description": "Pausar alertas"},
    {"command": "retomar", "description": "Retomar alertas"},
    {"command": "teste", "description": "Enviar um alerta de teste"},
    {"command": "cookie", "description": "Atualizar a sessão da Vinted (favoritos)"},
    {"command": "ajuda", "description": "Como funciona"},
]

HELP = (
    "<b>Vinted Alerta</b>\n\n"
    "🔎 <b>Pesquisar:</b> escreve na caixa em baixo (ex.: <i>casaco levis</i>). "
    "Nos resultados tens:\n"
    "   • <b>🔔 1–8</b>: seguir esse artigo\n"
    "   • <b>📌 Seguir pesquisa</b>: aviso quando um artigo dessa pesquisa dispara em likes\n"
    "🔗 Também podes colar o link de um artigo da Vinted para o seguir.\n"
    "❤️ Com a sessão da Vinted configurada, sigo também os teus favoritos.\n\n"
    "<b>⚙️ Regras → ✏️ Editar</b> muda likes, janela e pausa entre alertas.\n"
    "<b>📌 Seguidos</b> mostra o que estou a seguir (❌ para deixar de seguir).\n\n"
    "ℹ️ Corro no GitHub em ciclos; uma resposta pode demorar alguns minutos."
)


@dataclass
class Ctx:
    tg: object
    state: dict
    env_chat_id: str | None = None
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Lisbon"))
    force_check: bool = False
    vinted: Callable | None = None      # devolve um VintedClient (criado só quando é preciso)
    domain: str = "www.vinted.pt"

    @property
    def chat_id(self):
        return self.env_chat_id or self.state["tg"].get("chat_id")


# ---------------- formatação ----------------
def fmt_price(p, cur="EUR") -> str:
    if p is None:
        return "—"
    s = f"{p:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €" if (cur or "EUR") == "EUR" else f"{s} {cur}"


def fmt_window(m: int) -> str:
    if m % 60 == 0:
        return f"{m // 60} h"
    if m > 60:
        return f"{m // 60} h {m % 60} min"
    return f"{m} min"


def esc(s) -> str:
    return html.escape(str(s or ""))


def link(it: dict, n: int = 45) -> str:
    title = (it.get("title") or "")
    title = title if len(title) <= n else title[: n - 1] + "…"
    return f'<a href="{esc(it.get("url"))}">{esc(title)}</a>'


def source_label(src: str | None) -> str:
    src = src or ""
    if src == "fav":
        return "❤️ dos teus favoritos"
    if src == "item":
        return "🔔 artigo seguido"
    if src.startswith("search:"):
        return f"📌 pesquisa «{esc(src[7:])}»"
    return ""


def rules_text(cfg: dict) -> str:
    on = lambda b: "ligado ✅" if b else "desligado ❌"  # noqa: E731
    return (
        "⚙️ <b>Regras de alerta</b>\n\n"
        f"🔥 Avisar quando um artigo ganha <b>{cfg['likes']} likes</b> em <b>{fmt_window(cfg['window_min'])}</b>\n"
        f"⏱️ Não repetir o mesmo artigo durante <b>{cfg['cooldown_h']} h</b>\n"
        f"💸 Descida de preço: {on(cfg['price_alerts'])}\n"
        f"🔒 Reservado / vendido: {on(cfg['reserved_alerts'])}\n"
        f"{'⏸️ Alertas em <b>pausa</b>' if cfg['paused'] else '▶️ Alertas <b>ativos</b>'}"
    )


def rules_markup(cfg: dict, editing: bool) -> dict:
    b = lambda t, d: {"text": t, "callback_data": d}  # noqa: E731
    if not editing:
        return {"inline_keyboard": [[
            b("✏️ Editar", "v:edit"),
            b("▶️ Retomar" if cfg["paused"] else "⏸️ Pausar", "v:pause"),
        ]]}
    return {"inline_keyboard": [
        [b("➖", "e:likes:-1"), b(f"❤️ {cfg['likes']} likes", "noop"), b("➕", "e:likes:+1")],
        [b("➖", "e:win:-"), b(f"🕐 em {fmt_window(cfg['window_min'])}", "noop"), b("➕", "e:win:+")],
        [b("➖", "e:cd:-1"), b(f"⏱️ repetir após {cfg['cooldown_h']} h", "noop"), b("➕", "e:cd:+1")],
        [b(f"💸 Preço {'✅' if cfg['price_alerts'] else '❌'}", "t:price"),
         b(f"🔒 Reservado {'✅' if cfg['reserved_alerts'] else '❌'}", "t:res")],
        [b("✅ Concluir", "v:done")],
    ]}


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "nunca"
    m = int((now - ts) // 60)
    return "agora mesmo" if m < 1 else f"há {m} min" if m < 120 else f"há {m // 60} h"


def status_text(state: dict, now: float, tz: ZoneInfo) -> str:
    cfg, meta, fol = state["config"], state["meta"], state["follows"]
    active = [i for i in state["items"].values() if now - i.get("last_seen", 0) < 3 * 3600]
    favs = sum(1 for i in active if i.get("source") == "fav")
    last = meta.get("last_ok")
    hour = datetime.fromtimestamp(last, tz).strftime("%H:%M") if last else "—"
    lines = [
        "📊 <b>Estado</b>\n",
        f"❤️ Favoritos: <b>{favs}</b>" + ("" if state["vinted"].get("user_id") else " (sessão não configurada)"),
        f"🔔 Artigos seguidos: <b>{len(fol['items'])}</b>   📌 Pesquisas: <b>{len(fol['searches'])}</b>",
        f"🔄 Última leitura da Vinted: {_ago(last, now)} ({hour})",
        f"🔥 Regra: {cfg['likes']} likes em {fmt_window(cfg['window_min'])}" + ("  ·  ⏸️ em pausa" if cfg["paused"] else ""),
    ]
    top = detector.trending(state["items"], now, cfg["window_min"] * 60)
    if top:
        lines.append(f"\n📈 <b>A subir (última {fmt_window(cfg['window_min'])})</b>")
        for gain, it in top:
            lines.append(f"+{gain} ❤️ ({it.get('count', 0)}) · {link(it, 38)} — {fmt_price(it.get('price'), it.get('currency'))}")
    elif active:
        lines.append("\n😴 Nada a ganhar likes neste momento.")
    else:
        lines.append("\nEscreve uma pesquisa na caixa em baixo para começar. 🔎")
    return "\n".join(lines)


def follows_view(state: dict) -> tuple[str, dict]:
    fol = state["follows"]
    b = lambda t, d: {"text": t, "callback_data": d}  # noqa: E731
    lines, rows = ["📌 <b>A seguir</b>"], []
    if fol["searches"]:
        lines.append("\n<b>Pesquisas</b>")
        for n, s in enumerate(fol["searches"]):
            label = f" <i>({esc(s['label'])})</i>" if s.get("label") else ""
            name = s["q"] or s.get("title") or "pesquisa"
            lines.append(f"{n + 1}. «{esc(name)}»{label}")
            rows.append([b(f"❌ «{name[:28]}»", f"u:s:{n}")])
    if fol["items"]:
        lines.append("\n<b>Artigos</b>")
        pairs = []
        for n, (iid, it) in enumerate(fol["items"].items()):
            cur = state["items"].get(iid, {})
            likes = f" · ❤️ {cur['count']}" if cur.get("count") is not None and cur else ""
            lines.append(f"{n + 1}. {link({**it, **cur}, 40)}{likes}")
            pairs.append(b(f"❌ {n + 1}", f"u:i:{iid}"))
        rows += [pairs[i:i + 5] for i in range(0, len(pairs), 5)]
    if len(lines) == 1:
        lines.append("\nAinda nada. Escreve uma pesquisa na caixa em baixo 🔎 e carrega em 🔔 ou 📌.")
    lines.append(f"\n<i>Máximo: {MAX_FOLLOWED_SEARCHES} pesquisas e {MAX_FOLLOWED_ITEMS} artigos.</i>")
    return "\n".join(lines), {"inline_keyboard": rows}


def filter_note(info: dict | None) -> str:
    if not info or not (info.get("brands") or info.get("size")):
        return ""
    bits = []
    if info.get("brands"):
        bits.append("marca " + " ".join(b.title() for b in info["brands"]))
    if info.get("size"):
        bits.append(f"tamanho {info['size'].upper()}")
    return f"🎯 <i>Filtrado: {', '.join(bits)} — {info['kept']} de {info['total']}</i>"


def results_view(q: str, order: str, items: list[dict], domain: str, followed: dict,
                 info: dict | None = None) -> tuple[str, dict]:
    names = {"new": "mais recentes", "likes": "mais likes", "low": "mais baratos"}
    shown = items[:RESULTS_SHOWN]
    lines = [f"🔎 <b>«{esc(q)}»</b> — {names.get(order, order)}"]
    if note := filter_note(info):
        lines.append(note)
    for n, it in enumerate(shown, 1):
        extra = " · ".join(esc(x) for x in (it.get("size") and f"Tam. {it['size']}", it.get("condition"), it.get("brand")) if x)
        mark = " 🔔" if it["id"] in followed else ""
        lines.append(f"\n<b>{n}.</b> {link(it)}{mark}\n💶 {fmt_price(it.get('price'), it.get('currency'))} · "
                     f"❤️ {it.get('count', 0)}" + (f" · {extra}" if extra else ""))
    if not shown:
        lines.append("\nSem resultados que batam com a pesquisa. Experimenta outras palavras ou abre na Vinted.")
    b = lambda t, d: {"text": t, "callback_data": d}  # noqa: E731
    follow_btns = [b(f"🔔 {n}", f"f:{it['id']}") for n, it in enumerate(shown, 1)]
    rows = [follow_btns[i:i + 4] for i in range(0, len(follow_btns), 4)]
    rows.append([b(("• " if order == k else "") + t, f"s:o:{k}") for k, t in
                 (("new", "🆕 Recentes"), ("likes", "❤️ Likes"), ("low", "💶 Preço"))])
    rows.append([b("📌 Seguir pesquisa", "s:f"),
                 {"text": "Abrir na Vinted ↗", "url": search_url(domain, q, "rel" if order == "likes" else order)}])
    return "\n".join(lines), {"inline_keyboard": rows}


def alert_message(a: detector.Alert) -> tuple[str, dict, str | None]:
    it = a.item
    title = esc(it.get("title"))
    extra = " · ".join(esc(x) for x in (it.get("brand"), it.get("size")) if x)
    price = fmt_price(it.get("price"), it.get("currency"))
    if a.kind == "likes":
        head = f"🔥 <b>Está a aquecer!</b> +{a.gain} likes em {fmt_window(a.minutes)}"
        body = f"{title}\n❤️ {it.get('count')} likes · 💶 {price}"
    elif a.kind == "price":
        head = "💸 <b>Baixou de preço</b>"
        body = f"{title}\n💶 {fmt_price(a.old_price, it.get('currency'))} → <b>{price}</b> · ❤️ {it.get('count')}"
    elif a.kind == "sold":
        head = "✖️ <b>Vendido / indisponível</b>"
        body = f"{title}\n💶 {price}"
    else:
        head = "🔒 <b>Ficou reservado</b>"
        body = f"{title}\n💶 {price} · ❤️ {it.get('count')}"
    src = source_label(it.get("source"))
    caption = f"{head}\n\n{body}" + (f"\n{extra}" if extra else "") + (f"\n\n<i>{src}</i>" if src else "")
    markup = {"inline_keyboard": [[{"text": "Abrir na Vinted ↗", "url": it.get("url") or "https://www.vinted.pt"}]]}
    return caption, markup, it.get("photo")


def send_alert(tg, chat_id, a: detector.Alert) -> None:
    caption, markup, photo = alert_message(a)
    if photo:
        tg.send_photo(chat_id, photo, caption, markup)
    else:
        tg.send(chat_id, caption, markup)


# ---------------- ações ----------------
def do_search(ctx: Ctx, chat, q: str, order: str = "new", edit_id: int | None = None) -> None:
    q = " ".join(q.split())[:80]
    if len(q) < 2:
        ctx.tg.send(chat, "Escreve pelo menos 2 letras. 🔎", MAIN_KB)
        return
    if ctx.vinted is None:
        ctx.tg.send(chat, "Pesquisa indisponível neste momento.")
        return
    try:
        client = ctx.vinted()
        items = client.search(q, "rel" if order == "likes" else order)
    except BlockedError:
        ctx.tg.send(chat, "🚧 A Vinted bloqueou a pesquisa (proteção anti-bot). Tenta daqui a pouco.", MAIN_KB)
        return
    except (VintedError, OSError) as exc:
        ctx.tg.send(chat, f"⚠️ Não consegui pesquisar: <code>{esc(str(exc)[:120])}</code>", MAIN_KB)
        return
    except Exception as exc:
        log.exception("pesquisa")
        ctx.tg.send(chat, f"⚠️ Erro na pesquisa: <code>{type(exc).__name__}</code>", MAIN_KB)
        return
    items, info = refine(items, q)
    if order == "likes":
        items = sorted(items, key=lambda i: -i.get("count", 0))
    elif order == "low":
        items = sorted(items, key=lambda i: (i.get("price") is None, i.get("price") or 0))
    shown = items[:RESULTS_SHOWN]
    ctx.state["tg"]["last_search"] = {
        "q": q, "order": order,
        "items": {i["id"]: {"title": i["title"], "url": i["url"]} for i in shown},
    }
    text, markup = results_view(q, order, items, ctx.domain, ctx.state["follows"]["items"], info)
    if edit_id:
        ctx.tg.edit(chat, edit_id, text, markup)
    else:
        ctx.tg.send(chat, text, markup)


def follow_item(ctx: Ctx, iid: str, title: str | None = None, url: str | None = None) -> str:
    items = ctx.state["follows"]["items"]
    if iid in items:
        return "Já estou a seguir este artigo 🔔"
    if len(items) >= MAX_FOLLOWED_ITEMS:
        return f"Limite de {MAX_FOLLOWED_ITEMS} artigos. Remove algum em 📌 Seguidos."
    items[iid] = {"title": title or f"Artigo {iid}", "url": url or f"https://{ctx.domain}/items/{iid}", "added": time.time()}
    ctx.force_check = True
    return "🔔 A seguir! Aviso-te quando disparar em likes."


def follow_search(ctx: Ctx, q: str) -> str:
    searches = ctx.state["follows"]["searches"]
    if any(s["q"].lower() == q.lower() for s in searches):
        return "Já estou a seguir esta pesquisa 📌"
    if len(searches) >= MAX_FOLLOWED_SEARCHES:
        return f"Limite de {MAX_FOLLOWED_SEARCHES} pesquisas. Remove alguma em 📌 Seguidos."
    searches.append({"q": q, "added": time.time()})
    ctx.force_check = True
    return "📌 Pesquisa guardada! Aviso-te quando um artigo dela disparar em likes."


# ---------------- tratamento de mensagens ----------------
def handle_update(upd: dict, ctx: Ctx) -> None:
    if "callback_query" in upd:
        _handle_callback(upd["callback_query"], ctx)
    elif "message" in upd:
        _handle_message(upd["message"], ctx)


def _authorized(chat_id, ctx: Ctx, text: str) -> bool:
    if ctx.chat_id is None and text.startswith("/start"):
        ctx.state["tg"]["chat_id"] = str(chat_id)  # primeira pessoa a enviar /start fica associada
        return True
    return ctx.chat_id is not None and str(chat_id) == str(ctx.chat_id)


def _handle_message(msg: dict, ctx: Ctx) -> None:
    chat = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not _authorized(chat, ctx, text):
        ctx.tg.send(chat, "🔒 Este bot é privado.")
        return
    if not text:
        return
    tg, cfg = ctx.tg, ctx.state["config"]
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""

    if cmd == "/start":
        tg.send(chat, "👋 Olá! Estou ligado.\n\n" + HELP, MAIN_KB)
        ctx.force_check = True
    elif cmd == "/estado" or text == BTN_STATUS:
        tg.send(chat, status_text(ctx.state, time.time(), ctx.tz), MAIN_KB)
    elif cmd == "/seguidos" or text == BTN_FOLLOWS:
        t, m = follows_view(ctx.state)
        tg.send(chat, t, m)
    elif cmd in ("/regras", "/config") or text == BTN_RULES:
        tg.send(chat, rules_text(cfg), rules_markup(cfg, False))
    elif cmd in ("/ajuda", "/help") or text == BTN_HELP:
        tg.send(chat, HELP, MAIN_KB)
    elif cmd == "/teste" or text == "🧪 Teste":
        demo = detector.Alert("likes", {
            "title": "Exemplo — Casaco de ganga", "url": f"https://{ctx.domain}", "count": 23,
            "price": 18.0, "currency": "EUR", "brand": "Levi's", "size": "M", "source": "search:casaco levis",
        }, gain=cfg["likes"], minutes=max(10, cfg["window_min"] - 8))
        send_alert(tg, chat, demo)
    elif cmd in ("/likes", "/janela", "/cooldown"):
        key = {"/likes": "likes", "/janela": "window_min", "/cooldown": "cooldown_h"}[cmd]
        try:
            cfg[key] = clamp(key, int(arg.strip()))
            tg.send(chat, "✅ Atualizado.\n\n" + rules_text(cfg), rules_markup(cfg, False))
        except ValueError:
            tg.send(chat, f"Uso: <code>{cmd} número</code>")
    elif cmd in ("/pausa", "/pausar"):
        cfg["paused"] = True
        tg.send(chat, "⏸️ Alertas em pausa. Usa /retomar para voltar.")
    elif cmd in ("/retomar", "/resume"):
        cfg["paused"] = False
        tg.send(chat, "▶️ Alertas retomados.")
        ctx.force_check = True
    elif cmd == "/cookie":
        tg.safe("deleteMessage", chat_id=chat, message_id=msg.get("message_id"))
        if "=" not in arg:
            tg.send(chat, "Cola o valor completo do cabeçalho <code>cookie</code> a seguir a /cookie. "
                          "A tua mensagem é apagada logo a seguir.")
            return
        ctx.state["vinted"]["cookies"] = arg.strip()
        ctx.state["vinted"]["user_id"] = None
        ctx.state["notices"].pop("auth", None)
        tg.send(chat, "🔑 Sessão guardada (e a tua mensagem apagada). Vou testar já.")
        ctx.force_check = True
    elif cmd == "/pesquisar":
        do_search(ctx, chat, arg)
    elif cmd:
        tg.send(chat, "Comando desconhecido. Usa /ajuda.", MAIN_KB)
    elif (iid := item_id_from_text(text)) and "vinted" in text.lower():
        tg.send(chat, follow_item(ctx, iid, url=text.split()[0] if text.startswith("http") else None), MAIN_KB)
    else:
        do_search(ctx, chat, text)


def _handle_callback(cq: dict, ctx: Ctx) -> None:
    msg = cq.get("message") or {}
    chat = msg.get("chat", {}).get("id")
    tg, cfg = ctx.tg, ctx.state["config"]
    if ctx.chat_id is None or str(chat) != str(ctx.chat_id):
        tg.safe("answerCallbackQuery", callback_query_id=cq.get("id"), text="Bot privado")
        return
    data = cq.get("data") or ""
    answer = lambda t=None, alert=False: tg.safe(  # noqa: E731
        "answerCallbackQuery", callback_query_id=cq.get("id"), text=t, show_alert=alert or None)

    # ---- pesquisa / seguir ----
    if data.startswith("f:"):
        iid = data[2:]
        last = (ctx.state["tg"].get("last_search") or {}).get("items", {})
        info = last.get(iid, {})
        res = follow_item(ctx, iid, info.get("title"), info.get("url"))
        answer(res, alert=res.startswith("Limite"))
        return
    if data == "s:f":
        last = ctx.state["tg"].get("last_search")
        if not last:
            answer("Pesquisa expirada — pesquisa outra vez.")
            return
        res = follow_search(ctx, last["q"])
        answer(res, alert=True)
        return
    if data.startswith("s:o:"):
        last = ctx.state["tg"].get("last_search")
        if not last:
            answer("Pesquisa expirada — pesquisa outra vez.")
            return
        answer("A pesquisar…")
        do_search(ctx, chat, last["q"], data[4:], edit_id=msg.get("message_id"))
        return
    if data.startswith("u:"):
        _, kind, key = data.split(":", 2)
        fol = ctx.state["follows"]
        if kind == "i":
            fol["items"].pop(key, None)
        elif kind == "s" and key.isdigit() and int(key) < len(fol["searches"]):
            fol["searches"].pop(int(key))
        answer("Removido")
        t, m = follows_view(ctx.state)
        if msg.get("message_id"):
            tg.edit(chat, msg["message_id"], t, m)
        return

    # ---- regras ----
    editing, toast = True, None
    if data == "noop":
        answer("Usa ➖ e ➕")
        return
    if data == "v:edit":
        editing = True
    elif data == "v:done":
        editing, toast = False, "Guardado ✅"
    elif data == "v:pause":
        cfg["paused"] = not cfg["paused"]
        editing = False
        toast = "Em pausa" if cfg["paused"] else "Retomado"
        if not cfg["paused"]:
            ctx.force_check = True
    elif data.startswith("e:"):
        _, what, op = data.split(":")
        if what == "likes":
            cfg["likes"] = clamp("likes", cfg["likes"] + int(op))
        elif what == "cd":
            cfg["cooldown_h"] = clamp("cooldown_h", cfg["cooldown_h"] + int(op))
        elif what == "win":
            w = cfg["window_min"]
            if op == "+":
                w += 15 if w < 120 else 60
            else:
                w -= 15 if w <= 120 else 60
            cfg["window_min"] = clamp("window_min", w)
    elif data == "t:price":
        cfg["price_alerts"] = not cfg["price_alerts"]
    elif data == "t:res":
        cfg["reserved_alerts"] = not cfg["reserved_alerts"]
    else:
        answer()
        return

    answer(toast)
    if msg.get("message_id"):
        tg.edit(chat, msg["message_id"], rules_text(cfg), rules_markup(cfg, editing))
