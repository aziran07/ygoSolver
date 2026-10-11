"""Parsers for full listing pages of the configured Cardmoa and TCGSHOP single-card categories (no network I/O).

Each parser takes the exact response bytes and the URL they were requested from, and returns
{"products", "total_products", "total_pages", "page_number", "page_size"}. A page is accepted only when it is the
requested page of the requested category and every navigation total agrees; otherwise ShopCatalogParseError.

Products are stored as the shop lists them, without deciding which official card they are:
- card_number is the printed code without its outer parentheses, as listed, including legacy and suffixed
  codes such as "SK2-038", "SRL-K080" or "VP24-JP002(신)"; it is a source identifier, not a verified card mapping
  (raw.card_code_text keeps the original). It is omitted only when the listing prints no code.
- locale comes only from item evidence: the printed code region (-KR / -JP) and the TCGSHOP edition tag
  (한글판 / 일본판). Without evidence, or with conflicting evidence, it is None and raw.locale_evidence says why.
  The category is never used as locale evidence (Cardmoa's Korean A-grade category lists a -JP card).
- price_krw is None only when the listing shows no sale price or a price text without digits (e.g. an inquiry
  note), with raw.price_error. A price text with digits that is not exactly "1,234" fails the page.
  list_price_krw is the struck-through original price, or None when the listing shows none.
- rarity_label is printed text only, never normalized. TCGSHOP: all .glist_03 labels joined in source order except
  labels that are exactly an edition (한글판 / 일본판 / 영문판) or an edition-and-condition grade (e.g. 한글판A급,
  일본판 A급, A급카드, B급); finish distinctions (Normal / Parallel Rare, UR OverFrame / PSC OverFrame, QC Secret
  Rare) and any unrecognized extra label (e.g. PRIDE_언컷시트) stay in it verbatim. Cardmoa: the second main_code
  text; its card_type labels are condition/edition fields, kept in raw (raw.unrecognized_label_texts lists those
  not matching the edition/condition pattern, e.g. 일판스페셜에디션). raw.rarity_label_texts and
  raw.edition_condition_texts record the split.
- All source texts (codes, rarity, condition labels, price and discount texts, stock evidence) are kept in raw.

Encoding: both shops send EUC-KR text, decoded as CP949, the superset of EUC-KR that Korean sites emit (it adds
the Hangul syllables EUC-KR lacks). TCGSHOP pages must decode strictly as a whole. Every Cardmoa page carries UTF-8
literals in its shared menu and scripts (e.g. title="... 전체카드보기") before the product list, so Cardmoa pages
are decoded with undecodable bytes kept as escapes, and the parser requires that the elements it reads (total
text, product tables, paginator) contain none: any undecodable byte in product data fails the page.
"""

import math
import re

from bs4 import BeautifulSoup, Comment

import prices
from shop_catalog_store import ShopCatalogError

# (shop, category) -> language named by the category label. It describes the run, never a product.
CATEGORIES = {
    ("cardmoa", "101010"): "ko",  # 싱글카드(한)
    ("cardmoa", "102010"): "ja",  # 싱글카드(일)
    ("cardmoa", "101015"): "ko",  # 한글판A급싱글카드
    ("cardmoa", "102015"): "ja",  # 일본판A급싱글카드
    ("tcgshop", "276"): "ko",  # 싱글카드(한글)
    ("tcgshop", "288"): "ja",  # 싱글카드(일본)
    ("tcgshop", "491"): None,  # 지못미_A급카드, Korean and Japanese mixed
}
NAV_BLOCK_PAGES = 10
ENCODING = "cp949"
PRICE_TEXT = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})*)원")
UNDECODED = re.compile("[\udc80-\udcff]")
ASCII_NUMBER = re.compile("[0-9]+")
CODE_REGION = re.compile(r"[0-9A-Z]+-(KR|JP|EN)")
# Printed language -> locale; "en" is evidence of an English edition, which is stored as locale None.
REGION_LOCALES = {"KR": "ko", "JP": "ja", "EN": "en"}
EDITION_LOCALES = {"한글판": "ko", "일본판": "ja", "영문판": "en"}
# A label that only names an edition and/or a condition grade; it never becomes part of rarity_label.
EDITION_CONDITION_LABEL = re.compile(r"(한글판|일본판|영문판)|(?:(한글판|일본판|영문판) ?)?[AB]급(?:카드)?")

CARDMOA_LIST_URL = "http://cardmoa.com/shop/list.php?ca_id={category}"
CARDMOA_PAGE_URL = "http://cardmoa.com/shop/list.php?ca_id={category}&skin=&ev_id=&sort=&page={page}"
CARDMOA_URL = re.compile(r"http://cardmoa\.com/shop/list\.php\?ca_id=([0-9a-z]+)(?:&skin=&ev_id=&sort=&page=([0-9]+))?")
CARDMOA_ITEM_URL = "http://cardmoa.com/shop/item.php?it_id={product_id}"
CARDMOA_ITEM_LINKS = ("../shop/item.php?it_id={product_id}", "http://cardmoa.com/shop/item.php?it_id={product_id}")
CARDMOA_TOTAL = re.compile(r"총 ([0-9]{1,3}(?:,[0-9]{3})*) ?개의 상품이 있습니다\.")
CARDMOA_NAV_LINK = re.compile(r"/shop/list\.php\?ca_id=([0-9a-z]+)&skin=&ev_id=&sort=&page=([0-9]+)")
CARDMOA_FORM = re.compile(r"fitem([0-9]+)")

TCGSHOP_PAGE_SIZE = 56
TCGSHOP_LIST_URL = prices.LIST_URL_PREFIX + "?Index={category}"
TCGSHOP_PAGE_URL = (prices.LIST_URL_PREFIX + "?data=offset%3D{offset}%26letter_no%3D{letter_no}%26pagecnt%3D{block}"
                    "%26search%3D%26searchstring%3D&Index={category}&sort=3&searchstring=")
TCGSHOP_FIRST_URL = re.compile(re.escape(prices.LIST_URL_PREFIX) + r"\?Index=([0-9]+)")
TCGSHOP_LATER_URL = re.compile(
    r"(?:" + re.escape(prices.LIST_URL_PREFIX) + r"|goods_list\.php)\?data=offset%3D([0-9]+)%26letter_no%3D([0-9]+)"
    r"%26pagecnt%3D([0-9]+)%26search%3D%26searchstring%3D&Index=([0-9]+)&sort=3&searchstring=")
TCGSHOP_BLOCK_URL = re.compile(r"goods_list\.php\?data=pagecnt%3D([0-9]+)%26letter_no%3D([0-9]+)"
                               r"%26search%3D%26searchstring%3D&Index=([0-9]+)&sort=3&searchstring=")
TCGSHOP_ITEM_URL = "http://www.tcgshop.co.kr/goods_detail.php?goodsIdx={product_id}"
TCGSHOP_LIST_START = "상품 목록 시작"  # HTML comments around the product blocks
TCGSHOP_LIST_END = "상품 목록 끝"
TCGSHOP_SORT = re.compile(r"javascript:goodsSort\(([0-9])\);")


class ShopCatalogParseError(ShopCatalogError):
    """The page is not exactly the requested listing page, or its structure is not the verified one."""


def page_count(total_products, page_size):
    return max(1, math.ceil(total_products / page_size))


def page_url(shop, category, page_number=1, total_products=None):
    """Listing URL of one page. TCGSHOP pages after the first need the category total (their links carry it)."""
    if (shop, category) not in CATEGORIES:
        raise ValueError(f"unconfigured catalog category {shop!r} {category!r}")
    if type(page_number) is not int or page_number < 1:
        raise ValueError(f"page_number must be a positive int, got {page_number!r}")
    if page_number == 1:
        return (CARDMOA_LIST_URL if shop == "cardmoa" else TCGSHOP_LIST_URL).format(category=category)
    if shop == "cardmoa":
        return CARDMOA_PAGE_URL.format(category=category, page=page_number)
    if type(total_products) is not int or total_products < 0:
        raise ValueError(f"TCGSHOP page {page_number} needs total_products, got {total_products!r}")
    if page_number > page_count(total_products, TCGSHOP_PAGE_SIZE):
        raise ValueError(f"TCGSHOP page {page_number} is beyond {total_products} products")
    offset = TCGSHOP_PAGE_SIZE * (page_number - 1)
    return TCGSHOP_PAGE_URL.format(offset=offset, letter_no=total_products - offset,
                                   block=(page_number - 1) // NAV_BLOCK_PAGES, category=category)


def text_of(element):
    return " ".join(element.get_text(" ").split())


def price_value(text):
    match = PRICE_TEXT.fullmatch(text.replace(" ", ""))
    return int(match.group(1).replace(",", "")) if match else None


def sale_price(text, url, product_id):
    """(price, error): a won amount, or None with a reason for a text without digits; malformed numbers fail."""
    price = price_value(text)
    if price is not None:
        return price, None
    if re.search("[0-9]", text):
        raise ShopCatalogParseError(f"{url}: product {product_id} sale price {text!r} is not a won amount")
    return None, f"listing shows no numeric sale price: {text!r}"


def split_labels(labels):
    """(rarity parts, edition/condition labels) of nonempty printed labels, both in source order."""
    rarity_parts, edition_condition = [], []
    for label in labels:
        if label:
            (edition_condition if EDITION_CONDITION_LABEL.fullmatch(label) else rarity_parts).append(label)
    return rarity_parts, edition_condition


def item_locale(code_inner, edition_labels):
    """(locale, evidence) from the printed code region and edition/condition labels only."""
    evidence = []
    locales = set()
    region = CODE_REGION.match(code_inner) if code_inner else None
    if region:
        locales.add(REGION_LOCALES[region.group(1)])
        evidence.append(f"printed code {code_inner!r} region {region.group(1)}")
    for label in edition_labels:
        match = EDITION_CONDITION_LABEL.fullmatch(label)
        edition = match.group(1) or match.group(2)
        if edition:
            locales.add(EDITION_LOCALES[edition])
            evidence.append(f"edition label {label!r}")
    if locales == {"en"}:
        return None, "English edition evidence (only ko/ja are stored as locale): " + "; ".join(evidence)
    if len(locales) == 1:
        return locales.pop(), "; ".join(evidence)
    if locales:
        return None, "conflicting item evidence: " + "; ".join(evidence)
    printed = f"printed code {code_inner!r} has no -KR/-JP/-EN region" if code_inner else "no printed card code"
    return None, f"no item locale evidence: {printed} and no edition label; the category is not item evidence"


def printed_code(code_text, url, product_id):
    """The printed "(CODE)" text without its outer parentheses, or None when no code is printed."""
    if code_text is None:
        return None
    if not (code_text.startswith("(") and code_text.endswith(")")) or len(code_text) < 3:
        raise ShopCatalogParseError(f"{url}: product {product_id} code {code_text!r} is not parenthesized")
    return code_text[1:-1]


def build_product(product_id, name, product_url, locale, price, list_price, stock_status, card_number, rarity,
                  raw):
    product = {"product_id": product_id, "name": name, "product_url": product_url, "locale": locale,
               "price_krw": price, "list_price_krw": list_price, "stock_status": stock_status, "raw": raw}
    if card_number is not None:
        product["card_number"] = card_number
    if rarity:
        product["rarity_label"] = rarity
    return product


def check_item_count(url, page_number, total_products, total_pages, page_size, item_count):
    if page_number > total_pages:
        raise ShopCatalogParseError(f"{url}: page {page_number} is beyond {total_pages} pages")
    expected = page_size if page_number < total_pages else total_products - page_size * (total_pages - 1)
    if item_count != expected:
        raise ShopCatalogParseError(f"{url}: {item_count} products listed, expected {expected} "
                                    f"(page {page_number}/{total_pages}, {total_products} products, size {page_size})")


def check_decoded(element, what, url):
    position = UNDECODED.search(str(element))
    if position:
        raise ShopCatalogParseError(f"{url}: {what} contains bytes that are not {ENCODING} "
                                    f"near {str(element)[max(0, position.start() - 40):position.start()]!r}")


def nav_block(page_number, total_pages):
    first = (page_number - 1) // NAV_BLOCK_PAGES * NAV_BLOCK_PAGES + 1
    return first, min(first + NAV_BLOCK_PAGES - 1, total_pages)


# ---------------------------------------------------------------- Cardmoa


def cardmoa_expected_nav(page_number, total_pages):
    """Cardmoa (gnuboard get_paging): 처음 on any page after the first, 이전/다음 around the 10-page block,
    맨끝 on any page before the last."""
    first, last = nav_block(page_number, total_pages)
    tokens = []
    if page_number > 1:
        tokens.append(("link", 1, "처음"))
    if first > 1:
        tokens.append(("link", first - 1, "이전"))
    for page in range(first, last + 1):
        tokens.append(("current", str(page)) if page == page_number else ("link", page, str(page)))
    if last < total_pages:
        tokens.append(("link", last + 1, "다음"))
    if page_number < total_pages:
        tokens.append(("link", total_pages, "맨끝"))
    return tokens


def cardmoa_nav(nav, category, url):
    tokens = []
    for element in nav.find_all(["a", "b"]):
        if element.name == "b":
            tokens.append(("current", text_of(element)))
            continue
        match = CARDMOA_NAV_LINK.fullmatch(element.get("href", ""))
        if match is None or match.group(1) != category:
            raise ShopCatalogParseError(f"{url}: unexpected paginator link {element.get('href')!r}")
        tokens.append(("link", int(match.group(2)), text_of(element)))
    return tokens


def cardmoa_product(table, form, category, url):
    form_match = CARDMOA_FORM.fullmatch(form.get("name", ""))
    if form_match is None:
        raise ShopCatalogParseError(f"{url}: unexpected product form {form.get('name')!r}")
    form_id = form_match.group(1)
    if len(table.select("form")) != 1:
        raise ShopCatalogParseError(f"{url}: product {form_id} table holds {len(table.select('form'))} forms")

    def hidden(name):
        fields = form.select(f'input[name="{name}"]')
        if len(fields) != 1:
            raise ShopCatalogParseError(f"{url}: product {form_id} has {len(fields)} hidden {name} fields")
        return fields[0].get("value", "")

    product_id = hidden("it_id")
    if product_id != form_id or not ASCII_NUMBER.fullmatch(product_id):
        raise ShopCatalogParseError(f"{url}: form {form_id} carries it_id {product_id!r}")
    hidden_name = hidden("it_name")
    hidden_amount = hidden("it_amount")
    if not ASCII_NUMBER.fullmatch(hidden_amount):
        raise ShopCatalogParseError(f"{url}: product {product_id} hidden it_amount {hidden_amount!r} is not a number")

    allowed_links = {link.format(product_id=product_id) for link in CARDMOA_ITEM_LINKS}
    visible_names = []
    for link in table.select("a[href]"):
        if "it_id=" not in link["href"]:
            continue
        if link["href"] not in allowed_links:
            raise ShopCatalogParseError(f"{url}: product {product_id} links to {link['href']!r}")
        if text_of(link):
            visible_names.append(text_of(link))
    if len(visible_names) != 1:
        raise ShopCatalogParseError(f"{url}: product {product_id} shows {len(visible_names)} linked names")

    # main_code texts: (CODE) then rarity. Non-card listings (e.g. card cases) print none; their code and rarity
    # stay absent and raw.identity_evidence says so. Three or more texts are ambiguous and fail the page.
    codes = [text_of(font) for font in table.select("font.main_code")]
    if len(codes) > 2:
        raise ShopCatalogParseError(f"{url}: product {product_id} has {len(codes)} main_code texts {codes}")
    code_text = codes[0] if codes else None
    rarity_text = codes[1] if len(codes) == 2 else None
    card_number = printed_code(code_text, url, product_id)
    card_types = [text_of(font) for font in table.select("font.card_type")]
    card_types2 = [text_of(font) for font in table.select("font.card_type2")]
    # Cardmoa prints the rarity only as the second main_code. card_type/card_type2 hold condition and edition labels
    # (A급카드, B급, 일판스페셜에디션), never part of the rarity; labels not matching the known edition/condition
    # pattern are listed in raw.unrecognized_label_texts instead of being interpreted.
    rarity_parts = [rarity_text] if rarity_text else []
    rarity = rarity_text
    type_labels = [label for label in [*card_types, *card_types2] if label]
    edition_condition = [label for label in type_labels if EDITION_CONDITION_LABEL.fullmatch(label)]
    unrecognized = [label for label in type_labels if not EDITION_CONDITION_LABEL.fullmatch(label)]
    locale, locale_evidence = item_locale(card_number, edition_condition)

    raw = {"shop": "cardmoa", "category": category, "locale_evidence": locale_evidence,
           "it_name": hidden_name, "visible_name": visible_names[0], "main_code_texts": codes,
           "card_code_text": code_text, "rarity_text": rarity_text, "card_type_texts": card_types,
           "card_type2_texts": card_types2, "rarity_label_texts": rarity_parts,
           "edition_condition_texts": edition_condition, "unrecognized_label_texts": unrecognized,
           "condition_evidence": [text for text in [code_text, *card_types, *card_types2] if text and "급" in text],
           "hidden_it_amount": hidden_amount}
    if not codes:
        raw["identity_evidence"] = "listing prints no main_code: no card code and no rarity"

    amounts = table.select("span.amount")
    if len(amounts) > 1:
        raise ShopCatalogParseError(f"{url}: product {product_id} shows {len(amounts)} sale prices")
    raw["price_text"] = text_of(amounts[0]) if amounts else None
    price = None
    if not amounts:
        raw["price_error"] = f"listing shows no sale price (span.amount); hidden it_amount={hidden_amount}"
    else:
        price, price_error = sale_price(raw["price_text"], url, product_id)
        if price is None:
            raw["price_error"] = f"{price_error}; hidden it_amount={hidden_amount}"
        elif price != int(hidden_amount):
            raise ShopCatalogParseError(f"{url}: product {product_id} visible price {raw['price_text']!r} differs "
                                        f"from hidden it_amount {hidden_amount}")
        elif price == 0:
            raw["price_zero_evidence"] = f"visible {raw['price_text']!r} and hidden it_amount=0"

    strikes = table.find_all("strike")
    if len(strikes) > 1:
        raise ShopCatalogParseError(f"{url}: product {product_id} shows {len(strikes)} struck prices")
    raw["list_price_text"] = text_of(strikes[0]) if strikes else None
    list_price = None
    if strikes:
        list_price = price_value(raw["list_price_text"])
        if list_price is None:
            raise ShopCatalogParseError(f"{url}: product {product_id} struck price {raw['list_price_text']!r} "
                                        "is not a won amount")
        if list_price == 0:
            raw["price_zero_evidence"] = f"struck price {raw['list_price_text']!r}"
    raw["discount_texts"] = [text_of(font) for font in table.select("font.sale_per")]

    sold_out = bool(table.find_all(style=re.compile("sold_out_bg")))
    carts = [image for image in table.find_all("img") if image.get("src", "").endswith("/go_cart.gif")]
    if len(carts) > 1:
        raise ShopCatalogParseError(f"{url}: product {product_id} has {len(carts)} cart buttons")
    cart_click = carts[0].get("onclick", "") if carts else None
    if carts and cart_click != f"fitemcheck(document.fitem{product_id}, 'cart_update');":
        raise ShopCatalogParseError(f"{url}: product {product_id} cart button runs {cart_click!r}")
    if sold_out and carts:
        raise ShopCatalogParseError(f"{url}: product {product_id} shows the sold-out overlay and a cart button")
    if sold_out:
        stock_status, raw["stock_evidence"] = "out_of_stock", "sold_out_bg.png overlay, no go_cart.gif"
    elif carts:
        stock_status, raw["stock_evidence"] = "in_stock", f"go_cart.gif onclick={cart_click!r}"
    else:
        stock_status, raw["stock_evidence"] = "unknown", "no sold_out_bg.png overlay and no go_cart.gif"

    # The displayed name is the product name; the hidden cart field it_name stays evidence in raw.
    return build_product(product_id, visible_names[0], CARDMOA_ITEM_URL.format(product_id=product_id), locale, price,
                         list_price, stock_status, card_number, rarity, raw)


def parse_cardmoa_page(body, source_url, page_size=None):
    """One Cardmoa list page. page_size: None for page 1 (derived from its item count); required afterwards."""
    url_match = CARDMOA_URL.fullmatch(source_url) if isinstance(source_url, str) else None
    if url_match is None or ("cardmoa", url_match.group(1)) not in CATEGORIES:
        raise ShopCatalogParseError(f"not a configured Cardmoa list URL: {source_url!r}")
    category = url_match.group(1)
    page_number = int(url_match.group(2) or 1)
    if page_url("cardmoa", category, page_number) != source_url:
        raise ShopCatalogParseError(f"{source_url}: not the canonical URL of page {page_number}")
    if page_number > 1 and (type(page_size) is not int or page_size < 1):
        raise ShopCatalogParseError(f"{source_url}: page {page_number} needs the page size of page 1")
    if not isinstance(body, bytes) or not body:
        raise ShopCatalogParseError(f"{source_url}: body must be nonempty bytes")

    # Undecodable bytes (the UTF-8 menu literals) become lone surrogates; every element read below is then
    # required to contain none, so the product data itself is decoded strictly.
    soup = BeautifulSoup(body.decode(ENCODING, errors="surrogateescape"), "html.parser")
    total_cells = [cell for cell in soup.find_all("td")
                   if "개의 상품이 있습니다" in cell.get_text() and cell.find("td") is None]
    if len(total_cells) != 1:
        raise ShopCatalogParseError(f"{source_url}: expected one total product text, found {len(total_cells)}")
    check_decoded(total_cells[0], "total product text", source_url)
    total_match = CARDMOA_TOTAL.fullmatch(text_of(total_cells[0]))
    if total_match is None:
        raise ShopCatalogParseError(f"{source_url}: total product text {text_of(total_cells[0])!r}")
    total_products = int(total_match.group(1).replace(",", ""))

    categories = [field.get("value") for form in soup.select('form[name="frmsearch"]')
                  for field in form.select('input[name="ca_id"]')]
    if categories != [category]:
        raise ShopCatalogParseError(f"{source_url}: page category {categories} is not {category}")

    products = []
    for form in soup.select("form[name^=fitem]"):
        table = form.find_parent("table")
        if table is None:
            raise ShopCatalogParseError(f"{source_url}: product form {form.get('name')!r} is not in a table")
        check_decoded(table, f"product form {form.get('name')!r}", source_url)
        products.append(cardmoa_product(table, form, category, source_url))
    ids = [product["product_id"] for product in products]
    if len(set(ids)) != len(ids):
        raise ShopCatalogParseError(f"{source_url}: duplicate product ids on the page")

    if page_number == 1:
        if page_size is not None and page_size != len(products):
            raise ShopCatalogParseError(f"{source_url}: page 1 lists {len(products)} products, not {page_size}")
        page_size = len(products)
        if total_products > 0 and page_size == 0:
            raise ShopCatalogParseError(f"{source_url}: no products listed but the total is {total_products}")
    total_pages = page_count(total_products, page_size) if page_size else 1
    check_item_count(source_url, page_number, total_products, total_pages, page_size, len(products))

    navs = [div for div in soup.find_all("div", align="center") if div.get("style") == "clear:both;"]
    if len(navs) != 1:
        raise ShopCatalogParseError(f"{source_url}: expected one paginator, found {len(navs)}")
    check_decoded(navs[0], "paginator", source_url)
    found = cardmoa_nav(navs[0], category, source_url)
    expected = cardmoa_expected_nav(page_number, total_pages)
    if found != expected:
        raise ShopCatalogParseError(f"{source_url}: paginator {found} does not match page {page_number} of "
                                    f"{total_pages} ({total_products} products, {page_size} per page)")
    return {"products": products, "total_products": total_products, "total_pages": total_pages,
            "page_number": page_number, "page_size": page_size}


# ---------------------------------------------------------------- TCGSHOP


def tcgshop_expected_nav(page_number, total_products, total_pages):
    first, last = nav_block(page_number, total_pages)
    block = (page_number - 1) // NAV_BLOCK_PAGES
    block_products = TCGSHOP_PAGE_SIZE * NAV_BLOCK_PAGES
    tokens = []
    if block > 0:
        tokens.append(("prev", block - 1, total_products - (block - 1) * block_products))
    for page in range(first, last + 1):
        offset = TCGSHOP_PAGE_SIZE * (page - 1)
        tokens.append(("current", page) if page == page_number
                      else ("page", page, offset, total_products - offset, block))
    if last < total_pages:
        tokens.append(("next", block + 1, total_products - (block + 1) * block_products))
    return tokens


def tcgshop_nav(soup, category, url):
    """Paginator tokens in order; any cell other than a page, block link, current page or spacer fails."""
    list_ends = soup.find_all(string=lambda text: isinstance(text, Comment) and text.strip() == TCGSHOP_LIST_END)
    nav = list_ends[0].find_next("table", id="nom") if len(list_ends) == 1 else None
    if nav is None:
        raise ShopCatalogParseError(f"{url}: no paginator after the product list")
    tokens = []
    for cell in nav.find_all("td"):
        links = cell.select("a[href]")
        onclick = cell.get("onclick")
        fonts = cell.select("font")
        if links:
            images = [image.get("src", "").rsplit("/", 1)[-1] for image in cell.select("img")]
            match = TCGSHOP_BLOCK_URL.fullmatch(links[0]["href"])
            if len(links) != 1 or images not in (["prev.gif"], ["next.gif"]) or match is None \
                    or match.group(3) != category:
                raise ShopCatalogParseError(f"{url}: unexpected paginator link {links[0]['href']!r} {images}")
            tokens.append((images[0][:-4], int(match.group(1)), int(match.group(2))))
        elif onclick:
            target = re.fullmatch(r"location\.href='([^']*)'", onclick)
            match = TCGSHOP_LATER_URL.fullmatch(target.group(1)) if target else None
            if match is None or match.group(4) != category or not ASCII_NUMBER.fullmatch(text_of(cell)):
                raise ShopCatalogParseError(f"{url}: unexpected paginator cell {onclick!r} {text_of(cell)!r}")
            offset, letter_no, block = (int(value) for value in match.groups()[:3])
            tokens.append(("page", int(text_of(cell)), offset, letter_no, block))
        elif fonts:
            if not ASCII_NUMBER.fullmatch(text_of(cell)):
                raise ShopCatalogParseError(f"{url}: current page marker {text_of(cell)!r}")
            tokens.append(("current", int(text_of(cell))))
        elif text_of(cell):
            raise ShopCatalogParseError(f"{url}: unexpected paginator text {text_of(cell)!r}")
    return tokens


def tcgshop_product(block, category, url):
    product_id = re.fullmatch(r"list_card_([0-9]+)", block["id"])
    if product_id is None:
        raise ShopCatalogParseError(f"{url}: unrecognized product block id {block['id']!r}")
    product_id = product_id.group(1)
    if block.select("table[id^=list_card_]"):
        raise ShopCatalogParseError(f"{url}: product {product_id} has a nested product block")
    detail_href = f"goods_detail.php?goodsIdx={product_id}"
    for link in block.select("a[href]"):
        if "goodsIdx=" in link["href"] and link["href"] != detail_href:
            raise ShopCatalogParseError(f"{url}: product {product_id} links to {link['href']!r}")
    names = block.select(".glist_01")
    if len(names) != 1 or not text_of(names[0]) \
            or [link["href"] for link in names[0].select("a[href]")] != [detail_href]:
        raise ShopCatalogParseError(f"{url}: product {product_id} has no single linked name")
    name = text_of(names[0])

    codes = [text_of(span) for span in block.select(".glist_02")]
    if len(codes) > 1:
        raise ShopCatalogParseError(f"{url}: product {product_id} has {len(codes)} card codes")
    code_text = codes[0] if codes else None
    card_number = printed_code(code_text, url, product_id)
    label_texts = [text_of(span) for span in block.select(".glist_03")]
    rarity_parts, edition_condition = split_labels(label_texts)
    rarity = " ".join(rarity_parts)
    locale, locale_evidence = item_locale(card_number, edition_condition)
    images = [image.get("src", "").rsplit("/", 1)[-1] for image in block.find_all("img")
              if "upload/goods/" not in image.get("src", "")]

    raw = {"shop": "tcgshop", "category": category, "locale_evidence": locale_evidence,
           "card_code_text": code_text, "label_texts": label_texts, "rarity_label_texts": rarity_parts,
           "edition_condition_texts": edition_condition,
           "condition_evidence": [text for text in edition_condition if "급" in text], "listing_images": images}

    price_texts = block.select(".glist_price12")
    currencies = [text_of(span) for span in block.select(".glist_price_won")]
    if len(price_texts) > 1:
        raise ShopCatalogParseError(f"{url}: product {product_id} shows {len(price_texts)} sale prices")
    raw["price_text"] = text_of(price_texts[0]) + "".join(currencies) if price_texts else None
    price = None
    if not price_texts:
        raw["price_error"] = f"listing shows no sale price (.glist_price12); listing images {images}"
    else:
        price, price_error = sale_price(raw["price_text"], url, product_id)
        if price is None:
            raw["price_error"] = price_error
        elif price == 0:
            raw["price_zero_evidence"] = f"listing shows {raw['price_text']!r}"

    original = block.select(".glist_oprice")
    if len(original) > 1:
        raise ShopCatalogParseError(f"{url}: product {product_id} shows {len(original)} original prices")
    strikes = original[0].find_all("strike") if original else []
    if original and len(strikes) != 1:
        raise ShopCatalogParseError(f"{url}: product {product_id} original price cell has {len(strikes)} strikes")
    raw["list_price_text"] = text_of(strikes[0]) if strikes else None
    list_price = None
    if strikes:
        list_price = price_value(raw["list_price_text"])
        if list_price is None:
            raise ShopCatalogParseError(f"{url}: product {product_id} struck price {raw['list_price_text']!r} "
                                        "is not a won amount")
        if list_price == 0:
            raw["price_zero_evidence"] = f"struck price {raw['list_price_text']!r}"
    raw["discount_texts"] = [text_of(span) for span in block.find_all(class_=re.compile(r"^glist_sale"))]

    # prices.parse_stock: 품절 text -> out_of_stock, active own cart button with a positive purchase limit ->
    # in_stock, anything else (e.g. discontinued items showing stop_product.gif) -> unknown; contradictions raise.
    try:
        stock_status, raw["stock_evidence"] = prices.parse_stock(block, product_id)
    except prices.PriceError as error:
        raise ShopCatalogParseError(f"{url}: {error}") from error
    if "stop_product.gif" in images:
        raw["stock_evidence"] += "; listing shows stop_product.gif"

    return build_product(product_id, name, TCGSHOP_ITEM_URL.format(product_id=product_id), locale, price,
                         list_price, stock_status, card_number, rarity, raw)


def parse_tcgshop_page(body, source_url):
    """One TCGSHOP category list page (56 products per page, newest first)."""
    first = TCGSHOP_FIRST_URL.fullmatch(source_url) if isinstance(source_url, str) else None
    later = TCGSHOP_LATER_URL.fullmatch(source_url) if isinstance(source_url, str) else None
    if later and not source_url.startswith(prices.LIST_URL_PREFIX):
        later = None
    if first:
        category, page_number, url_total = first.group(1), 1, None
    elif later:
        offset, letter_no, _, category = later.groups()
        if int(offset) % TCGSHOP_PAGE_SIZE or int(offset) == 0:
            raise ShopCatalogParseError(f"{source_url}: offset {offset} is not a later page start")
        page_number, url_total = int(offset) // TCGSHOP_PAGE_SIZE + 1, int(offset) + int(letter_no)
    else:
        raise ShopCatalogParseError(f"not a TCGSHOP list URL: {source_url!r}")
    if ("tcgshop", category) not in CATEGORIES:
        raise ShopCatalogParseError(f"{source_url}: category {category} is not configured")
    if not isinstance(body, bytes) or not body:
        raise ShopCatalogParseError(f"{source_url}: body must be nonempty bytes")
    try:
        html = body.decode(ENCODING)
    except UnicodeDecodeError as error:
        raise ShopCatalogParseError(f"{source_url}: byte {error.start} is not {ENCODING}: {error.reason}") from error

    soup = BeautifulSoup(html, "html.parser")
    forms = soup.select('form[name="sortForm"]')
    page_indexes = [field.get("value") for field in soup.select('input[name="Index"]')]
    if len(forms) != 1 or forms[0].get("action") != "goods_list.php" or page_indexes != [category]:
        raise ShopCatalogParseError(f"{source_url}: page category {page_indexes} is not Index {category}")
    selected_sorts = []
    for icon in soup.find_all("img", src=re.compile(r"/check_icon\.gif$")):
        label = icon.find_parent("span").find_next_sibling("span") if icon.find_parent("span") else None
        sort = TCGSHOP_SORT.fullmatch(label.get("onclick", "")) if label else None
        selected_sorts.append(sort.group(1) if sort else None)
    if selected_sorts != ["3"]:
        raise ShopCatalogParseError(f"{source_url}: selected sort {selected_sorts} is not 최근등록순 (sort 3)")
    starts = soup.find_all(string=lambda text: isinstance(text, Comment) and text.strip() == TCGSHOP_LIST_START)
    ends = soup.find_all(string=lambda text: isinstance(text, Comment) and text.strip() == TCGSHOP_LIST_END)
    if len(starts) != 1 or len(ends) != 1 or ends[0].find_previous(string=lambda text: text is starts[0]) is None:
        raise ShopCatalogParseError(f"{source_url}: no single product list section")
    blocks = soup.select("table[id^=list_card_]")
    listed = [block for block in blocks
              if block.find_previous(string=lambda text: text is starts[0]) is not None
              and block.find_next(string=lambda text: text is ends[0]) is not None]
    if len(listed) != len(blocks):
        raise ShopCatalogParseError(f"{source_url}: product blocks outside the product list section")

    products = [tcgshop_product(block, category, source_url) for block in blocks]
    ids = [product["product_id"] for product in products]
    if len(set(ids)) != len(ids):
        raise ShopCatalogParseError(f"{source_url}: duplicate product ids on the page")

    found = tcgshop_nav(soup, category, source_url)
    totals = {token[2] + token[3] if token[0] == "page"
              else token[2] + token[1] * TCGSHOP_PAGE_SIZE * NAV_BLOCK_PAGES
              for token in found if token[0] != "current"}
    if len(totals) > 1:
        raise ShopCatalogParseError(f"{source_url}: paginator links disagree on the total: {sorted(totals)}")
    if totals:
        total_products = totals.pop()
    elif page_number == 1 and products:
        total_products = len(products)  # a single page shows no page links: the listed products are all
    elif page_number == 1:
        # TCGSHOP prints no total, so a page without products or page links cannot prove an empty category.
        raise ShopCatalogParseError(f"{source_url}: no products and no paginator total")
    else:
        raise ShopCatalogParseError(f"{source_url}: page {page_number} has no paginator links")
    if url_total is not None and url_total != total_products:
        raise ShopCatalogParseError(f"{source_url}: URL total {url_total} differs from the page's {total_products}")
    total_pages = page_count(total_products, TCGSHOP_PAGE_SIZE)
    check_item_count(source_url, page_number, total_products, total_pages, TCGSHOP_PAGE_SIZE, len(products))
    expected = tcgshop_expected_nav(page_number, total_products, total_pages)
    if found != expected:
        raise ShopCatalogParseError(f"{source_url}: paginator {found} does not match page {page_number} of "
                                    f"{total_pages} ({total_products} products)")
    if page_url("tcgshop", category, page_number, total_products) != source_url:
        raise ShopCatalogParseError(f"{source_url}: not the canonical URL of page {page_number}")
    return {"products": products, "total_products": total_products, "total_pages": total_pages,
            "page_number": page_number, "page_size": TCGSHOP_PAGE_SIZE}
