"""Independent fixtures: prices are deliberately not ordered by cost."""
from datetime import datetime, timedelta, timezone

CID = 5631
NAME_JA = "拡散する波動"
NAME_KO = "확산하는 파동"


def detail_html(cid=CID, locale="ja", name=NAME_JA, prints=None):
    if prints is None:
        prints = [(3315001, "15AY-JPB22", "N", 1),
                  (2201013, "SD6-JP030", "N", 1),
                  (3311000, "EE1-JP162", "UR", 4)]
    rows = "".join(
        f'<div class="t_row"><span class="time">2014-07-05</span>'
        f'<span class="card_number">{number}</span>'
        f'<input class="link_value" value="/yugiohdb/card_search.action?ope=1&sess=1&pid={pid}&rp=99999">'
        f'<div class="icon rarity"><div class="lr_icon rid_{rid}"><p>{code}</p>'
        f'<span>ノーマル仕様</span></div></div></div>'
        for pid, number, code, rid in prints
    )
    return f'''<html><head>
      <meta property="og:url" content="https://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=2&amp;cid={cid}&amp;request_locale={locale}">
      <meta property="og:locale" content="{'ja_JP' if locale == 'ja' else 'ko_KR'}">
      </head><body><script>
      $('#card_image_1').attr('src', '/yugiohdb/get_image.action?type=2&cid={cid}&ciid=1&enc=TEST').show();
      </script><div id="cardname"><h1>{name}</h1></div>
      <div id="update_list"><div class="t_body">{rows}</div></div></body></html>'''


def product(product_id="39577", number="15AY-JPB22", price=240,
            stock="in_stock", rarity="Normal", locale="ja", observed_at=None):
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    return {"product_id": product_id, "name": NAME_KO, "card_number": number,
            "locale": locale, "rarity_label": rarity, "price_krw": price,
            "stock_status": stock, "stock_evidence": "fixture stock evidence",
            "product_url": f"http://www.tcgshop.co.kr/goods_detail.php?goodsIdx={product_id}",
            "observed_at": observed_at}


def group(number, products):
    expires_at = min(datetime.fromisoformat(p["observed_at"]) for p in products) + timedelta(hours=12)
    return {"card_number": number, "locale": "ja", "scope": "observed_products",
            "prices": products, "expires_at": expires_at.isoformat(), "cache_status": "miss"}


def capture_downloads(files):
    """Execute the real deferred download callback as a click, then render its bytes for AppTest."""
    from streamlit.delta_generator import DeltaGenerator
    original = DeltaGenerator.download_button

    def download(container, label, data, *args, **kwargs):
        if callable(data):
            data = data()
        if not kwargs.get("disabled", False):
            files["csv" if kwargs.get("file_name", "").endswith(".csv") else "xlsx"] = data
        return original(container, label, data, *args, **kwargs)
    return download
