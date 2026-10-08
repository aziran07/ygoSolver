"""Export choices must control actual file contents without changing the deck."""

import copy
import csv
import io
import os
import unittest
from unittest import mock

from openpyxl import load_workbook
from streamlit.delta_generator import DeltaGenerator
from price_test_fixtures import capture_downloads

PRICE_HEADERS = ['단가(원)', '합계(원)']
PRICE_VALUES = [None, None]

import exports
import rarities
import deck_order
from export_order_fixtures import app_sort_keys
import recognition
import references
from test_app import ASH, download_buttons_disabled, row, start_app


class ExportOptionsAppTest(unittest.TestCase):
    fields = ("name_ko", "name_ja", "name_en", "cid")
    labels = ("한국어 카드명", "일본어 카드명", "영어 카드명", "CID")
    headers = ("한국어 이름", "일본어 이름", "영어 이름", "공식 CID")
    values = ("하루 우라라", "灰流うらら", "Ash Blossom & Joyous Spring", 12950)

    def setUp(self):
        sort_patch = mock.patch.object(deck_order, "get_sort_keys", side_effect=app_sort_keys)
        sort_patch.start()
        self.addCleanup(sort_patch.stop)
        self.files = {}
        download_patch = mock.patch.object(DeltaGenerator, 'download_button', capture_downloads(self.files))
        download_patch.start()
        self.addCleanup(download_patch.stop)
        environment = mock.patch.dict(os.environ, {'DATABASE_URL': '', 'REDIS_URL': ''})
        environment.start()
        self.addCleanup(environment.stop)

        for patch in (
            mock.patch.object(recognition, "models_ready", return_value=True),
            mock.patch.object(references, "library_info", return_value=None),
            mock.patch.object(rarities, "get_rarities", return_value=["N", "SR"]),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def assert_file_contents(self, app, indices):
        headers = ["카드명", "레어도", "수량", *[self.headers[i] for i in indices], *PRICE_HEADERS]
        values = ["내 우라라", "슈퍼 레어", 3, *[self.values[i] for i in indices], *PRICE_VALUES]
        self.assertFalse(app.exception)
        self.assertEqual(list(app.dataframe[0].value.columns), headers)
        self.assertEqual(app.dataframe[0].value.values.tolist(), [values])
        self.assertEqual(download_buttons_disabled(app), [False, False])
        csv_rows = list(csv.reader(io.StringIO(self.files["csv"].decode("utf-8-sig"))))
        self.assertEqual(csv_rows, [headers, ["" if value is None else str(value) for value in values]])
        sheet = load_workbook(io.BytesIO(self.files["xlsx"])).active
        self.assertEqual(list(sheet.values), [tuple(headers), tuple(values)])

    def prepared_app(self):
        first = row("a", card=ASH, quantity=3)
        first["name_override"] = "내 우라라"
        app = start_app([first]).run()
        app.selectbox(key="rarity_a").set_value("SR").run()
        return app

    def test_default_choices_and_both_downloads_exclude_optional_columns(self):
        app = self.prepared_app()
        for field, label in zip(self.fields, self.labels):
            checkbox = app.checkbox(key="export_" + field)
            self.assertEqual(checkbox.label, label)
            self.assertFalse(checkbox.value)
        self.assertEqual(download_buttons_disabled(app), [True, True])
        app.checkbox(key="confirmed").check().run()
        self.assert_file_contents(app, [])

    def test_each_choice_and_mixed_choices_persist_reset_confirmation_and_preserve_deck(self):
        app = self.prepared_app()
        original_rows = copy.deepcopy(app.session_state["rows"])
        app.checkbox(key="confirmed").check().run()
        # Add one column at a time, then remove in a different order.
        enabled = set()
        for index in (0, 1, 2, 3, 1, 3, 0, 2):
            with self.subTest(index=index, enabled=enabled.copy()):
                key = "export_" + self.fields[index]
                app.checkbox(key=key).set_value(index not in enabled).run()
                enabled.symmetric_difference_update({index})
                self.assertFalse(app.checkbox(key="confirmed").value)
                self.assertEqual(download_buttons_disabled(app), [True, True])
                app.run()
                for position, field in enumerate(self.fields):
                    self.assertEqual(app.checkbox(key="export_" + field).value, position in enabled)
                self.assertEqual(app.session_state["rows"], original_rows)
                app.checkbox(key="confirmed").check().run()
                self.assert_file_contents(app, sorted(enabled))

    def test_hiding_identity_columns_does_not_bypass_invalid_cid_or_conflicting_name(self):
        invalid = row("bad", card={**ASH, "cid": 0})
        conflict = row("b", card=ASH)
        conflict["name_override"] = "다른 표시 이름"
        for rows, message in (([invalid], "올바른 공식 CID"),
                              ([row("a", card=ASH), conflict], "서로 다른 카드명")):
            with self.subTest(message=message):
                app = start_app(rows).run()
                self.assertFalse(app.exception)
                self.assertTrue(any(message in error.value for error in app.error))
                self.assertEqual(download_buttons_disabled(app), [True, True])
                self.assertEqual(len(app.dataframe), 0)
                for field in self.fields:
                    app.checkbox(key="export_" + field).check().run()
                self.assertTrue(any(message in error.value for error in app.error))
                self.assertEqual(download_buttons_disabled(app), [True, True])


if __name__ == "__main__":
    unittest.main()
