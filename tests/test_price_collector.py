"""On-demand collection requirements; all HTTP responses here are controlled fixtures."""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import requests
from bs4 import BeautifulSoup

import card_prices
import price_collector
import price_store
from price_test_fixtures import group, product

NUMBER = "15AY-JPB22"
PRINTS = [{"pid": 1, "card_number": NUMBER, "rid": 1}]


def search_html():
    # Real product markup, synthetic search echo. This does not prove the live search route.
    soup = BeautifulSoup((Path(__file__).parent / "fixtures/tcgshop_ja_list.html").read_text(encoding="utf-8"),
                         "html.parser")
    for block in list(soup.select("table[id^=list_card_]")):
        if block["id"] != "list_card_39577":
            block.decompose()
    form = soup.select_one('form[name="sortForm"]')
    form["action"] = "goods_list.php"
    form["method"] = "get"
    form.select_one('input[name="Index"]')["type"] = "hidden"
    field = soup.new_tag("input", attrs={"name": "searchstring", "value": NUMBER})
    form.append(field)
    return str(soup)


class CollectorHttpTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(price_store, "latest_attempt", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def session(self, status=200):
        session = mock.Mock()
        response = session.get.return_value
        response.status_code = status
        response.url = price_collector.search_url(NUMBER, "ja")
        response.headers = {"Content-Type": "text/html", "Retry-After": "3600"}
        response.content = search_html().encode("euc-kr")
        return session

    def test_search_is_scoped_to_exact_print_number_and_locale(self):
        for number, locale, index in [(NUMBER, "ja", "288"), ("SD6-KR030", "ko", "276")]:
            parts = urlsplit(price_collector.search_url(number, locale))
            self.assertEqual((parts.scheme, parts.netloc, parts.path),
                             ("http", "www.tcgshop.co.kr", "/goods_list.php"))
            query = parse_qs(parts.query)
            self.assertEqual(query["Index"], [index])
            self.assertEqual(query["searchstring"], [number])
        for number, locale in [(NUMBER, "ko"), ("BAD", "ja"), (NUMBER, "en")]:
            with self.subTest(number=number, locale=locale), self.assertRaises((ValueError, price_store.PriceError)):
                price_collector.search_url(number, locale)

    def test_response_yields_actual_sale_price_stock_and_provenance(self):
        session = self.session()
        metadata, products = price_collector.fetch_search_page(NUMBER, "ja", session=session)
        self.assertEqual(len(products), 1)
        p = products[0]
        self.assertEqual((p["card_number"], p["locale"], p["price_krw"], p["stock_status"]),
                         (NUMBER, "ja", 240, "in_stock"))
        self.assertEqual(p["product_id"], "39577")
        self.assertEqual(metadata["status_code"], 200)
        self.assertEqual(metadata["bytes"], len(session.get.return_value.content))
        self.assertIsNotNone(datetime.fromisoformat(metadata["observed_at"]).tzinfo)
        self.assertEqual(session.get.call_count, 1)
        self.assertIs(session.get.call_args.kwargs["allow_redirects"], False)
        self.assertIn("timeout", session.get.call_args.kwargs)

    def test_redirects_http_errors_invalid_payloads_never_become_empty_success(self):
        for status in [302, 403, 429, 500]:
            session = self.session(status)
            with self.subTest(status=status), self.assertRaises(price_collector.CollectionError):
                price_collector.fetch_search_page(NUMBER, "ja", session=session)
            self.assertEqual(session.get.call_count, 1)
        for body in [b"<html>no results?</html>", b"\xff", search_html().replace('value="288"', 'value="276"').encode("euc-kr")]:
            session = self.session()
            session.get.return_value.content = body
            with self.subTest(body=body[:30]), self.assertRaises(price_collector.CollectionError):
                price_collector.fetch_search_page(NUMBER, "ja", session=session)
        session = self.session()
        session.get.side_effect = requests.Timeout("timeout proof")
        with self.assertRaises(price_collector.CollectionError):
            price_collector.fetch_search_page(NUMBER, "ja", session=session)
        self.assertEqual(session.get.call_count, 1)

    def test_missing_echo_off_target_products_and_empty_page_are_rejected(self):
        html = search_html()
        cases = [html.replace(f'value="{NUMBER}"', 'value="different"'),
                 html.replace(f"({NUMBER})", "(SD6-JP030)"),
                 '<form name="sortForm" action="goods_list.php"><input name="Index" value="288">'
                 f'<input name="searchstring" value="{NUMBER}"></form>',
                 html.replace('action="goods_list.php"', 'action="search_result.php"')]
        for case in cases:
            with self.subTest(case=case[:50]), self.assertRaises(price_collector.CollectionError):
                price_collector.parse_search_page(case, NUMBER, "ja")

    def test_database_failure_never_contacts_shop(self):
        session = self.session()
        with mock.patch.object(price_store, "begin_collection", side_effect=price_store.PriceDatabaseError("db proof")), \
             self.assertRaises(price_store.PriceDatabaseError):
            price_collector.collect_card_number(NUMBER, "ja", "postgresql://test", session=session)
        session.get.assert_not_called()

    def test_http_failure_is_persisted_and_not_imported(self):
        now = datetime.now(timezone.utc)
        permit = {"attempt_id": 7, "started_at": now}
        with mock.patch.object(price_store, "begin_collection", return_value=permit), \
             mock.patch.object(price_store, "finish_attempt") as finish, \
             mock.patch.object(price_store, "store_listing") as store:
            result = price_collector.collect_card_number(NUMBER, "ja", "postgresql://test", session=self.session(429))
        self.assertEqual(result["state"], "failed")
        self.assertIn("429", result["error"])
        self.assertEqual(finish.call_count, 1)
        store.assert_not_called()

    def test_real_search_returns_sale_price_and_stock_without_using_crossed_out_list_price(self):
        html = (Path(__file__).parent / 'fixtures/tcgshop_search_RV01-JP069.html').read_text(encoding='utf-8')
        products = price_collector.parse_search_page(html, 'RV01-JP069', 'ja')
        self.assertEqual(len(products), 1)
        p = products[0]
        self.assertEqual((p['product_id'], p['card_number'], p['rarity_label'], p['price_krw'], p['stock_status']),
                         ('130184', 'RV01-JP069', 'Normal', 640, 'in_stock'))
        self.assertEqual(p['product_url'], 'http://www.tcgshop.co.kr/goods_detail.php?goodsIdx=130184')

    def test_real_unavailable_stock_is_not_inferred_from_a_price(self):
        html = (Path(__file__).parent / 'fixtures/tcgshop_search_SLF1-JP081.html').read_text(encoding='utf-8')
        products = price_collector.parse_search_page(html, 'SLF1-JP081', 'ja')
        self.assertEqual([(p['product_id'], p['rarity_label'], p['price_krw'], p['stock_status']) for p in products],
                         [('88627', 'Secret Rare', 12000, 'unknown'), ('88537', 'Super Rare', 5600, 'unknown'),
                          ('88437', 'Normal', 3200, 'unknown')])

    def test_only_verified_complete_empty_search_is_accepted(self):
        html = (Path(__file__).parent / 'fixtures/tcgshop_search_SLF1-JP999.html').read_text(encoding='utf-8')
        self.assertEqual(price_collector.parse_search_page(html, 'SLF1-JP999', 'ja'), [])
        malformed = [html.replace('<!-- 상품 목록 끝 -->', ''),
                     html.replace('<!-- 상품 목록 시작 -->', '<!-- 상품 목록 시작 --><!-- 상품 목록 시작 -->'),
                     html.replace('value="SLF1-JP999"', 'value="SLF1-JP081"'),
                     html.replace('value="288"', 'value="276"'),
                     html.replace('<!-- 상품 목록 끝 -->', '<p>Service unavailable</p><!-- 상품 목록 끝 -->'),
                     html.replace('<!-- 상품 목록 끝 -->', '<table id="list_card_bad"></table><!-- 상품 목록 끝 -->')]
        for bad in malformed:
            with self.subTest(bad=bad[-100:]), self.assertRaises(price_collector.CollectionError):
                price_collector.parse_search_page(bad, 'SLF1-JP999', 'ja')

    def test_previous_failure_is_stable_until_explicit_retry(self):
        old = datetime.now(timezone.utc) - timedelta(days=2)
        attempt = {'outcome': 'failed', 'reserved_at': old, 'finished_at': old, 'error': 'HTTP 403 prior'}
        session = self.session(429)
        with mock.patch.object(price_store, 'latest_attempt', return_value=attempt), \
             mock.patch.object(price_store, 'begin_collection', return_value={'attempt_id': 9, 'started_at': old}) as begin, \
             mock.patch.object(price_store, 'finish_attempt'):
            prior = price_collector.collect_card_number(NUMBER, 'ja', 'postgresql://test', session=session)
            self.assertEqual(prior['state'], 'failed')
            self.assertIn('403 prior', prior['error'])
            session.get.assert_not_called()
            begin.assert_not_called()
            actual = price_collector.collect_card_number(NUMBER, 'ja', 'postgresql://test', session=session, retry=True)
            self.assertEqual(actual['state'], 'failed')
            self.assertIn('429', actual['error'])
            self.assertEqual(session.get.call_count, 1)

    def test_verified_empty_has_finite_freshness_and_explicit_retry(self):
        now = datetime.now(timezone.utc)
        for age, retry, expected_calls in [(1, False, 0), (13, False, 1), (1, True, 1)]:
            observed = now - timedelta(hours=age)
            prior = {'outcome': 'empty', 'reserved_at': observed, 'finished_at': observed, 'error': None}
            with mock.patch.object(price_store, 'latest_attempt', return_value=prior), \
                 mock.patch.object(price_store, 'begin_collection', return_value={'attempt_id': 9, 'started_at': now}), \
                 mock.patch.object(price_store, 'record_empty', return_value=now), \
                 mock.patch.object(price_collector, 'fetch_search_page', return_value=({}, [])) as fetch:
                result = price_collector.collect_card_number(NUMBER, 'ja', 'postgresql://test', retry=retry)
            self.assertEqual(result['state'], 'empty')
            self.assertEqual(fetch.call_count, expected_calls)
            self.assertEqual(result['observed_at'], now if expected_calls else observed)


class DemandDecisionTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(price_store, "latest_attempt", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def quote(self, observations, collect, prints=PRINTS):
        return card_prices.price_with_collection("N", "ja", prints, observations,
                                                 "postgresql://test", "redis://test", collect=collect)

    def test_fresh_in_stock_or_no_stock_and_backend_errors_never_collect(self):
        for stock, expected in [("in_stock", "ok"), ("out_of_stock", "no_stock")]:
            collector = mock.Mock()
            result = self.quote({NUMBER: {"observed": group(NUMBER, [product(stock=stock)])}}, collector)
            self.assertEqual(result["status"], expected)
            collector.assert_not_called()
        for error, status in [(price_store.PriceDatabaseError("db"), "database_error"),
                              (price_store.PriceCacheError("cache"), "cache_error"),
                              (price_store.PriceError("data"), "data_error")]:
            collector = mock.Mock()
            result = self.quote({NUMBER: {"error": error}}, collector)
            self.assertEqual(result["status"], status)
            collector.assert_not_called()

    def test_absence_collects_then_rereads_stored_price(self):
        collector = mock.Mock(return_value={"state": "stored", "snapshot_id": 10, "product_count": 1})
        new = {NUMBER: {"observed": group(NUMBER, [product(price=360)])}}
        with mock.patch.object(card_prices, "observe_card_numbers", return_value=new) as read:
            result = self.quote({NUMBER: {"not_found": True}}, collector)
        self.assertEqual((result["status"], result["unit_price_krw"]), ("ok", 360))
        self.assertEqual(collector.call_count, 1)
        self.assertEqual(collector.call_args.args[:2], (NUMBER, "ja"))
        self.assertEqual(read.call_count, 1)

    def test_expired_is_refreshed_before_never_observed_alternative(self):
        stale = "SD6-JP030"
        prints = PRINTS + [{"pid": 2, "card_number": stale, "rid": 1}]
        observations = {NUMBER: {"not_found": True}, stale: {"error": price_store.StalePriceError("stale")}}
        collector = mock.Mock(return_value={"state": "stored", "snapshot_id": 11, "product_count": 1})
        fresh = {stale: {"observed": group(stale, [product(number=stale, price=450)])}}
        with mock.patch.object(card_prices, "observe_card_numbers", return_value=fresh):
            result = self.quote(observations, collector, prints)
        self.assertEqual(collector.call_args_list[0].args[0], stale)
        self.assertEqual((result["status"], result["unit_price_krw"]), ("ok", 450))
        self.assertEqual(collector.call_count, 1)

    def test_collection_failure_never_returns_old_amount_or_invents_next_time(self):
        cases = [({"state": "failed", "error": "HTTP 429 proof", "attempted_at": datetime.now(timezone.utc)},
                  "collection_failed")]
        for state, status in cases:
            collector = mock.Mock(return_value=state)
            with self.subTest(status=status):
                result = self.quote({NUMBER: {"error": price_store.StalePriceError("old")}}, collector)
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["unit_price_krw"])
                if status == "collection_failed":
                    self.assertIn("429", result["detail"])
                self.assertNotIn("12시간", result["detail"])
                self.assertNotIn("다음 시도는", result["detail"])

    def test_read_failure_after_collection_does_not_use_response_as_price(self):
        collector = mock.Mock(return_value={"state": "stored", "snapshot_id": 12, "product_count": 1})
        failed = {NUMBER: {"error": price_store.PriceCacheError("cache read proof")}}
        with mock.patch.object(card_prices, "observe_card_numbers", return_value=failed):
            result = self.quote({NUMBER: {"not_found": True}}, collector)
        self.assertEqual(result["status"], "cache_error")
        self.assertIsNone(result["unit_price_krw"])
        self.assertIn("cache read proof", result["detail"])

    def test_unknown_collection_outcome_is_not_treated_as_stored(self):
        collector = mock.Mock(return_value={"state": "unexpected"})
        with mock.patch.object(card_prices, "observe_card_numbers") as read, \
             self.assertRaises((ValueError, price_store.PriceError)):
            self.quote({NUMBER: {"not_found": True}}, collector)
        read.assert_not_called()

    def test_verified_empty_after_expiry_never_returns_the_historical_price(self):
        now = datetime.now(timezone.utc)
        collector = mock.Mock(return_value={'state': 'empty', 'observed_at': now})
        result = self.quote({NUMBER: {'error': price_store.StalePriceError('old quote')}}, collector)
        self.assertEqual(result['status'], 'not_listed')
        self.assertIsNone(result['unit_price_krw'])
        self.assertEqual(collector.call_count, 1)

    def test_explicit_retry_uses_the_retry_keyword_not_the_http_session_slot(self):
        collector = mock.Mock(return_value={'state': 'failed', 'error': 'HTTP 429',
                                            'attempted_at': datetime.now(timezone.utc)})
        card_prices.price_with_collection('N', 'ja', PRINTS, {NUMBER: {'not_found': True}},
                                          'postgresql://test', 'redis://test', collect=collector, retry=True)
        self.assertEqual(len(collector.call_args.args), 4)
        self.assertIs(collector.call_args.kwargs['retry'], True)


if __name__ == "__main__":
    unittest.main()
