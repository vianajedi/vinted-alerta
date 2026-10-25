"""Páginas de teste com a MESMA estrutura das páginas reais da Vinted (capturadas a 25-10-2026).

Os valores são inventados; a forma (chaves, aninhamento, codificação RSC) é a real.
"""
import json


def _push(payload: str) -> str:
    # o HTML real traz um <script>self.__next_f.push([1,"…"])</script> com o texto escapado como string JS/JSON
    return "<script>self.__next_f.push([1," + json.dumps(payload) + "])</script>"


def product(iid, title, likes, price, brand="Levi's", second="M · Muito bom"):
    img = f"https://images1.vinted.net/t/xx/310x430/{iid}.webp?s=abc"
    return {
        "id": iid,
        "productItem": {
            "id": iid, "title": title, "url": f"/items/{iid}-slug", "favouriteCount": likes,
            "priceWithDiscount": None, "price": {"amount": f"{price:.2f}", "currencyCode": "EUR"},
            "serviceFee": {"amount": "1.45", "currencyCode": "EUR"},
            "totalItemPrice": {"amount": f"{price + 1.45:.2f}", "currencyCode": "EUR"},
            "isPromoted": False, "isFavourite": False, "thumbnailUrl": img, "thumbnailUrls": [img],
            "dominantColor": "#CACED3", "user": {"id": 1, "photo": None, "thumbnailUrl": None, "isBusiness": False},
            "photos": [{"url": img.replace("310x430", "f800"), "thumbnails": [], "isMain": True, "imageNo": 1}],
            "itemBox": {"firstLine": brand, "secondLine": second, "exposure": "$undefined", "exposures": [],
                        "ffExposures": [], "accessibilityLabel": title, "badge": "$undefined",
                        "actions": "$undefined", "itemId": str(iid)},
        },
        "catalogTracking": {"contentSource": "search", "ownerId": 1, "searchScore": 1.0, "searchSignals": "$undefined"},
    }


def search_page(products: list[dict]) -> str:
    payload = (
        '1:"$Sreact.fragment"\n2:"https://marketplace-web-assets.vinted.com/_next/static/chunks/x.js"\n'
        '3:I[676088,["$2"],"default"]\n'
        '5:["$","$L4",null,{"general":{"filtersChangeCount":1,"title":"$undefined"},"items":{"items":'
        + json.dumps(products, ensure_ascii=False, separators=(',', ':')) + ',"uiState":"SUCCESS"}}]\n'
    )
    return "<!DOCTYPE html><html lang=\"pt\"><head><title>Artigos | Vinted</title></head><body>" \
           "<div class=\"feed-grid\">…</div>" + _push(payload) + "</body></html>"


def item_page(iid, title, likes, price, availability="InStock", reserved=False, can_buy=True):
    ld = {"@type": "Product", "name": title, "description": "…",
          "image": f"https://images1.vinted.net/t/xx/f800/{iid}.webp?s=1",
          "brand": {"@type": "Brand", "name": "Levi's"},
          "offers": {"@type": "Offer", "url": f"https://www.vinted.pt/items/{iid}-slug", "priceCurrency": "EUR",
                     "price": price, "availability": availability, "itemCondition": "UsedCondition"},
          "category": "Homem Blusões de ganga", "@context": "https://schema.org"}
    payload = (
        '1:"$Sreact.fragment"\n'
        '103:["$","$Lff",null,{"data":{"data":' + json.dumps(separators=(',', ':'), obj=
            {"favourite_count": likes, "is_favourite": False, "item_id": str(iid), "seller_id": "1"}) +
        ',"exposures":[],"name":"favourite","section":"sidebar","type":"favourite"}}]\n'
        '104:["$","$L10",null,{"plugins":[{"data":' + json.dumps(separators=(',', ':'), obj=
            {"can_buy": can_buy, "instant_buy": True, "is_hidden": False, "is_reserved": reserved,
             "item_id": str(iid), "reservation": None, "seller_id": "1"}) +
        ',"exposures":[],"name":"ask_seller","section":"sidebar","type":"buttons"}]}]\n'
        # outro artigo na mesma página (recomendações) não deve confundir o parser
        '105:{"favourite_count":999,"item_id":"1","seller_id":"2"}\n'
    )
    return ("<!DOCTYPE html><html><head><meta property=\"og:title\" content=\"x | Vinted\"/>"
            "<script type=\"application/ld+json\">" + json.dumps(ld, ensure_ascii=False) + "</script></head><body>"
            + _push(payload) + "</body></html>")


BLOCKED_PAGE = ('<html><head><title>vinted.pt</title></head><body><script>var dd={"cid":"x","hsh":"y"}</script>'
                '<script src="https://ct.captcha-delivery.com/c.js"></script></body></html>')
