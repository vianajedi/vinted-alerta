"""Leitura da Vinted (sem API oficial).

Desde out. 2026 a Vinted deixou de servir /api/v2/catalog/items (404). Os artigos
vêm agora embutidos nas páginas (Next.js / RSC):
  • pesquisa  → /catalog?search_text=…   (objetos "productItem" com favouriteCount)
  • artigo    → /items/<id>              (JSON-LD Product + "favourite_count", "is_reserved")
  • favoritos → /api/v2/users/<id>/items/favourites (continua a existir; exige sessão)
"""
from __future__ import annotations

import html as htmllib
import json
import re
import time
import unicodedata
from urllib.parse import urlencode

FAVOURITES_PATH = "/api/v2/users/{uid}/items/favourites"
CURRENT_USER_PATH = "/api/v2/users/current"
REFRESH_PATH = "/web/api/auth/refresh"

API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
}
HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
}

ORDERS = {"new": "newest_first", "rel": "relevance", "low": "price_low_to_high", "high": "price_high_to_low"}


class VintedError(Exception):
    pass


class AuthError(VintedError):
    """Sessão inválida, expirada ou inexistente."""


class BlockedError(VintedError):
    """Pedido bloqueado pela proteção anti-bot (DataDome)."""


class NotFound(VintedError):
    """Artigo apagado / inexistente."""


def parse_cookie_header(header: str) -> dict:
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out


# ---------------------------------------------------------------- parsing
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_LD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S)
_DEC = json.JSONDecoder()


def rsc_payload(page: str) -> str:
    """Junta os blocos RSC (self.__next_f.push) da página, já descodificados."""
    parts = []
    for m in _PUSH_RE.finditer(page):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            continue
    return "".join(parts)


def _objects_after(text: str, key: str):
    """Para cada ocorrência de "key":{…}, devolve o objeto JSON."""
    needle = f'"{key}":'
    pos = 0
    while (i := text.find(needle, pos)) >= 0:
        j = i + len(needle)
        while j < len(text) and text[j] in " \t\n":
            j += 1
        pos = j
        if j < len(text) and text[j] == "{":
            try:
                obj, end = _DEC.raw_decode(text, j)
                pos = end
                yield obj
            except ValueError:
                continue


def _enclosing_object(text: str, pos: int) -> dict | None:
    depth = 0
    for i in range(pos, -1, -1):
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                try:
                    return _DEC.raw_decode(text, i)[0]
                except ValueError:
                    return None
            depth -= 1
    return None


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", "").replace(" ", ""))
    except ValueError:
        return None


def _abs(url: str | None, base: str) -> str | None:
    if url and url.startswith("/"):
        return base + url
    return url


def normalize_product(p: dict, base: str) -> dict:
    """Formato novo das páginas de pesquisa ("productItem")."""
    iid = str(p.get("id"))
    price = p.get("price") or {}
    box = p.get("itemBox") or {}
    photo = p.get("thumbnailUrl")
    if not photo and p.get("photos"):
        photo = (p["photos"][0] or {}).get("url")
    # secondLine = "M · Muito bom" (tamanho · estado) ou só "Novo com etiquetas" (sem tamanho)
    parts = [x.strip() for x in (box.get("secondLine") or "").split("·") if x.strip()]
    size = parts[0] if len(parts) >= 2 else None
    condition = parts[-1] if parts else None
    return {
        "id": iid,
        "title": p.get("title") or f"Artigo {iid}",
        "url": _abs(p.get("url") or f"/items/{iid}", base),
        "photo": photo,
        "price": _to_float(price.get("amount") if isinstance(price, dict) else price),
        "currency": (price.get("currencyCode") if isinstance(price, dict) else None) or "EUR",
        "count": int(p.get("favouriteCount") or 0),
        "has_count": "favouriteCount" in p,
        "size": size,
        "condition": condition,
        "brand": box.get("firstLine"),
        "reserved": False,
        "closed": False,
    }


def normalize_item(raw: dict, base: str) -> dict:
    """Formato da API JSON (favoritos)."""
    price = raw.get("price")
    if isinstance(price, dict):
        amount, cur = price.get("amount"), price.get("currency_code") or price.get("currencyCode")
    else:
        amount, cur = price, raw.get("currency")
    photo = (raw.get("photo") or {}).get("url") if isinstance(raw.get("photo"), dict) else None
    if not photo and raw.get("photos"):
        photo = (raw["photos"][0] or {}).get("url")
    iid = str(raw.get("id"))
    count_key = next((k for k in ("favourite_count", "favourites_count", "favouriteCount", "favourites") if k in raw), None)
    return {
        "id": iid,
        "title": raw.get("title") or f"Artigo {iid}",
        "url": _abs(raw.get("url") or f"/items/{iid}", base),
        "photo": photo,
        "price": _to_float(amount),
        "currency": cur or "EUR",
        "count": int(raw.get(count_key) or 0) if count_key else 0,
        "has_count": count_key is not None,
        "size": raw.get("size_title") or raw.get("size"),
        "brand": raw.get("brand_title") or raw.get("brand"),
        "reserved": bool(raw.get("is_reserved")),
        "closed": bool(raw.get("is_closed") or raw.get("is_sold")),
    }


def parse_search_page(page: str, base: str) -> list[dict]:
    payload = rsc_payload(page) or page
    out, seen = [], set()
    for p in _objects_after(payload, "productItem"):
        if p.get("id") and str(p["id"]) not in seen:
            seen.add(str(p["id"]))
            out.append(normalize_product(p, base))
    return out


def parse_item_page(page: str, base: str, item_id: str) -> dict:
    product = {}
    for m in _LD_RE.finditer(page):
        try:
            data = json.loads(htmllib.unescape(m.group(1)) if "&quot;" in m.group(1) else m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            product = data
            break
    payload = rsc_payload(page) or page
    count, has_count, reserved, can_buy = 0, False, False, True
    for key in ("favourite_count", "is_reserved"):
        for m in re.finditer(f'"{key}":', payload):
            obj = _enclosing_object(payload, m.start())
            if not obj or str(obj.get("item_id", item_id)) != str(item_id):
                continue
            if key == "favourite_count":
                count, has_count = int(obj.get("favourite_count") or 0), True
            else:
                reserved = bool(obj.get("is_reserved"))
                can_buy = obj.get("can_buy", True) is not False
            break
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    availability = str(offers.get("availability") or "")
    brand = product.get("brand")
    closed = any(s in availability for s in ("SoldOut", "OutOfStock", "Discontinued")) or (
        not can_buy and not reserved and bool(product))
    if not product and not has_count:
        raise VintedError("página do artigo sem dados reconhecíveis")
    return {
        "id": str(item_id),
        "title": product.get("name") or f"Artigo {item_id}",
        "url": offers.get("url") or f"{base}/items/{item_id}",
        "photo": product.get("image") if isinstance(product.get("image"), str) else None,
        "price": _to_float(offers.get("price")),
        "currency": offers.get("priceCurrency") or "EUR",
        "count": count,
        "has_count": has_count,
        "size": None,
        "brand": brand.get("name") if isinstance(brand, dict) else brand,
        "reserved": reserved,
        "closed": closed,
    }


def _looks_blocked(text: str) -> bool:
    """Página de desafio da DataDome em vez do conteúdo."""
    return "captcha-delivery.com" in text[:20000]


# ---------------------------------------------------------------- client
class VintedClient:
    def __init__(self, domain: str, cookie_header: str = "", proxy: str | None = None, session=None):
        if session is None:
            from curl_cffi import requests as cr  # imita o TLS de um Chrome real
            session = cr.Session(impersonate="chrome", proxies={"https": proxy, "http": proxy} if proxy else None)
        self.s = session
        self.domain = domain
        self.base = f"https://{domain}"
        self.cookie_domain = "." + domain.removeprefix("www.")
        self.logged_in = bool(cookie_header)
        for k, v in parse_cookie_header(cookie_header).items():
            self.s.cookies.set(k, v, domain=self.cookie_domain)
        self._warmed = False

    # ---------- HTTP ----------
    def _api(self, path: str, params: dict | None = None, _retry: bool = True):
        r = self.s.get(self.base + path, params=params, headers=API_HEADERS, timeout=30)
        body = r.text or ""
        if r.status_code in (401, 403):
            data = None
            try:
                data = r.json()
            except Exception:
                pass
            denied = r.status_code == 401 or (isinstance(data, dict) and (
                data.get("code") in (100, 106) or data.get("message_code") in ("access_denied", "invalid_authentication_token")))
            if denied:
                if _retry and self.logged_in and self.refresh():
                    return self._api(path, params, _retry=False)
                raise AuthError(str(r.status_code))
            if _retry and not self._warmed:
                self._warm()
                return self._api(path, params, _retry=False)
            raise BlockedError(str(r.status_code))
        if r.status_code == 429:
            raise BlockedError("429")
        if r.status_code == 404:
            raise NotFound(path)
        if r.status_code >= 400:
            raise VintedError(f"HTTP {r.status_code}")
        try:
            return r.json()
        except Exception as exc:
            if _looks_blocked(body):
                raise BlockedError("resposta não-JSON") from exc
            raise VintedError("resposta não-JSON") from exc

    def _html(self, path: str, params: dict | None = None, _retry: bool = True) -> str:
        url = self.base + path + ("?" + urlencode(params, doseq=True) if params else "")
        r = self.s.get(url, headers=HTML_HEADERS, timeout=40)
        text = r.text or ""
        if r.status_code in (403, 429) or (r.status_code == 200 and _looks_blocked(text)):
            if _retry and not self._warmed:
                self._warm()
                return self._html(path, params, _retry=False)
            raise BlockedError(str(r.status_code))
        if r.status_code == 404:
            raise NotFound(path)
        if r.status_code >= 400:
            raise VintedError(f"HTTP {r.status_code}")
        return text

    def _warm(self) -> None:
        self._warmed = True
        try:
            self.s.get(self.base + "/", headers=HTML_HEADERS, timeout=30)
            time.sleep(1.5)
        except Exception:
            pass

    def refresh(self) -> bool:
        try:
            r = self.s.post(self.base + REFRESH_PATH, headers=API_HEADERS, timeout=30)
            return r.status_code < 300
        except Exception:
            return False

    # ---------- dados ----------
    def current_user_id(self) -> str:
        data = self._api(CURRENT_USER_PATH)
        user = data.get("user") or data
        if not user.get("id"):
            raise AuthError("sem utilizador")
        return str(user["id"])

    def favourites(self, user_id: str, max_pages: int = 10) -> list[dict]:
        items: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._api(FAVOURITES_PATH.format(uid=user_id), {"page": page, "per_page": 96})
            batch = data.get("items") or data.get("favourite_items") or data.get("favourites") or []
            items.extend(normalize_item(r, self.base) for r in batch if r.get("id"))
            total = (data.get("pagination") or {}).get("total_pages") or 1
            if not batch or page >= total:
                break
            time.sleep(1.0)
        return items

    def search(self, text: str, order: str = "new", filters: dict | None = None) -> list[dict]:
        """filters: filtros da Vinted, ex. {"size_ids[]": [208], "brand_ids[]": [3799]}."""
        params = [("search_text", parse_query(text)["text"] or text), ("order", ORDERS.get(order, order))]
        for k, vals in (filters or {}).items():
            for v in (vals if isinstance(vals, list) else [vals]):
                params.append((k, v))
        items = parse_search_page(self._html("/catalog", params), self.base)
        if not items:
            raise VintedError("a página de pesquisa não trouxe artigos (formato mudou?)")
        return items

    def search_url(self, text: str, order: str = "new") -> str:
        return f"{self.base}/catalog?" + urlencode({"search_text": text, "order": ORDERS.get(order, order)})

    def item(self, item_id: str) -> dict:
        return parse_item_page(self._html(f"/items/{item_id}"), self.base, str(item_id))

    def cookie_header(self) -> str:
        jar = getattr(self.s.cookies, "jar", self.s.cookies)
        vals = {}
        for c in jar:
            name = getattr(c, "name", None)
            if name:
                vals[name] = c.value
        return "; ".join(f"{k}={v}" for k, v in vals.items())


def search_url(domain: str, text: str, order: str = "new", filters: dict | None = None) -> str:
    q = parse_query(text)["text"] or text
    params = [("search_text", q), ("order", ORDERS.get(order, order))]
    for k, vals in (filters or {}).items():
        params += [(k, v) for v in (vals if isinstance(vals, list) else [vals])]
    return f"https://{domain}/catalog?" + urlencode(params)


def search_key(q: str, filters: dict | None = None) -> str:
    """Identifica uma pesquisa (texto + filtros) para não a seguir duas vezes."""
    f = ";".join(f"{k}={','.join(str(x) for x in sorted(v if isinstance(v, list) else [v]))}"
                 for k, v in sorted((filters or {}).items()))
    return norm(q) + ("|" + f if f else "")


# ---------------------------------------------------------------- filtro de relevância
# A pesquisa da Vinted é muito "solta" (devolve outras marcas) e não entende "tamanho M"
# como filtro. Aqui: tira o tamanho do texto, exige a marca pedida e filtra pelo tamanho.
_SIZE_RE = re.compile(r"\b(?:tamanho|tam|talla|taille|size|tg)\s*[.:]?\s*([a-z0-9]{1,5})\b", re.I)
_STOP = {"de", "da", "do", "das", "dos", "para", "com", "e", "a", "o", "em", "the", "for", "y", "et"}


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def parse_query(q: str) -> dict:
    size = None
    m = _SIZE_RE.search(q or "")
    if m:
        size = m.group(1)
        q = (q[: m.start()] + " " + q[m.end():])
    text = " ".join((q or "").split())
    words = [w for w in norm(text).split() if len(w) >= 2 and w not in _STOP]
    return {"text": text, "size": size, "words": words}


def _has_word(hay: str, w: str) -> bool:
    return re.search(rf"\b{re.escape(w)}", hay) is not None


def _size_ok(it: dict, size: str) -> bool:
    sz = norm(size)
    s = norm(it.get("size"))
    if s:
        return s == sz or sz in s.split()
    t = norm(it.get("title"))
    return re.search(rf"\b(?:tamanho|tam|talla|taille|size|tg)\s*{re.escape(sz)}\b", t) is not None


def refine(items: list[dict], query: str) -> tuple[list[dict], dict]:
    """Devolve (artigos filtrados, info) — info diz que filtros foram aplicados."""
    pq = parse_query(query)
    brands = {norm(i.get("brand")) for i in items if i.get("brand")}
    brand_words = [w for w in pq["words"] if any(w == b or w in b.split() for b in brands)]
    out = items
    if brand_words:
        out = [i for i in out if all(_has_word(norm(i.get("brand")), w) or _has_word(norm(i.get("title")), w)
                                     for w in brand_words)]
    if pq["size"]:
        out = [i for i in out if _size_ok(i, pq["size"])]
    return out, {"brands": brand_words, "size": pq["size"], "total": len(items), "kept": len(out)}


_ITEM_ID_RE = re.compile(r"/items/(\d+)")


def item_id_from_text(text: str) -> str | None:
    m = _ITEM_ID_RE.search(text or "")
    return m.group(1) if m else None
