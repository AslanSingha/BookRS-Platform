"""Patron pseudonymisation."""

from bookrs.ingestion.patrons import patron_reference


class TestPatronReference:
    def test_stable_within_a_deployment(self):
        assert patron_reference(42, "secret") == patron_reference(42, "secret")

    def test_differs_across_deployments(self):
        """Otherwise references would be correlatable between libraries,
        which is the point of keying the hash."""
        assert patron_reference(42, "a") != patron_reference(42, "b")

    def test_differs_between_patrons(self):
        assert patron_reference(1, "s") != patron_reference(2, "s")

    def test_accepts_string_or_int(self):
        assert patron_reference(42, "s") == patron_reference("42", "s")

    def test_is_opaque_hex(self):
        ref = patron_reference(42, "s")
        assert len(ref) == 32
        assert all(c in "0123456789abcdef" for c in ref)

    def test_does_not_contain_the_identifier(self):
        assert "123456" not in patron_reference(123456, "s")
