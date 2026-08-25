"""Tests for circulation harvesting and patron pseudonymisation."""

from datetime import datetime

import pytest

from bookrs.ingestion.circulation import (
    CirculationConfig,
    CirculationError,
    Loan,
    patron_reference,
)


def _cfg(**kw) -> CirculationConfig:
    defaults = dict(base_url="http://koha.test", username="u", password="p",
                    patron_secret="s3cret")
    defaults.update(kw)
    return CirculationConfig(**defaults)


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


class TestConfig:
    def test_secret_is_required(self):
        """A shared default would make every deployment's references
        identical, defeating the purpose."""
        with pytest.raises(CirculationError, match="patron_secret"):
            _cfg(patron_secret="")

    def test_valid_config_constructs(self):
        assert _cfg().page_size == 100


class TestLoan:
    def _loan(self, **kw) -> Loan:
        defaults = dict(loan_id=1, patron_ref="abc", biblio_id=42, item_id=7,
                        checkout_date=None, checkin_date=None)
        defaults.update(kw)
        return Loan(**defaults)

    def test_current_when_not_checked_in(self):
        assert self._loan().is_current

    def test_not_current_once_returned(self):
        assert not self._loan(checkin_date=datetime(2026, 1, 2)).is_current

    def test_days_held(self):
        loan = self._loan(checkout_date=datetime(2026, 1, 1),
                          checkin_date=datetime(2026, 1, 15))
        assert loan.days_held == pytest.approx(14.0)

    def test_days_held_unknown_while_on_loan(self):
        assert self._loan(checkout_date=datetime(2026, 1, 1)).days_held is None


class TestRowConversion:
    """A row missing patron, work or loan id cannot be used."""

    def _row(self, **kw) -> dict:
        row = {"checkout_id": 5, "patron_id": 3, "item_id": 9,
               "checkout_date": "2026-01-01T00:00:00+00:00",
               "checkin_date": None, "renewals_count": 2,
               "item": {"biblio": {"biblio_id": 77}}}
        row.update(kw)
        return row

    def test_full_row_converts(self):
        from bookrs.ingestion.circulation import _to_loan
        loan = _to_loan(self._row(), "s")
        assert loan.loan_id == 5 and loan.biblio_id == 77 and loan.renewals == 2

    def test_loan_id_is_not_the_item_id(self):
        """A copy is borrowed many times. Keying on item_id would make
        two patrons' loans of the same copy collide."""
        from bookrs.ingestion.circulation import _to_loan
        loan = _to_loan(self._row(checkout_id=5, item_id=9), "s")
        assert loan.loan_id == 5 and loan.item_id == 9

    @pytest.mark.parametrize("missing", ["checkout_id", "patron_id"])
    def test_missing_required_field_is_dropped(self, missing):
        from bookrs.ingestion.circulation import _to_loan
        assert _to_loan(self._row(**{missing: None}), "s") is None

    def test_missing_biblio_is_dropped(self):
        from bookrs.ingestion.circulation import _to_loan
        assert _to_loan(self._row(item={}), "s") is None
