import unittest

from base import build


def person(pid, ln, uat):
    return {"id": pid, "type": "Person",
            "attributes": {"first_name": "P", "last_name": ln, "status": "active",
                           "created_at": "2020-01-01T00:00:00Z", "updated_at": uat},
            "relationships": {}}


class TestWriterSemantics(unittest.TestCase):
    """Spot-check the canonical writer through the package (full matrix is in
    docs/schema_test_sqlite.py)."""

    def setUp(self):
        self.m, _ = build()
        self.w = self.m.writer

    def _ln(self, pid="1"):
        return self.m.db.query_one("SELECT last_name FROM person WHERE pco_id=?", (pid,))["last_name"]

    def _row(self, pid="1"):
        return dict(self.m.db.query_one("SELECT * FROM person WHERE pco_id=?", (pid,)))

    def test_monotonic_older_is_noop(self):
        self.w.upsert("person", "1", person("1", "New", "2026-02-01T00:00:00Z"), "webhook")
        self.w.upsert("person", "1", person("1", "Old", "2025-01-01T00:00:00Z"), "webhook")
        self.assertEqual(self._ln(), "New")

    def test_same_second_correction(self):
        self.w.upsert("person", "1", person("1", "Wrong", "2026-03-01T12:00:00Z"), "webhook")
        self.w.upsert("person", "1", person("1", "Right", "2026-03-01T12:00:00Z"), "webhook")
        self.assertEqual(self._ln(), "Right")

    def test_sticky_and_merge_terminal(self):
        self.w.upsert("person", "1", person("1", "A", "2026-03-01T12:00:00Z"), "webhook")
        self.w.tombstone("person", "1", "2026-03-02T00:00:00Z", "destroyed")
        # reordered older update does NOT resurrect
        self.w.upsert("person", "1", person("1", "Reorder", "2026-03-01T12:00:00Z"), "webhook")
        self.assertIsNotNone(self._row()["deleted_at"])
        # merge is terminal even under a newer update; only confirm_live revives
        self.w.tombstone("person", "1", "2026-03-04T00:00:00Z", "merged", merged_into="9")
        self.w.upsert("person", "1", person("1", "Nope", "2026-03-05T00:00:00Z"), "webhook")
        self.assertIsNotNone(self._row()["deleted_at"])
        self.w.confirm_live("person", "1", person("1", "Alive", "2026-03-06T00:00:00Z"), "reconcile")
        self.assertIsNone(self._row()["deleted_at"])

    def test_field_datum_typed_projection(self):
        self.w.upsert_untimed("field_definition", "d1", {"id": "d1", "type": "FieldDefinition",
                              "attributes": {"name": "Age", "slug": "age", "data_type": "number"}}, "backfill")
        self.w.upsert("field_datum", "fd1", {"id": "fd1", "type": "FieldDatum",
                      "attributes": {"value": "42", "created_at": "2020-01-01T00:00:00Z",
                                     "updated_at": "2026-01-01T00:00:00Z"},
                      "relationships": {"customizable": {"data": {"type": "Person", "id": "1"}},
                                        "field_definition": {"data": {"type": "FieldDefinition", "id": "d1"}}}},
                      "backfill")
        self.assertEqual(self.m.db.query_one("SELECT value_number FROM field_datum WHERE pco_id='fd1'")["value_number"], 42.0)


if __name__ == "__main__":
    unittest.main()
