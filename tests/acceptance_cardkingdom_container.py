"""Opt-in live test: fresh Linux Chromium with an explicit navigation flow.

Never runs during unittest discovery. Prices/stock are observations from
2026-10-08; a changed listing fails for review instead of inventing a price.
Only rendered text, screenshots, and allowlisted metadata leave the browser.
"""

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit


SHOP = "https://smartstore.naver.com/cardkingdom"
PUBLIC_REQUEST_HEADERS = (
    "user-agent", "accept", "accept-language", "accept-encoding",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
    "upgrade-insecure-requests", "priority",
)
PRODUCTS = (
    {
        "id": "4960632716",
        "title": "유희왕 한글판 유령토끼 울트라레어 RC03-KR007",
        "price": 200,
        "availability": "InStock",
    },
    {
        "id": "11557581798",
        "title": "유희왕 한글판 유령토끼 슈퍼레어 QCAC-KR048",
        "price": 200,
        "availability": "OutOfStock",
    },
)


def public_url(url):
    """Exclude query parameters and fragments from diagnostic URLs."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def chrome_headers(browser_version):
    """Explicit HTTP-only experiment, not a copied user profile or TLS identity."""
    major = browser_version.split(".")[0]
    require(major.isdecimal(), "Cannot form Chrome headers from browser version")
    return {
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        ),
        "accept-language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": (
            f'"Chromium";v="{major}", "Google Chrome";v="{major}", '
            '"Not_A Brand";v="24"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }


def save_page(page, output, label):
    rendered = page.locator("body").inner_text(timeout=5000)
    (output / f"{label}.txt").write_text(rendered[:20000], encoding="utf-8")
    page.screenshot(path=str(output / f"{label}.png"), timeout=10000)
    return rendered


def check_access(page, response, report):
    if response is not None:
        require(response.status < 400, f"Document returned HTTP {response.status}")
    require(urlsplit(page.url).hostname != "nid.naver.com", "Redirected to Naver login")
    failures = report["http_errors"]
    require(not failures, f"HTTP errors during this browser flow: {failures}")
    text = page.locator("body").inner_text(timeout=5000)
    for marker in ("현재 서비스 접속이 불가합니다", "접속이 일시적으로 제한", "자동입력 방지문자"):
        require(marker not in text, f"Access challenge or error page: {marker}")


def verify_product(page, expected, output):
    from playwright.sync_api import expect

    expect(page.get_by_role("heading", name=expected["title"], exact=True)).to_be_visible()
    page.locator('script[type="application/ld+json"]').first.wait_for(state="attached")
    products = []
    for raw in page.locator('script[type="application/ld+json"]').all_text_contents():
        value = json.loads(raw)
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("@type") == "Product":
                products.append(entry)
    require(len(products) == 1, f"Expected one Product JSON-LD, found {len(products)}")
    product = products[0]
    require(product["name"] in (expected["title"], expected["title"] + " : 카드킹덤"),
            "JSON-LD name does not identify the expected print")
    offer = product["offers"]
    require(isinstance(offer, dict), "Expected a single unambiguous Offer")
    require(offer["priceCurrency"] == "KRW", "Unexpected currency")
    require(offer["price"] == expected["price"], "Price differs from the reviewed snapshot")
    require(offer["availability"] in (
        "http://schema.org/" + expected["availability"],
        "https://schema.org/" + expected["availability"],
    ), "Stock state differs from the reviewed snapshot")
    require(public_url(page.url) == SHOP + "/products/" + expected["id"],
            "Wrong store or product URL")
    require(urlsplit(offer["url"]).hostname == "smartstore.naver.com"
            and urlsplit(offer["url"]).path in (
                "/main/products/" + expected["id"],
                "/cardkingdom/products/" + expected["id"],
            ), "JSON-LD offer points to a different product")
    if expected["availability"] == "InStock":
        expect(page.get_by_role("button", name="구매하기", exact=True)).to_be_enabled()
    else:
        expect(page.get_by_role("button", name="품절되었습니다", exact=True)).to_be_disabled()
    text = save_page(page, output, expected["id"])
    require(re.search(r"(?<![\d,])" + str(expected["price"]) + r"\s*원", text) is not None,
            "Expected price is absent from the rendered page")
    return {
        "url": public_url(page.url),
        "title": expected["title"],
        "price": offer["price"],
        "currency": offer["priceCurrency"],
        "availability": offer["availability"],
        "visible_price_and_purchase_state_checked": True,
    }


def verify_naver_product(page, context, report, output):
    """One Naver previsit and one product visit, without retry or search."""
    report["phase"] = "naver_previsit"
    response = page.goto("https://www.naver.com/", wait_until="load")
    require(response is not None, "Naver navigation returned no response")
    check_access(page, response, report)
    require(response.status == 200, f"Naver returned HTTP {response.status}")
    require(public_url(page.url) == "https://www.naver.com/", "Unexpected Naver destination")
    cookies = context.cookies()
    report["naver_previsit"] = {
        "url": public_url(page.url),
        "status": response.status,
        "wait_until": "load",
        "cookie_count": len(cookies),
        "nnb_present": any(cookie["name"] == "NNB" for cookie in cookies),
    }
    report["phase"] = "in_stock_product"
    response = page.goto(SHOP + "/products/" + PRODUCTS[0]["id"], wait_until="load")
    require(response is not None, "Product navigation returned no response")
    check_access(page, response, report)
    require(response.status == 200, f"Product returned HTTP {response.status}")
    report["products"].append(verify_product(page, PRODUCTS[0], output))
    check_access(page, None, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--header-profile", choices=("baseline", "chrome-headers"),
                        default="baseline")
    parser.add_argument("--flow", choices=("shop-search", "headed-naver-product"),
                        default="shop-search")
    args = parser.parse_args()
    headed = args.flow == "headed-naver-product"
    if headed and args.header_profile != "baseline":
        parser.error("headed-naver-product requires unmodified baseline headers")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("The evidence directory must be empty; preserve earlier runs separately")
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "phase": "environment",
        "header_profile": args.header_profile,
        "flow": args.flow,
        "scope": "one known in-stock product" if headed else "shop search and two products",
        "environment": {
            "system": platform.system(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "docker_marker": Path("/.dockerenv").is_file(),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "headless": not headed,
            "display_present": bool(os.environ.get("DISPLAY")),
        },
        "http_errors": [],
        "documents": [],
        "products": [],
    }
    page = None
    browser = None
    try:
        require(report["environment"]["system"] == "Linux", "Requires actual Linux")
        require(report["environment"]["docker_marker"], "Requires an actual Docker container")
        require(report["environment"]["uid"] not in (None, 0), "Requires a non-root user")
        if headed:
            require(report["environment"]["display_present"], "Headed flow requires an X display")
        from playwright.sync_api import sync_playwright, expect

        report["environment"]["playwright"] = version("playwright")
        report["environment"]["os_release"] = platform.freedesktop_os_release()
        with sync_playwright() as playwright:
            try:
                report["phase"] = "browser_launch"
                browser = playwright.chromium.launch(headless=not headed, chromium_sandbox=True)
                report["environment"]["chromium"] = browser.version
                report["environment"]["sandbox_requested"] = True
                options = {"viewport": {"width": 1280, "height": 1000}}
                configured_headers = {}
                if args.header_profile == "chrome-headers":
                    configured_headers = chrome_headers(browser.version)
                    options["user_agent"] = configured_headers["user-agent"]
                    options["extra_http_headers"] = {
                        name: value for name, value in configured_headers.items()
                        if name != "user-agent"
                    }
                report["configured_headers"] = configured_headers
                report["phase"] = "browser_context"
                context = browser.new_context(**options)
                report["initial_cookie_count"] = len(context.cookies())
                require(report["initial_cookie_count"] == 0, "Browser context is not fresh")
                report["phase"] = "browser_page"
                page = context.new_page()
                page.set_default_timeout(20000)
                page.set_default_navigation_timeout(30000)
                report["environment"]["navigator_webdriver"] = page.evaluate("navigator.webdriver")

                def record_response(response):
                    request = response.request
                    record = {"url": public_url(response.url), "status": response.status}
                    if request.resource_type == "document" and request.frame == page.main_frame:
                        headers = request.all_headers()
                        record["request_headers"] = {
                            name: headers[name] for name in PUBLIC_REQUEST_HEADERS if name in headers
                        }
                        record["retry_after"] = response.header_value("retry-after")
                        report["documents"].append(record)
                    if response.status >= 400 and urlsplit(response.url).hostname == "smartstore.naver.com":
                        report["http_errors"].append(record)

                page.on("response", record_response)
                if headed:
                    verify_naver_product(page, context, report, args.output)
                else:
                    report["phase"] = "browser_network_smoke"
                    response = page.goto("https://example.com", wait_until="domcontentloaded")
                    require(response is not None and response.status == 200, "Network smoke failed")
                    expect(page).to_have_title("Example Domain")
                    report["network_smoke_passed"] = True

                    report["phase"] = "shop_home"
                    response = page.goto(SHOP, wait_until="domcontentloaded")
                    require(response is not None, "Shop navigation returned no response")
                    sent_headers = response.request.all_headers()
                    report["configured_headers_verified"] = all(
                        sent_headers.get(name) == value for name, value in configured_headers.items()
                    )
                    require(report["configured_headers_verified"],
                            "Configured HTTP headers were not sent as intended")
                    check_access(page, response, report)
                    page.get_by_role("button", name="검색어를 입력해주세요", exact=True).click()
                    report["phase"] = "shop_search"
                    page.get_by_role("textbox", name="검색어 입력", exact=True).fill("RC03-KR007")
                    page.get_by_role("button", name="검색하기", exact=True).click()
                    target = page.get_by_role("link", name=PRODUCTS[0]["title"], exact=True)
                    expect(target).to_be_visible()
                    check_access(page, None, report)
                    save_page(page, args.output, "search")
                    report["search"] = {"query": "RC03-KR007", "expected_product_visible": True}

                    report["phase"] = "in_stock_product"
                    target.click()
                    report["products"].append(verify_product(page, PRODUCTS[0], args.output))
                    check_access(page, None, report)
                    report["phase"] = "sold_out_product"
                    response = page.goto(SHOP + "/products/" + PRODUCTS[1]["id"],
                                         wait_until="domcontentloaded")
                    check_access(page, response, report)
                    report["products"].append(verify_product(page, PRODUCTS[1], args.output))
                    check_access(page, None, report)
                report["status"] = "passed"
                report["phase"] = "complete"
            except Exception:
                if page is not None and not page.is_closed():
                    try:
                        report["last_url"] = public_url(page.url)
                        report["last_title"] = page.title()
                        save_page(page, args.output, "failure")
                    except Exception as evidence_error:
                        report["evidence_error"] = str(evidence_error)
                raise
            finally:
                if browser is not None:
                    browser.close()
    except Exception as error:
        report["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    memory_peak = Path("/sys/fs/cgroup/memory.peak")
    if memory_peak.is_file():
        try:
            report["cgroup_memory_peak_bytes"] = int(memory_peak.read_text().strip())
        except (OSError, ValueError) as error:
            report["status"] = "failed"
            report["metrics_error"] = str(error)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
