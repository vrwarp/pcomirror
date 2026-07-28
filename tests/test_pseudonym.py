"""Believable strangers, reproducibly — and nobody real left behind.

This package exists so a divergence log can be handed to somebody outside the
building. Two properties carry that, and everything here is one or the other:
the structure survives (or the log is not diagnosable), and the people do not
(or it must never leave).
"""
from __future__ import annotations

import json
import re
import unittest

from base import build
from pcomirror import pseudonym
from pcomirror.pseudonym import fields, pools, values

SECRET = b"a-fixed-secret-for-tests"
OTHER_SECRET = b"a-different-install-entirely"


def P(secret=SECRET):
    return pseudonym.Pseudonymiser(secret)


class TestTheStructureSurvives(unittest.TestCase):
    """What makes a divergence readable is which records relate to which."""

    def test_the_same_value_always_becomes_the_same_pseudonym(self):
        p = P()
        self.assertEqual(p.value("first_name", "Nathaniel"), p.value("first_name", "Nathaniel"))
        self.assertEqual(P().value("last_name", "Reed"), P().value("last_name", "Reed"))

    def test_a_family_still_looks_like_a_family(self):
        """One surname across a child, a parent and their household name."""
        p = P()
        child = p.value("last_name", "Reed")
        parent = p.value("last_name", "Reed")
        household = p.value("name", "Reed Household")
        full = p.value("name", "Dana Reed")
        self.assertEqual(child, parent)
        self.assertEqual(household, f"{child} Household")
        self.assertTrue(full.endswith(f" {child}"), f"{full!r} should end with {child!r}")

    def test_a_persons_name_agrees_with_its_parts(self):
        p = P()
        composed = f'{p.value("first_name", "Dana")} {p.value("last_name", "Reed")}'
        self.assertEqual(p.value("name", "Dana Reed"), composed)

    def test_different_values_stay_different(self):
        p = P()
        self.assertNotEqual(p.value("last_name", "Reed"), p.value("last_name", "Okafor"))

    def test_spelling_differences_that_pco_ignores_are_folded(self):
        p = P()
        self.assertEqual(p.value("first_name", "  dana "), p.value("first_name", "Dana"))

    def test_two_installs_disagree(self):
        """A pseudonym is only stable within the install that made it."""
        self.assertNotEqual(P(SECRET).value("last_name", "Reed"),
                            P(OTHER_SECRET).value("last_name", "Reed"))


class TestNobodyRealSurvives(unittest.TestCase):
    def test_no_input_word_appears_in_the_output(self):
        p = P()
        real = {"first_name": "Nathaniel", "last_name": "Reed", "nickname": "Nate",
                "name": "Nathaniel Reed", "address": "nathaniel.reed@gmail.com",
                "number": "+1 (555) 867-5309", "street": "42 Rectory Lane",
                "city": "Guildford", "zip": "12401",
                "medical_notes": "Severe peanut allergy"}
        rendered = json.dumps(p.attributes(real))
        for word in ("Nathaniel", "Reed", "Nate", "gmail", "Rectory", "Guildford",
                     "peanut", "867", "12401"):
            self.assertNotIn(word, rendered, f"{word!r} survived into {rendered}")

    def test_an_unclassified_attribute_is_redacted_not_passed_through(self):
        """The failure mode when PCO adds a field has to be redaction."""
        p = P()
        self.assertEqual(fields.kind_of("some_field_invented_next_year"), fields.OPAQUE)
        out = p.value("some_field_invented_next_year", "whatever it holds")
        self.assertTrue(out.startswith(values.REDACTED_PREFIX), out)
        self.assertNotIn("whatever", out)

    def test_free_text_is_never_fabricated(self):
        """A plausible-looking medical note is worse than an obvious placeholder."""
        out = P().value("medical_notes", "Carries an EpiPen")
        self.assertTrue(out.startswith(values.REDACTED_PREFIX), out)
        self.assertNotIn("EpiPen", out)

    def test_identifiers_that_point_outside_pco_are_redacted(self):
        p = P()
        for attribute in ("remote_id", "login_identifier", "stripe_customer_identifier"):
            out = p.value(attribute, "abc-123")
            self.assertTrue(out.startswith(values.REDACTED_PREFIX), out)
            self.assertNotIn("abc-123", out)

    def test_every_classified_name_is_covered_by_a_generator(self):
        """A kind with no generator silently redacts; that must be deliberate."""
        handled = set(values._GENERATORS) | {fields.KEEP, fields.OPAQUE, fields.FREE_TEXT}
        self.assertEqual(set(fields.BY_ATTRIBUTE.values()) - handled, set())


class TestTheEvidenceIsKept(unittest.TestCase):
    """The bug that started all this was `primary: true` against `primary: false`."""

    def test_booleans_numbers_and_nulls_pass_through(self):
        p = P()
        for attribute, raw in (("primary", True), ("primary", False), ("grade", 8),
                               ("child", True), ("member_count", 4), ("first_name", None)):
            self.assertIs(p.value(attribute, raw), raw)

    def test_timestamps_pass_through(self):
        """Staleness and divergence are told apart by `updated_at`."""
        stamp = "2026-07-28T15:06:28Z"
        self.assertEqual(P().value("updated_at", stamp), stamp)

    def test_ids_and_relationships_are_untouched(self):
        doc = {"data": {"id": "100", "type": "Person",
                        "attributes": {"first_name": "Dana"},
                        "relationships": {"households": {"data": [{"type": "Household",
                                                                   "id": "900"}]}}}}
        out = P().document(doc)
        self.assertEqual(out["data"]["id"], "100")
        self.assertEqual(out["data"]["relationships"], doc["data"]["relationships"])

    def test_an_empty_value_stays_empty(self):
        """'This field was blank' is a fact a pseudonym would erase."""
        self.assertEqual(P().value("first_name", ""), "")


class TestTheFakesAreBelievable(unittest.TestCase):
    def test_an_email_is_still_an_email(self):
        out = P().value("address", "nathaniel.reed@gmail.com")
        self.assertRegex(out, r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def test_a_phone_keeps_its_shape_and_its_dialling_code(self):
        out = P().value("number", "+1 (555) 867-5309")
        self.assertTrue(out.startswith("+1 "), out)
        self.assertEqual(re.sub(r"\d", "#", out), re.sub(r"\d", "#", "+1 (555) 867-5309"))

    def test_a_phone_with_no_country_code_keeps_its_digit_count(self):
        out = P().value("number", "555-867-5309")
        self.assertEqual(len(re.findall(r"\d", out)), 10)

    def test_a_date_is_still_a_date_in_the_same_year(self):
        out = P().value("birthdate", "2011-04-17")
        self.assertRegex(out, r"^2011-\d{2}-\d{2}$")
        self.assertNotEqual(out, "2011-04-17")

    def test_a_malformed_value_is_redacted_rather_than_repaired(self):
        """A malformed address is often the bug; inventing a valid one hides it."""
        p = P()
        for attribute, raw in (("address", "not-an-email"),
                               ("birthdate", "9999-99-99-nonsense"),
                               ("number", "no digits here")):
            out = p.value(attribute, raw)
            self.assertTrue(out.startswith(values.REDACTED_PREFIX), out)
            self.assertNotIn(raw, out)

    def test_the_pools_are_big_enough_to_be_varied(self):
        seen = {P().value("first_name", f"Person{i}") for i in range(400)}
        self.assertGreater(len(seen), 200, "too many collisions to read as a real roster")


class TestTheKey(unittest.TestCase):
    """Derived from the PCO credential — nothing minted, nothing extra to keep.

    Anyone holding the token can read the real records anyway, so a key derived
    from it guards exactly what it should. The useful consequence is that the
    mapping survives a rebuild: throw the database away, backfill again, and an
    old log still lines up with a new one.
    """

    def test_the_same_credential_always_gives_the_same_key(self):
        m, _ = build()
        self.assertEqual(pseudonym.secret_for(m.settings),
                         pseudonym.secret_for(m.settings))
        self.assertEqual(len(pseudonym.secret_for(m.settings)), 32)

    def test_it_survives_a_rebuilt_database(self):
        first, _ = build()
        second, _ = build()          # a different database, same credential
        self.assertEqual(pseudonym.secret_for(first.settings),
                         pseudonym.secret_for(second.settings))
        p1 = pseudonym.Pseudonymiser(pseudonym.secret_for(first.settings))
        p2 = pseudonym.Pseudonymiser(pseudonym.secret_for(second.settings))
        self.assertEqual(p1.value("last_name", "Reed"), p2.value("last_name", "Reed"))

    def test_a_different_organization_maps_differently(self):
        m, _ = build()
        other = type(m.settings)(**{**vars(m.settings), "pco_secret": "another-token"})
        self.assertNotEqual(pseudonym.secret_for(m.settings),
                            pseudonym.secret_for(other))

    def test_rotating_the_token_repseudonymises(self):
        """Not a bug, but the operator has to know: old logs stop comparing."""
        before = pseudonym.Pseudonymiser(pseudonym.derive_key("app:one"))
        after = pseudonym.Pseudonymiser(pseudonym.derive_key("app:two"))
        self.assertNotEqual(before.value("last_name", "Reed"),
                            after.value("last_name", "Reed"))

    def test_the_key_is_not_the_credential(self):
        """HMAC does not leak its key, but a key that *is* the PAT is one
        careless log line away from being the PAT."""
        token = "pco_pat_something_secret"
        self.assertNotIn(token.encode(), pseudonym.derive_key(token))

    def test_no_credential_still_works_for_a_dev_box(self):
        self.assertEqual(len(pseudonym.derive_key("")), 32)

    def test_it_is_not_derivable_from_the_pools(self):
        """Without the secret, a name pool is not a lookup table."""
        p = P()
        mapping = {name: p.value("first_name", name) for name in pools.FIRST_NAMES[:50]}
        other = P(OTHER_SECRET)
        agree = sum(1 for name, fake in mapping.items()
                    if other.value("first_name", name) == fake)
        self.assertLess(agree, 5, "two installs agreed far too often")


class TestWholeDocuments(unittest.TestCase):
    def test_a_document_is_copied_not_edited(self):
        doc = {"data": {"id": "1", "type": "Person", "attributes": {"first_name": "Dana"}}}
        P().document(doc)
        self.assertEqual(doc["data"]["attributes"]["first_name"], "Dana")

    def test_a_collection_and_its_includes_are_both_covered(self):
        doc = {"data": [{"id": "1", "type": "Person", "attributes": {"last_name": "Reed"}}],
               "included": [{"id": "e1", "type": "Email",
                             "attributes": {"address": "dana@gmail.com"}}],
               "meta": {"total_count": 1}}
        out = P().document(doc)
        self.assertNotIn("Reed", json.dumps(out))
        self.assertNotIn("gmail", json.dumps(out))
        self.assertEqual(out["meta"], {"total_count": 1})

    def test_a_shape_it_does_not_recognise_is_returned_unharmed(self):
        self.assertEqual(P().document({"errors": [{"code": "404"}]}),
                         {"errors": [{"code": "404"}]})


class TestARedactionIsStillComparable(unittest.TestCase):
    """A constant marker would make every hidden value equal to every other.

    These are the fields a divergence log can say least about, so the one
    question it must still answer for them is whether the two sides match. A
    mirror holding one medical note and PCO holding a different one has to read
    as different, or the report hides the very thing it was written to show.
    """

    def test_equal_values_tag_alike(self):
        p = P()
        self.assertEqual(p.value("medical_notes", "Carries an EpiPen"),
                         p.value("medical_notes", "Carries an EpiPen"))

    def test_different_values_tag_differently(self):
        p = P()
        self.assertNotEqual(p.value("medical_notes", "Carries an EpiPen"),
                            p.value("medical_notes", "Carries an inhaler"))

    def test_a_difference_of_case_is_a_difference(self):
        """Names fold case because PCO matches them that way; free text does not."""
        p = P()
        self.assertNotEqual(p.value("medical_notes", "EpiPen"),
                            p.value("medical_notes", "epipen"))
        self.assertEqual(p.value("first_name", "Dana"), p.value("first_name", "dana"))

    def test_the_same_text_tags_alike_across_fields(self):
        """So 'this value moved' and 'these records share one' stay visible."""
        p = P()
        self.assertEqual(p.value("medical_notes", "shared"),
                         p.value("a_field_nobody_classified", "shared"))

    def test_a_tag_does_not_carry_the_value(self):
        p = P()
        out = p.value("medical_notes", "Severe peanut allergy, carries an EpiPen")
        self.assertNotIn("peanut", out)
        self.assertNotIn("EpiPen", out)
        self.assertLess(len(out), 32, "a tag should not scale with what it replaced")

    def test_two_installs_tag_the_same_text_differently(self):
        """Keyed, so a short value out of a small space cannot be hashed back."""
        self.assertNotEqual(P(SECRET).value("medical_notes", "asthma"),
                            P(OTHER_SECRET).value("medical_notes", "asthma"))

    def test_a_divergence_in_a_redacted_field_survives_pseudonymisation(self):
        """End to end: two documents that differ must still differ afterwards."""
        p = P()
        mine = {"data": {"id": "1", "type": "Person",
                         "attributes": {"medical_notes": "Carries an EpiPen"}}}
        theirs = {"data": {"id": "1", "type": "Person",
                           "attributes": {"medical_notes": "Carries an inhaler"}}}
        self.assertNotEqual(p.document(mine), p.document(theirs))
        self.assertEqual(p.document(mine), p.document(json.loads(json.dumps(mine))))


if __name__ == "__main__":
    unittest.main()
