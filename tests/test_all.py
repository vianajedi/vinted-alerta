import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

from vinted_alerta import bot, detector, main, state as st  # noqa: E402
from vinted_alerta import vinted as vt  # noqa: E402
from vinted_alerta.vinted import AuthError, BlockedError, NotFound, VintedClient, normalize_item  # noqa: E402

import fixtures as fx  # noqa: E402

CFG = dict(st.DEFAULT_CONFIG)
T0 = 1_700_000_000


def item(iid="1", count=10, price=20.0, **kw):
    d = {"id": iid, "title": f"Artigo {iid}", "url": f"https://www.vinted.pt/items/{iid}", "photo": None,
         "price": price, "currency": "EUR", "count": count, "has_count": True, "size": "M", "brand": "X",
         "reserved": False, "closed": False}
    d.update(kw)
    return d


# ---------------- deteção ----------------
def test_alerta_5_likes_em_1h():
    s = {}
    assert detector.record_and_detect(s, [item(count=10)], CFG, T0) == []
    assert detector.record_and_detect(s, [item(count=12)], CFG, T0 + 1200) == []
    a = detector.record_and_detect(s, [item(count=15)], CFG, T0 + 2400)
    assert len(a) == 1 and a[0].kind == "likes" and a[0].gain == 5 and a[0].minutes == 40


def test_sem_alerta_com_4_likes_e_fora_da_janela():
    s = {}
    detector.record_and_detect(s, [item(count=10)], CFG, T0)
    assert detector.record_and_detect(s, [item(count=14)], CFG, T0 + 1800) == []
    # 5 likes, mas espalhados por 2 h → não alerta
    s2 = {}
    for k, c in enumerate([10, 11, 12, 13, 14, 15, 15, 15, 15]):
        res = detector.record_and_detect(s2, [item(count=c)], CFG, T0 + k * 900)
    assert res == []


def test_cooldown_evita_repeticao():
    s = {}
    detector.record_and_detect(s, [item(count=0)], CFG, T0)
    assert len(detector.record_and_detect(s, [item(count=6)], CFG, T0 + 600)) == 1
    assert detector.record_and_detect(s, [item(count=13)], CFG, T0 + 1200) == []
    # depois do cooldown (6 h), nova subida volta a alertar
    detector.record_and_detect(s, [item(count=13)], CFG, T0 + 6 * 3600 + 600)
    assert len(detector.record_and_detect(s, [item(count=20)], CFG, T0 + 6 * 3600 + 1200)) == 1


def test_verificacoes_espacadas_aceita_amostra_um_pouco_antes_da_janela():
    s = {}
    detector.record_and_detect(s, [item(count=10)], CFG, T0)
    a = detector.record_and_detect(s, [item(count=16)], CFG, T0 + 70 * 60)  # 70 min ≤ 75 min
    assert len(a) == 1 and a[0].gain == 6
    s2 = {}
    detector.record_and_detect(s2, [item(count=10)], CFG, T0)
    assert detector.record_and_detect(s2, [item(count=16)], CFG, T0 + 90 * 60) == []


def test_janela_configuravel():
    cfg = dict(CFG, likes=3, window_min=30)
    s = {}
    detector.record_and_detect(s, [item(count=1)], cfg, T0)
    assert len(detector.record_and_detect(s, [item(count=4)], cfg, T0 + 25 * 60)) == 1


def test_preco_reservado_vendido():
    s = {}
    detector.record_and_detect(s, [item(price=20.0)], CFG, T0)
    kinds = [a.kind for a in detector.record_and_detect(s, [item(price=15.0, reserved=True)], CFG, T0 + 600)]
    assert kinds == ["price", "reserved"]
    kinds = [a.kind for a in detector.record_and_detect(s, [item(price=15.0, reserved=True, closed=True)], CFG, T0 + 1200)]
    assert kinds == ["sold"]
    s2 = {}
    off = dict(CFG, price_alerts=False, reserved_alerts=False)
    detector.record_and_detect(s2, [item(price=20.0)], off, T0)
    assert detector.record_and_detect(s2, [item(price=10.0, reserved=True)], off, T0 + 600) == []


def test_unlikes_nao_dao_falso_alerta_e_esquece_artigos():
    s = {}
    detector.record_and_detect(s, [item(count=20)], CFG, T0)
    assert detector.record_and_detect(s, [item(count=15)], CFG, T0 + 600) == []
    detector.record_and_detect(s, [], CFG, T0 + 8 * 24 * 3600)
    assert s == {}


# ---------------- estado ----------------
def test_estado_encriptado(tmp_path):
    p = str(tmp_path / "s.enc")
    s = st.default_state()
    s["config"]["likes"] = 9
    st.save(p, s, "frase secreta")
    assert b"likes" not in open(p, "rb").read()
    s2, warn = st.load(p, "frase secreta")
    assert warn is None and s2["config"]["likes"] == 9
    s3, warn = st.load(p, "outra frase")
    assert warn and s3["config"]["likes"] == 5
    with pytest.raises(ValueError):
        st.save(p, s, "")


def test_pesquisa_nao_gera_alertas_de_preco_ou_reserva():
    s = {}
    detector.record_and_detect(s, [item(price=20.0, source="search:x")], CFG, T0)
    assert detector.record_and_detect(s, [item(price=10.0, reserved=True, source="search:x")], CFG, T0 + 600) == []


# ---------------- parsing com a estrutura real das páginas ----------------
BASE = "https://www.vinted.pt"


def test_parse_pagina_de_pesquisa():
    page = fx.search_page([fx.product(10523906221, "Veste en jean Lévis", 3, 15.0),
                           fx.product(10523891458, "Giacca di Levi’s \"vintage\"", 12, 23.0, second="XS · Novo")])
    items = vt.parse_search_page(page, BASE)
    assert [i["id"] for i in items] == ["10523906221", "10523891458"]
    a, b = items
    assert (a["count"], a["price"], a["brand"], a["size"], a["has_count"]) == (3, 15.0, "Levi's", "M", True)
    assert a["url"] == "https://www.vinted.pt/items/10523906221-slug" and a["photo"].startswith("https://images1")
    assert b["title"] == 'Giacca di Levi’s "vintage"' and b["size"] == "XS"


def test_parse_pagina_de_artigo():
    it = vt.parse_item_page(fx.item_page(77, "Casaco Levi's", 9, 70), BASE, "77")
    assert (it["count"], it["price"], it["brand"], it["reserved"], it["closed"]) == (9, 70.0, "Levi's", False, False)
    assert it["url"] == "https://www.vinted.pt/items/77-slug"
    r = vt.parse_item_page(fx.item_page(77, "x", 9, 70, reserved=True, can_buy=False), BASE, "77")
    assert r["reserved"] and not r["closed"]
    sold = vt.parse_item_page(fx.item_page(77, "x", 9, 70, availability="https://schema.org/SoldOut"), BASE, "77")
    assert sold["closed"]
    gone = vt.parse_item_page(fx.item_page(77, "x", 9, 70, can_buy=False), BASE, "77")
    assert gone["closed"]
    with pytest.raises(vt.VintedError):
        vt.parse_item_page("<html>nada</html>", BASE, "77")


def test_id_do_link():
    assert vt.item_id_from_text("olha https://www.vinted.pt/items/10523639648-veste?ref=x") == "10523639648"
    assert vt.item_id_from_text("casaco levis") is None


# ---------------- cliente (HTTP simulado) ----------------
class Resp:
    def __init__(self, code, data=None, text=None):
        self.status_code, self._d = code, data
        self.text = text if text is not None else (__import__("json").dumps(data) if data is not None else "")

    def json(self):
        if self._d is None:
            raise ValueError
        return self._d


class FakeCookies(dict):
    def set(self, k, v, domain=None):
        self[k] = v

    @property
    def jar(self):
        return [type("C", (), {"name": k, "value": v}) for k, v in self.items()]


class FakeSession:
    def __init__(self, responses):
        self.responses, self.cookies, self.log = list(responses), FakeCookies(), []

    def get(self, url, **kw):
        self.log.append(("GET", url, kw.get("params")))
        return self.responses.pop(0)

    def post(self, url, **kw):
        self.log.append(("POST", url, None))
        r = self.responses.pop(0)
        if r.status_code < 300:
            self.cookies["access_token_web"] = "novo"
        return r


DENIED = {"code": 106, "message": "Acesso negado", "message_code": "access_denied"}


def test_cliente_favoritos_refresh_e_paginacao():
    page = lambda n, tot: Resp(200, {"items": [{"id": n, "title": "t", "favourite_count": n,  # noqa: E731
                                                "price": {"amount": "5.0", "currency_code": "EUR"}}],
                                     "pagination": {"total_pages": tot}})
    sess = FakeSession([Resp(401), Resp(200, {}), page(1, 2), page(2, 2)])
    c = VintedClient("www.vinted.pt", "access_token_web=velho; x=1", session=sess)
    out = c.favourites("123")
    assert [(i["id"], i["count"]) for i in out] == [("1", 1), ("2", 2)]
    assert sess.log[1][0] == "POST" and "/web/api/auth/refresh" in sess.log[1][1]
    assert "access_token_web=novo" in c.cookie_header()


def test_403_acesso_negado_e_sessao_nao_bloqueio():
    # comportamento real: /api/v2/users/current sem sessão → 403 {"code":106,...}
    c = VintedClient("www.vinted.pt", "a=1", session=FakeSession([Resp(403, DENIED), Resp(500), Resp(403, DENIED)]))
    with pytest.raises(AuthError):
        c.current_user_id()
    c2 = VintedClient("www.vinted.pt", "", session=FakeSession([Resp(403, DENIED)]))
    with pytest.raises(AuthError):
        c2.favourites("1")
    c3 = VintedClient("www.vinted.pt", "a=1", session=FakeSession([Resp(403, text=fx.BLOCKED_PAGE), Resp(200, text="<html>"),
                                                                  Resp(403, text=fx.BLOCKED_PAGE)]))
    with pytest.raises(BlockedError):
        c3.favourites("1")


def test_cliente_pesquisa_artigo_bloqueio_404():
    page = fx.search_page([fx.product(1, "A", 2, 5.0)])
    sess = FakeSession([Resp(200, text=page)])
    c = VintedClient("www.vinted.pt", session=sess)
    assert c.search("casaco levis")[0]["id"] == "1"
    assert "search_text=casaco+levis" in sess.log[0][1] and "order=newest_first" in sess.log[0][1]
    c2 = VintedClient("www.vinted.pt", session=FakeSession([Resp(200, text=fx.BLOCKED_PAGE), Resp(200, text="<html>"),
                                                            Resp(200, text=fx.BLOCKED_PAGE)]))
    with pytest.raises(BlockedError):
        c2.search("x")
    c3 = VintedClient("www.vinted.pt", session=FakeSession([Resp(200, text="<html>sem artigos</html>")]))
    with pytest.raises(vt.VintedError):
        c3.search("x")
    c4 = VintedClient("www.vinted.pt", session=FakeSession([Resp(404, text="<html>")]))
    with pytest.raises(NotFound):
        c4.item("9")
    c5 = VintedClient("www.vinted.pt", session=FakeSession([Resp(200, text=fx.item_page(9, "C", 4, 12))]))
    assert c5.item("9")["count"] == 4


def test_normalize_api_formatos():
    a = normalize_item({"id": 5, "title": "T", "price": "12,5", "currency": "EUR", "favourite_count": 7,
                        "photos": [{"url": "p"}], "is_closed": 1}, BASE)
    assert (a["price"], a["count"], a["photo"], a["closed"], a["has_count"]) == (12.5, 7, "p", True, True)
    assert a["url"] == "https://www.vinted.pt/items/5"
    b = normalize_item({"id": 6}, BASE)
    assert b["has_count"] is False and b["price"] is None


# ---------------- bot ----------------
class FakeTG:
    def __init__(self, updates=None):
        self.sent, self.edits, self.calls = [], [], []
        self.updates = list(updates or [])

    def send(self, chat, text, markup=None, preview=False):
        self.sent.append((str(chat), text, markup))
        return {"message_id": len(self.sent)}

    def send_photo(self, chat, photo, caption, markup=None):
        return self.send(chat, caption, markup)

    def edit(self, chat, mid, text, markup=None):
        self.edits.append((mid, text, markup))

    def safe(self, method, **kw):
        self.calls.append((method, kw))
        return True

    def get_updates(self, offset, timeout=0):
        out = [u for u in self.updates if u["update_id"] >= offset]
        self.updates = []
        return out


def msg(text, chat=42, uid=1, mid=10):
    return {"update_id": uid, "message": {"message_id": mid, "chat": {"id": chat}, "text": text}}


def cb(data, chat=42, uid=2):
    return {"update_id": uid, "callback_query": {"id": "c", "data": data,
                                                 "message": {"message_id": 99, "chat": {"id": chat}}}}


class FakeClient:
    """Cliente Vinted simulado; `script` define as respostas."""
    searches: dict = {}
    items: dict = {}
    favs: list = []
    fav_error = None
    calls: list = []

    def __init__(self, domain="www.vinted.pt", cookie="", proxy=None):
        self.base = f"https://{domain}"
        self.cookie = cookie

    def current_user_id(self):
        if FakeClient.fav_error:
            raise FakeClient.fav_error
        return "123"

    def favourites(self, uid):
        FakeClient.calls.append(("fav", uid))
        return [dict(i) for i in FakeClient.favs]

    def search(self, q, order="new", filters=None):
        FakeClient.calls.append(("search", q, order))
        r = FakeClient.searches.get(q, [])
        if isinstance(r, Exception):
            raise r
        return [dict(i) for i in r]

    def item(self, iid):
        FakeClient.calls.append(("item", iid))
        r = FakeClient.items[iid]
        if isinstance(r, Exception):
            raise r
        return dict(r)

    def cookie_header(self):
        return self.cookie


@pytest.fixture(autouse=True)
def reset_fake():
    FakeClient.searches, FakeClient.items, FakeClient.favs, FakeClient.fav_error, FakeClient.calls = {}, {}, [], None, []


def ctx_with(state=None, updates=None):
    s = state or st.default_state()
    s["tg"]["chat_id"] = "42"
    c = bot.Ctx(tg=FakeTG(updates), state=s)
    c.vinted = FakeClient
    return c


def test_start_associa_chat_e_bloqueia_estranhos():
    ctx = bot.Ctx(tg=FakeTG(), state=st.default_state())
    bot.handle_update(msg("/start", chat=42), ctx)
    assert ctx.chat_id == "42" and ctx.force_check
    assert ctx.tg.sent[0][2]["input_field_placeholder"] == "🔎 Pesquisar na Vinted…"
    bot.handle_update(msg("/start", chat=7), ctx)
    assert ctx.tg.sent[-1] == ("7", "🔒 Este bot é privado.", None)
    bot.handle_update(msg("casaco", chat=7), ctx)
    assert ctx.tg.sent[-1][1] == "🔒 Este bot é privado." and FakeClient.calls == []


def test_barra_de_pesquisa_resultados_e_seguir():
    FakeClient.searches["casaco levis"] = [item(str(i), count=i, price=30 - i, source=None) for i in range(1, 11)]
    ctx = ctx_with()
    bot.handle_update(msg("  casaco   levis "), ctx)
    text, markup = ctx.tg.sent[-1][1], ctx.tg.sent[-1][2]["inline_keyboard"]
    assert "«casaco levis»" in text and "Artigo 8" in text and "Artigo 9" not in text   # 8 resultados
    assert markup[0][0] == {"text": "🔔 1", "callback_data": "f:1"} and len(markup[0]) == 4
    assert markup[-1][0]["callback_data"] == "s:f" and "search_text=casaco+levis" in markup[-1][1]["url"]
    # seguir artigo 3
    bot.handle_update(cb("f:3"), ctx)
    assert ctx.state["follows"]["items"]["3"]["title"] == "Artigo 3"
    assert ctx.tg.calls[-1][1]["text"].startswith("🔔 A seguir")
    bot.handle_update(cb("f:3"), ctx)
    assert "Já estou" in ctx.tg.calls[-1][1]["text"]
    # seguir a pesquisa
    bot.handle_update(cb("s:f"), ctx)
    assert ctx.state["follows"]["searches"][0]["q"] == "casaco levis"
    # ordenar por likes → edita a mesma mensagem, mais likes primeiro
    bot.handle_update(cb("s:o:likes"), ctx)
    assert FakeClient.calls[-1] == ("search", "casaco levis", "rel")
    assert ctx.tg.edits[-1][1].index("Artigo 10") < ctx.tg.edits[-1][1].index("Artigo 9")


def test_limites_e_lista_de_seguidos():
    ctx = ctx_with()
    for n in range(st.MAX_FOLLOWED_ITEMS):
        bot.follow_item(ctx, str(n))
    assert bot.follow_item(ctx, "x").startswith("Limite")
    for n in range(st.MAX_FOLLOWED_SEARCHES):
        bot.follow_search(ctx, f"q{n}")
    assert bot.follow_search(ctx, "outra").startswith("Limite")
    bot.handle_update(msg(bot.BTN_FOLLOWS), ctx)
    text, kb = ctx.tg.sent[-1][1], ctx.tg.sent[-1][2]["inline_keyboard"]
    assert "«q0»" in text and kb[0][0]["callback_data"] == "u:s:0"
    bot.handle_update(cb("u:s:0"), ctx)
    bot.handle_update(cb("u:i:5"), ctx)
    assert len(ctx.state["follows"]["searches"]) == st.MAX_FOLLOWED_SEARCHES - 1
    assert "5" not in ctx.state["follows"]["items"]


def test_colar_link_segue_artigo_e_erros_de_pesquisa():
    ctx = ctx_with()
    bot.handle_update(msg("https://www.vinted.pt/items/10523639648-veste-en-jean"), ctx)
    assert "10523639648" in ctx.state["follows"]["items"]
    FakeClient.searches["bloq"] = BlockedError("403")
    bot.handle_update(msg("bloq"), ctx)
    assert "bloqueou" in ctx.tg.sent[-1][1]
    bot.handle_update(msg("a"), ctx)
    assert "2 letras" in ctx.tg.sent[-1][1]


def test_botao_editar_altera_regras():
    ctx = ctx_with()
    for d in ["v:edit", "e:likes:+1", "e:likes:+1", "e:win:+", "e:cd:-1", "t:price", "v:done"]:
        bot.handle_update(cb(d), ctx)
    c = ctx.state["config"]
    assert (c["likes"], c["window_min"], c["cooldown_h"], c["price_alerts"]) == (7, 75, 5, False)
    assert ctx.tg.edits[-1][2]["inline_keyboard"][0][0]["text"] == "✏️ Editar"
    assert ctx.tg.edits[1][2]["inline_keyboard"][0][1]["text"] == "❤️ 6 likes"
    for _ in range(300):
        bot.handle_update(cb("e:likes:-1"), ctx)
    assert c["likes"] == 1


def test_comandos_texto_e_cookie():
    ctx = ctx_with()
    bot.handle_update(msg("/likes 8"), ctx)
    bot.handle_update(msg("/janela 45"), ctx)
    bot.handle_update(msg("/janela abc"), ctx)
    assert (ctx.state["config"]["likes"], ctx.state["config"]["window_min"]) == (8, 45)
    bot.handle_update(msg("/cookie access_token_web=abc; refresh_token_web=def", mid=55), ctx)
    assert ctx.state["vinted"]["cookies"].startswith("access_token_web=abc")
    assert ("deleteMessage", {"chat_id": 42, "message_id": 55}) in ctx.tg.calls
    bot.handle_update(msg(bot.BTN_STATUS), ctx)
    assert "Estado" in ctx.tg.sent[-1][1]
    bot.handle_update(msg("/teste"), ctx)
    assert "aquecer" in ctx.tg.sent[-1][1] and "pesquisa «casaco levis»" in ctx.tg.sent[-1][1]


# ---------------- ciclo completo ----------------
def test_ciclo_pesquisa_seguida_alerta_sem_sessao():
    env = main.Env({"STATE_KEY": "k"})          # sem cookie: pesquisas funcionam na mesma
    ctx = ctx_with()
    ctx.state["follows"]["searches"].append({"q": "casaco", "added": T0})
    FakeClient.searches["casaco"] = [item("1", count=2), item("2", count=0)]
    main.check_vinted(env, ctx, FakeClient, now=T0)
    assert ("fav", "123") not in FakeClient.calls
    FakeClient.searches["casaco"] = [item("1", count=8), item("2", count=1, price=1.0, reserved=True)]
    main.check_vinted(env, ctx, FakeClient, now=T0 + 1800)
    texts = [t for _, t, _ in ctx.tg.sent]
    assert len(texts) == 1 and "+6 likes" in texts[0] and "pesquisa «casaco»" in texts[0]


def test_ciclo_favoritos_artigo_seguido_e_falhas_isoladas():
    env = main.Env({"VINTED_COOKIE": "access_token_web=a", "STATE_KEY": "k"})
    ctx = ctx_with()
    ctx.state["follows"]["items"]["9"] = {"title": "Casaco 9", "url": "u"}
    ctx.state["follows"]["items"]["8"] = {"title": "Apagado", "url": "u8"}
    ctx.state["follows"]["searches"].append({"q": "erro", "added": T0})
    FakeClient.favs = [item("1", count=10, price=20.0)]
    FakeClient.items = {"9": item("9", count=1), "8": item("8", count=3)}
    FakeClient.searches["erro"] = vt.VintedError("formato mudou")
    main.check_vinted(env, ctx, FakeClient, now=T0)
    assert "Sessão da Vinted OK" in ctx.tg.sent[-1][1]
    FakeClient.favs = [item("1", count=10, price=15.0)]
    FakeClient.items = {"9": item("9", count=7), "8": NotFound("/items/8")}
    main.check_vinted(env, ctx, FakeClient, now=T0 + 1200)
    texts = [t for _, t, _ in ctx.tg.sent]
    assert any("Baixou de preço" in t and "20,00 € → <b>15,00 €</b>" in t and "favoritos" in t for t in texts)
    assert any("+6 likes" in t and "artigo seguido" in t for t in texts)
    assert any("anúncio foi removido" in t for t in texts)
    assert "8" not in ctx.state["follows"]["items"]
    assert ctx.state["items"]["1"]["source"] == "fav"


def test_ciclo_sessao_expirada_avisa_uma_vez_e_continua():
    env = main.Env({"VINTED_COOKIE": "a=1", "STATE_KEY": "k"})
    ctx = ctx_with()
    ctx.state["follows"]["searches"].append({"q": "x", "added": T0})
    FakeClient.fav_error = AuthError("403")
    FakeClient.searches["x"] = [item("1", count=0)]
    main.check_vinted(env, ctx, FakeClient, now=T0)
    main.check_vinted(env, ctx, FakeClient, now=T0 + 600)
    auth = [t for _, t, _ in ctx.tg.sent if "expirou" in t]
    assert len(auth) == 1 and ctx.state["meta"]["last_ok"] == T0 + 600


def test_ciclo_tudo_bloqueado_avisa_e_recupera():
    env = main.Env({"STATE_KEY": "k"})
    ctx = ctx_with()
    ctx.state["follows"]["searches"].append({"q": "x", "added": T0})
    FakeClient.searches["x"] = BlockedError("403")
    main.check_vinted(env, ctx, FakeClient, now=T0)
    main.check_vinted(env, ctx, FakeClient, now=T0 + 600)
    assert sum("bloqueou" in t for _, t, _ in ctx.tg.sent) == 1
    FakeClient.searches["x"] = [item("1")]
    main.check_vinted(env, ctx, FakeClient, now=T0 + 1200)
    assert "reposta" in ctx.tg.sent[-1][1]


def test_secret_novo_tem_prioridade():
    env = main.Env({"VINTED_COOKIE": "novo=1", "STATE_KEY": "k"})
    s = st.default_state()
    s["vinted"].update(cookies="velho=1", env_hash="outro")
    assert main.effective_cookie(env, s) == "novo=1"
    s["vinted"]["cookies"] = "renovado=1"
    assert main.effective_cookie(env, s) == "renovado=1"


def test_run_cycle_responde_pesquisa_e_respeita_intervalo():
    env = main.Env({"STATE_KEY": "k"})
    s = st.default_state()
    s["follows"]["searches"].append({"q": "x", "added": T0})
    tg = FakeTG([msg("/start", chat=42, uid=5), msg("blusao", chat=42, uid=6)])
    ctx = bot.Ctx(tg=tg, state=s)
    FakeClient.searches = {"x": [item("1")], "blusao": [item("2")]}
    main.run_cycle(env, ctx, 0, FakeClient)
    assert s["tg"]["offset"] == 7 and s["tg"]["chat_id"] == "42"
    assert ("setMyCommands", {"commands": bot.COMMANDS}) in tg.calls
    assert ("search", "blusao", "new") in FakeClient.calls and ("search", "x", "new") in FakeClient.calls
    n = len(FakeClient.calls)
    main.run_cycle(env, ctx, 0, FakeClient)        # dentro do intervalo de 10 min → não volta a ler
    assert len(FakeClient.calls) == n


# ---------------- filtro de relevância (marca + tamanho) ----------------
def test_parse_query_e_url_sem_tamanho():
    pq = vt.parse_query("Fox capacetes tamanho M")
    assert (pq["text"], pq["size"], pq["words"]) == ("Fox capacetes", "M", ["fox", "capacetes"])
    assert vt.parse_query("casaco levis tam. L")["size"] == "L"
    assert vt.parse_query("casaco levis")["size"] is None
    assert "search_text=Fox+capacetes&" in vt.search_url("www.vinted.pt", "Fox capacetes tamanho M")


def test_refine_exige_marca_e_tamanho():
    items = [item("1", title="Capacete mota", brand="novo", size=None),
             item("2", title="Scorpion exo-391", brand="Scorpion", size=None),
             item("3", title="Fox flux capacete mtb", brand="Fox", size=None),
             item("4", title="Capacete Fox V3 tamanho M", brand="Fox", size=None),
             item("5", title="Casco cross", brand="Fox", size="M"),
             item("6", title="Casque Fox", brand="Fox", size="L"),
             item("7", title="Capacete Fox Racing", brand="Fox Racing", size="M / 57-58")]
    out, info = vt.refine(items, "Fox capacetes tamanho M")
    assert [i["id"] for i in out] == ["4", "5", "7"]
    assert info == {"brands": ["fox"], "size": "M", "total": 7, "kept": 3}
    out2, info2 = vt.refine(items, "fox capacete")
    assert [i["id"] for i in out2] == ["3", "4", "5", "6", "7"] and info2["size"] is None
    out3, info3 = vt.refine(items, "capacete mota")      # sem marca conhecida → não filtra
    assert len(out3) == 7 and info3["brands"] == []


def test_estado_nao_e_confundido_com_tamanho():
    page = fx.search_page([fx.product(1, "Capacete", 5, 20.0, brand="DOT", second="Novo com etiquetas"),
                           fx.product(2, "Casaco", 5, 20.0, second="M · Muito bom")])
    a, b = vt.parse_search_page(page, BASE)
    assert (a["size"], a["condition"]) == (None, "Novo com etiquetas")
    assert (b["size"], b["condition"]) == ("M", "Muito bom")


def test_bot_mostra_filtro_aplicado():
    FakeClient.searches["Fox capacetes tamanho M"] = [
        item("2", title="Scorpion exo", brand="Scorpion", size=None, count=27),
        item("5", title="Casco cross", brand="Fox", size="M", count=3, condition="Muito bom")]
    ctx = ctx_with()
    bot.handle_update(msg("Fox capacetes tamanho M"), ctx)
    text = ctx.tg.sent[-1][1]
    assert "Filtrado: marca Fox, tamanho M — 1 de 2" in text
    assert "Casco cross" in text and "Scorpion" not in text and "Tam. M · Muito bom · Fox" in text


# ---------------- pesquisas guardadas da Vinted (filtros + importação) ----------------
def test_pesquisa_com_filtros_da_vinted():
    page = fx.search_page([fx.product(1, "A", 2, 5.0)])
    sess = FakeSession([Resp(200, text=page)])
    c = VintedClient("www.vinted.pt", session=sess)
    c.search("schott homem", "new", {"brand_ids[]": [3799], "size_ids[]": [208, 209]})
    url = sess.log[0][1]
    assert "brand_ids%5B%5D=3799" in url and "size_ids%5B%5D=208" in url and "size_ids%5B%5D=209" in url
    assert vt.search_key("Schott  Homem", {"size_ids[]": [209, 208]}) == vt.search_key("schott homem", {"size_ids[]": [208, 209]})
    assert vt.search_key("x", {"size_ids[]": [1]}) != vt.search_key("x")


def test_importar_pesquisas_uma_vez(tmp_path):
    f = tmp_path / "seguir.json"
    f.write_text('[{"q": "timberland homem", "filters": {"size_ids[]": [781]}, "label": "40,5"}, {"q": "DJI neo 2"}]')
    ctx = ctx_with()
    assert main.import_searches(ctx, str(f)) == 2
    s = ctx.state["follows"]["searches"]
    assert s[0]["filters"] == {"size_ids[]": [781]} and s[1]["q"] == "DJI neo 2"
    assert "<b>+2</b> novas" in ctx.tg.sent[-1][1]
    n = len(ctx.tg.sent)
    assert main.import_searches(ctx, str(f)) == 0 and len(ctx.tg.sent) == n     # não repete
    bot.handle_update(cb("u:s:1"), ctx)                                          # removida no bot…
    assert main.import_searches(ctx, str(f)) == 0                                # …não volta
    assert len(ctx.state["follows"]["searches"]) == 1
    bot.handle_update(msg(bot.BTN_FOLLOWS), ctx)
    assert "«timberland homem» <i>(40,5)</i>" in ctx.tg.sent[-1][1]


def test_importar_respeita_limite(tmp_path):
    import json as _j
    f = tmp_path / "seguir.json"
    f.write_text(_j.dumps([{"q": f"q{i}"} for i in range(st.MAX_FOLLOWED_SEARCHES + 3)]))
    ctx = ctx_with()
    assert main.import_searches(ctx, str(f)) == st.MAX_FOLLOWED_SEARCHES
    assert "limite" in ctx.tg.sent[-1][1]


def test_ciclo_usa_filtros_da_pesquisa_seguida():
    env = main.Env({"STATE_KEY": "k"})
    ctx = ctx_with()
    ctx.state["follows"]["searches"].append({"q": "endura homem", "filters": {"brand_ids[]": [317532]}})
    calls = []

    class C(FakeClient):
        def search(self, q, order="new", filters=None):
            calls.append((q, filters))
            return [item("1", brand="Endura", title="Casaco Endura")]
    main.check_vinted(env, ctx, C, now=T0)
    assert calls == [("endura homem", {"brand_ids[]": [317532]})]


def test_sincroniza_lista_da_vinted(tmp_path):
    import json as _j
    f = tmp_path / "seguir.json"
    f.write_text(_j.dumps([{"q": "DJI neo 2"}, {"q": "adidas", "filters": {"size_ids[]": [207]}}, {"q": "endura"}]))
    ctx = ctx_with()
    bot.follow_search(ctx, "minha pesquisa do bot")
    main.import_searches(ctx, str(f))
    bot.handle_update(cb("u:s:3"), ctx)        # remove "endura" no bot
    # na Vinted: DJI neo 2 deixou de estar guardada, adidas ganhou filtro de marca, nova eastpak
    f.write_text(_j.dumps([{"q": "adidas", "filters": {"size_ids[]": [207], "brand_ids[]": [194976]}},
                           {"q": "endura"}, {"q": "eastpak"}]))
    main.import_searches(ctx, str(f))
    qs = [(x["q"], x.get("filters", {})) for x in ctx.state["follows"]["searches"]]
    assert qs == [("minha pesquisa do bot", {}),
                  ("adidas", {"size_ids[]": [207], "brand_ids[]": [194976]}),
                  ("eastpak", {})]
    assert "+2" in ctx.tg.sent[-1][1] and "−2" in ctx.tg.sent[-1][1]


def test_pesquisa_so_por_categoria_sem_texto(tmp_path):
    import json as _j
    f = tmp_path / "seguir.json"
    f.write_text(_j.dumps([{"q": "", "title": "Homem → Hoodies com fecho",
                            "filters": {"catalog[]": [1812], "brand_ids[]": [4273], "size_ids[]": [207]},
                            "label": "Polo Ralph Lauren, S"}]), encoding="utf-8")
    ctx = ctx_with()
    assert main.import_searches(ctx, str(f)) == 1
    bot.handle_update(msg(bot.BTN_FOLLOWS), ctx)
    assert "«Homem → Hoodies com fecho» <i>(Polo Ralph Lauren, S)</i>" in ctx.tg.sent[-1][1]
    calls = []

    class C(FakeClient):
        def search(self, q, order="new", filters=None):
            calls.append((q, filters))
            return [item("1", brand="Polo Ralph Lauren", title="Hoodie", count=0)]
    main.check_vinted(main.Env({"STATE_KEY": "k"}), ctx, C, now=T0)
    assert calls == [("", {"catalog[]": [1812], "brand_ids[]": [4273], "size_ids[]": [207]})]
    assert ctx.state["items"]["1"]["source"] == "search:Homem → Hoodies com fecho"
    page = fx.search_page([fx.product(1, "A", 2, 5.0)])
    sess = FakeSession([Resp(200, text=page)])
    VintedClient("www.vinted.pt", session=sess).search("", "new", {"catalog[]": [1812]})
    assert "search_text=&" in sess.log[0][1] and "catalog%5B%5D=1812" in sess.log[0][1]
