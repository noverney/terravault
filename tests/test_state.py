"""Tests for terravault.state."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
import tempfile
import os

from terravault.state import JSONStateManager, SQLiteStateManager


def _dt(year=2024, month=6, day=1, hour=12) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


class TestJSONStateManager(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmpdir, "state.json")
        self.mgr = JSONStateManager(self.state_file)

    def test_initial_state_is_empty(self):
        self.assertIsNone(self.mgr.last_processed)
        self.assertEqual(self.mgr.ingested_ids(), set())

    def test_mark_processed_updates_last_processed(self):
        dt = _dt()
        self.mgr.mark_processed("item-1", dt)
        self.assertEqual(self.mgr.last_processed, dt)

    def test_mark_processed_records_item_id(self):
        self.mgr.mark_processed("item-1", _dt())
        self.assertTrue(self.mgr.is_ingested("item-1"))

    def test_unknown_item_not_ingested(self):
        self.assertFalse(self.mgr.is_ingested("item-unknown"))

    def test_last_processed_advances_with_later_timestamps(self):
        self.mgr.mark_processed("item-1", _dt(day=1))
        self.mgr.mark_processed("item-2", _dt(day=3))
        self.assertEqual(self.mgr.last_processed, _dt(day=3))

    def test_last_processed_does_not_regress(self):
        self.mgr.mark_processed("item-1", _dt(day=3))
        self.mgr.mark_processed("item-2", _dt(day=1))
        self.assertEqual(self.mgr.last_processed, _dt(day=3))

    def test_duplicate_item_ids_not_double_stored(self):
        self.mgr.mark_processed("item-1", _dt())
        self.mgr.mark_processed("item-1", _dt())
        ids = self.mgr.ingested_ids()
        self.assertEqual(ids, {"item-1"})

    def test_state_persists_across_manager_instances(self):
        self.mgr.mark_processed("item-abc", _dt())
        mgr2 = JSONStateManager(self.state_file)
        self.assertTrue(mgr2.is_ingested("item-abc"))
        self.assertIsNotNone(mgr2.last_processed)

    def test_ingested_ids_returns_all_ids(self):
        for i in range(5):
            self.mgr.mark_processed(f"item-{i}", _dt(day=i + 1))
        self.assertEqual(len(self.mgr.ingested_ids()), 5)


class TestSQLiteStateManager(unittest.TestCase):
    def setUp(self):
        # Use in-memory database for speed
        self.mgr = SQLiteStateManager(":memory:")

    def test_initial_state_is_empty(self):
        self.assertIsNone(self.mgr.last_processed)
        self.assertEqual(self.mgr.ingested_ids(), set())

    def test_mark_processed_updates_last_processed(self):
        dt = _dt()
        self.mgr.mark_processed("item-1", dt)
        self.assertIsNotNone(self.mgr.last_processed)

    def test_mark_processed_records_item_id(self):
        self.mgr.mark_processed("item-1", _dt())
        self.assertTrue(self.mgr.is_ingested("item-1"))

    def test_unknown_item_not_ingested(self):
        self.assertFalse(self.mgr.is_ingested("item-unknown"))

    def test_duplicate_insert_is_idempotent(self):
        self.mgr.mark_processed("item-1", _dt())
        self.mgr.mark_processed("item-1", _dt(day=2))  # should not raise
        self.assertTrue(self.mgr.is_ingested("item-1"))

    def test_ingested_ids_returns_all(self):
        for i in range(3):
            self.mgr.mark_processed(f"item-{i}", _dt(day=i + 1))
        self.assertEqual(len(self.mgr.ingested_ids()), 3)

    def test_close_does_not_raise(self):
        self.mgr.close()


if __name__ == "__main__":
    unittest.main()
