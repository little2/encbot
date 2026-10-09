import unittest
from collections import deque

import utils.peach_exchange_store as peach_exchange_store


class PeachExchangeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        peach_exchange_store.PEACH_EXCHANGE_RECORDS = deque(maxlen=3)

    def test_remember_peach_exchange_record_is_unique_and_trims_oldest(self) -> None:
        self.assertTrue(peach_exchange_store.remember_peach_exchange_record(1, 10))
        self.assertFalse(peach_exchange_store.remember_peach_exchange_record(1, 10))
        self.assertTrue(peach_exchange_store.remember_peach_exchange_record(2, 20))
        self.assertTrue(peach_exchange_store.remember_peach_exchange_record(3, 30))
        self.assertTrue(peach_exchange_store.remember_peach_exchange_record(4, 40))

        self.assertEqual(
            list(peach_exchange_store.PEACH_EXCHANGE_RECORDS),
            [(2, 20), (3, 30), (4, 40)],
        )

    def test_remove_peach_exchange_for_user_removes_matching_value(self) -> None:
        peach_exchange_store.remember_peach_exchange_record(10, 100)
        peach_exchange_store.remember_peach_exchange_record(11, 101)
        peach_exchange_store.remember_peach_exchange_record(12, 100)

        self.assertTrue(peach_exchange_store.has_peach_exchange_for_user(100, 10))
        self.assertTrue(peach_exchange_store.remove_peach_exchange_for_user(100, 10))
        self.assertFalse(peach_exchange_store.has_peach_exchange_for_user(100, 10))
        self.assertEqual(list(peach_exchange_store.PEACH_EXCHANGE_RECORDS), [(11, 101), (12, 100)])

    def test_bump_alert_button_text_increments_numeric_suffix(self) -> None:
        self.assertEqual(peach_exchange_store.bump_alert_button_text("👍", "alert:like"), "👍 1")
        self.assertEqual(peach_exchange_store.bump_alert_button_text("👍 2", "alert:like"), "👍 3")
        self.assertEqual(peach_exchange_store.bump_alert_button_text("👎", "alert:dislike"), "👎 1")
        self.assertEqual(peach_exchange_store.bump_alert_button_text("👎 5", "alert:dislike"), "👎 6")


if __name__ == "__main__":
    unittest.main()
