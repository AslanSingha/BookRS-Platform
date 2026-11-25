"""Tests for the OPAC widget.

This is the only code in the project a patron reads, and its heading
makes a conditional claim about the library's own readers. "Readers
also borrowed" is a statement about real people's behaviour; where no
result carries circulation evidence it is false, and a patron has no way
to check it. Where the evidence is star ratings it is false too: the
heading must then name ratings, and claim nothing when the API does not
say what the evidence is.

Run through a stub DOM (``widget_harness.js``) so the assertions are
about what a patron sees rather than about an extracted helper. No npm
dependency -- the harness implements the small fixed part of the DOM the
widget touches. Where node is unavailable these skip; the widget is then
untested rather than wrongly reported as passing.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "widget_harness.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not available; widget rendering is not exercised",
)

# The API's collaborative_signal left out of the answer altogether, as
# an API from before the field existed sends it. Distinct from None,
# which the API sends (as null) when the refit recorded nothing.
ABSENT = object()


def render(results, attributes=None, evidence=ABSENT):
    """Run the real widget against stubbed responses, return the panel.

    evidence is the similar-books answer's collaborative_signal: what the
    factors were learned from ("loans", "ratings"), None, or ABSENT.
    """
    request = {
        "attributes": {"data-api": "http://example.test", **(attributes or {})},
        "results": results,
    }
    if evidence is not ABSENT:
        request["collaborative_signal"] = evidence
    out = subprocess.run(
        ["node", str(HARNESS), json.dumps(request)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


def work(signal="content", title="A title"):
    return {"title": title, "signal": signal, "authors": [],
            "biblionumber": "1", "availability": {}}


class TestHeadingHonesty:
    """The borrowing claim must be earned by every result shown."""

    def test_no_circulation_evidence_does_not_claim_borrowing(self):
        panel = render([work("content"), work("content")], evidence="loans")
        assert panel["heading"] == "Related in this catalogue"

    def test_all_results_backed_by_circulation_claims_borrowing(self):
        panel = render([work("hybrid"), work("hybrid")], evidence="loans")
        assert panel["heading"] == "Readers also borrowed"

    def test_mixed_panel_does_not_claim_borrowing(self):
        """One result with evidence does not license a heading that
        speaks for all of them."""
        panel = render([work("hybrid"), work("content")], evidence="loans")
        assert panel["heading"] == "Related in this catalogue"

    def test_single_unbacked_result(self):
        panel = render([work("content")])
        assert panel["heading"] == "Related in this catalogue"

    def test_missing_signal_field_does_not_claim_borrowing(self):
        """An older API, or a field that stops being sent, must fail
        toward the weaker claim rather than the stronger one."""
        panel = render([{"title": "A", "authors": [], "biblionumber": "1",
                         "availability": {}}], evidence="loans")
        assert panel["heading"] == "Related in this catalogue"


class TestEvidenceIsNamed:
    """What readers did is what the factors were learned from. Factors
    learned from ratings back the same results as factors learned from
    loans, and "Readers also borrowed" over them claims borrowing that
    never happened."""

    def test_factors_from_ratings_say_rated_not_borrowed(self):
        panel = render([work("hybrid"), work("hybrid")], evidence="ratings")
        assert panel["heading"] == "Readers who rated this also rated"

    def test_a_mixed_panel_over_ratings_claims_nothing_about_readers(self):
        panel = render([work("hybrid"), work("content")], evidence="ratings")
        assert panel["heading"] == "Related in this catalogue"

    def test_unrecorded_evidence_claims_neither(self):
        """Factors from a refit that did not record its signal (null):
        the page cannot know which claim is true, so it makes neither."""
        panel = render([work("hybrid"), work("hybrid")], evidence=None)
        assert panel["heading"] == "Related in this catalogue"

    def test_an_answer_without_the_field_claims_neither(self):
        """An API from before the field existed sends hybrid results
        with no word on what backs them."""
        panel = render([work("hybrid"), work("hybrid")])
        assert panel["heading"] == "Related in this catalogue"

    def test_unknown_evidence_claims_neither(self):
        panel = render([work("hybrid"), work("hybrid")], evidence="clicks")
        assert panel["heading"] == "Related in this catalogue"


class TestLibraryOverride:
    """A library that has chosen its own wording knows its own
    catalogue, and keeps the last word."""

    def test_explicit_heading_wins_over_semantic_default(self):
        panel = render([work("content")], {"data-heading": "Our picks"})
        assert panel["heading"] == "Our picks"

    def test_explicit_heading_wins_over_borrowing_default(self):
        panel = render([work("hybrid")], {"data-heading": "Our picks"},
                       evidence="loans")
        assert panel["heading"] == "Our picks"

    def test_explicit_heading_wins_over_rating_default(self):
        panel = render([work("hybrid")], {"data-heading": "Our picks"},
                       evidence="ratings")
        assert panel["heading"] == "Our picks"


class TestPerResultMarking:
    def test_backed_results_are_marked(self):
        panel = render([work("hybrid")], evidence="loans")
        assert panel["markers"] == ["Borrowed together"]

    def test_unbacked_results_are_not_marked(self):
        panel = render([work("content")], evidence="loans")
        assert panel["markers"] == [None]

    def test_only_the_backed_result_is_marked(self):
        panel = render([work("hybrid"), work("content")], evidence="loans")
        assert panel["markers"] == ["Borrowed together", None]

    def test_rating_evidence_is_marked_as_ratings(self):
        panel = render([work("hybrid"), work("content")], evidence="ratings")
        assert panel["markers"] == ["Readers rated both", None]

    def test_unrecorded_evidence_marks_nothing(self):
        assert render([work("hybrid")], evidence=None)["markers"] == [None]
        assert render([work("hybrid")])["markers"] == [None]


class TestSilence:
    def test_no_results_renders_nothing(self):
        """A missing panel is a disappointment; an empty panel on a
        library's catalogue is a defect."""
        assert render([])["rendered"] is False
