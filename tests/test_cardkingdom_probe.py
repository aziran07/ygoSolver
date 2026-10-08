"""Offline checks for the one-request probe's failure and identity boundaries."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


spec = importlib.util.spec_from_file_location(
    "cardkingdom_probe", Path(__file__).with_name("acceptance_cardkingdom_container.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

NAVER = "https://www.naver.com/"
PRODUCT = "https://smartstore.naver.com/cardkingdom/products/4960632716"
# Reviewed source snapshot, not computed from the validator's expected values.
PRODUCT_DATA = {
    "@type": "Product",
    "name": "유희왕 한글판 유령토끼 울트라레어 RC03-KR007 : 카드킹덤",
    "offers": {
        "url": "https://smartstore.naver.com/main/products/4960632716",
        "price": 200,
        "priceCurrency": "KRW",
        "availability": "https://schema.org/InStock",
    },
}


class HeadedFlowTests(unittest.TestCase):
    def setUp(self):
        self.report = {"http_errors": [], "products": [], "status": "failed"}
        self.page = Mock(url="about:blank")
        self.page.locator.return_value.inner_text.return_value = "normal page"
        self.context = Mock()
        self.context.cookies.return_value = [{"name": "NNB", "value": "must-not-be-saved"}]
        self.statuses = [200, 200]

        def navigate(url, **kwargs):
            self.page.url = url
            return SimpleNamespace(status=self.statuses[self.page.goto.call_count - 1])

        self.page.goto.side_effect = navigate

    def run_flow(self):
        probe.verify_naver_product(self.page, self.context, self.report, Path("unused"))

    def test_one_previsit_then_one_product_both_wait_for_load(self):
        with patch.object(probe, "verify_product", return_value={"price": 200}) as verify:
            self.run_flow()
        self.assertEqual(self.page.goto.call_args_list, [
            call(NAVER, wait_until="load"), call(PRODUCT, wait_until="load")])
        verify.assert_called_once_with(self.page, probe.PRODUCTS[0], Path("unused"))
        self.assertEqual(self.report["products"], [{"price": 200}])
        self.assertTrue(self.report["naver_previsit"]["nnb_present"])
        self.assertNotIn("must-not-be-saved", json.dumps(self.report))

    def test_nnb_absence_is_observation_not_a_claimed_blocking_cause(self):
        self.context.cookies.return_value = []
        with patch.object(probe, "verify_product", return_value={"price": 200}):
            self.run_flow()
        self.assertFalse(self.report["naver_previsit"]["nnb_present"])
        self.assertEqual(len(self.report["products"]), 1)

    def test_previsit_429_stops_before_product(self):
        self.statuses = [429]
        with self.assertRaisesRegex(AssertionError, "HTTP 429"):
            self.run_flow()
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertEqual(self.report["phase"], "naver_previsit")
        self.assertEqual(self.report["products"], [])

    def test_product_429_stops_without_retry_or_price_validation(self):
        self.statuses = [200, 429]
        with patch.object(probe, "verify_product") as verify:
            with self.assertRaisesRegex(AssertionError, "HTTP 429"):
                self.run_flow()
        verify.assert_not_called()
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertEqual(self.report["phase"], "in_stock_product")
        self.assertEqual(self.report["products"], [])

    def test_login_and_challenge_pages_fail(self):
        for url, body, message in (
            ("https://nid.naver.com/login", "로그인", "Naver login"),
            (PRODUCT, "자동입력 방지문자", "Access challenge"),
        ):
            with self.subTest(url=url):
                self.page.url = url
                self.page.locator.return_value.inner_text.return_value = body
                with self.assertRaisesRegex(AssertionError, message):
                    probe.check_access(self.page, SimpleNamespace(status=200), self.report)

    def test_missing_response_and_failed_load_do_not_continue(self):
        for error in (None, TimeoutError("load timed out")):
            with self.subTest(error=error):
                self.page.goto.reset_mock(side_effect=True)
                if isinstance(error, Exception):
                    self.page.goto.side_effect = error
                else:
                    self.page.goto.return_value = None
                with self.assertRaises((AssertionError, TimeoutError)):
                    self.run_flow()
                self.assertEqual(self.page.goto.call_count, 1)
                self.assertEqual(self.report["products"], [])

    def test_validation_error_propagates_without_success(self):
        with patch.object(probe, "verify_product", side_effect=AssertionError("Wrong print")):
            with self.assertRaisesRegex(AssertionError, "Wrong print"):
                self.run_flow()
        self.assertEqual(self.report["products"], [])
        self.assertEqual(self.report["status"], "failed")
        self.assertEqual(self.page.goto.call_count, 2)


class ProductValidationTests(unittest.TestCase):
    def verify(self, data, url=PRODUCT):
        page = Mock(url=url)
        page.locator.return_value.all_text_contents.return_value = [json.dumps(data)]
        # Browser visibility/purchase controls are checked by the live acceptance
        # test; these offline tests exercise actual structured identity validators.
        with patch.dict("sys.modules", {"playwright.sync_api": SimpleNamespace(expect=Mock())}):
            with patch.object(probe, "save_page", return_value="판매가 200원"):
                return probe.verify_product(page, probe.PRODUCTS[0], Path("unused"))

    def test_reviewed_offer_is_accepted(self):
        result = self.verify(PRODUCT_DATA)
        self.assertEqual(result["price"], 200)
        self.assertEqual(result["currency"], "KRW")
        self.assertEqual(result["availability"], "https://schema.org/InStock")

    def test_changed_or_ambiguous_offer_is_rejected(self):
        changes = (
            ("price", 201), ("priceCurrency", "USD"),
            ("availability", "https://schema.org/OutOfStock"),
            ("url", "https://smartstore.naver.com/main/products/999"),
        )
        for key, value in changes:
            with self.subTest(key=key):
                data = copy.deepcopy(PRODUCT_DATA)
                data["offers"][key] = value
                with self.assertRaises(AssertionError):
                    self.verify(data)
        for data in ([], [PRODUCT_DATA, PRODUCT_DATA],
                     dict(PRODUCT_DATA, name="Different card"),
                     dict(PRODUCT_DATA, offers=[PRODUCT_DATA["offers"]])):
            with self.subTest(data=data):
                with self.assertRaises(AssertionError):
                    self.verify(data)
        with self.assertRaisesRegex(AssertionError, "Wrong store"):
            self.verify(PRODUCT_DATA, "https://smartstore.naver.com/other/products/4960632716")

    def test_non_linux_run_exits_failed_before_network(self):
        with tempfile.TemporaryDirectory() as output:
            with patch("sys.argv", ["probe", "--output", output, "--flow", "headed-naver-product"]):
                with patch.object(probe.platform, "system", return_value="Windows"):
                    with patch("builtins.print"):
                        self.assertEqual(probe.main(), 1)
            report = json.loads((Path(output) / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["phase"], "environment")
        self.assertEqual(report["error"]["message"], "Requires actual Linux")
        self.assertEqual(report["documents"], [])


if __name__ == "__main__":
    unittest.main()
