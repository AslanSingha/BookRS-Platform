"""MARC flavour detection.

A Koha instance configured for UNIMARC serves UNIMARC records under a
metadata prefix named ``marc21``, in the ``MARC21/slim`` XML namespace.
Neither the prefix nor the namespace identifies what is actually being
served, so flavour must be determined from record structure.

This matters because several tag numbers exist in both standards with
different meanings -- 100 (main author / general processing data),
300 (physical description / general note) and 020 (ISBN / country of
publication).  Applying the wrong field map does not raise an error; it
silently yields plausible-looking nonsense, such as the string
``"20070130              frey50        "`` as a book's author.

See docs/marc-field-analysis.md sections 10.1 and 10.2.
"""

from __future__ import annotations

from enum import Enum
from xml.etree import ElementTree as ET

MARCXML_NS = "http://www.loc.gov/MARC21/slim"
OAI_NS = "http://www.openarchives.org/OAI/2.0/"

# Title is the discriminator: present in 100% of records in both
# flavours, and the tag numbers do not overlap.
MARC21_TITLE_TAG = "245"
UNIMARC_TITLE_TAG = "200"


class Flavour(str, Enum):
    MARC21 = "MARC21"
    UNIMARC = "UNIMARC"


class FlavourDetectionError(Exception):
    """Raised when a record's flavour cannot be determined.

    Deliberately fatal.  Guessing here produces silently corrupt data
    rather than a visible failure, so an unrecognisable record must stop
    the harvest and be reported.
    """


def _diagnose_non_xml(xml: bytes | str) -> str:
    """Best-effort explanation for a response that is not XML.

    Koha's ``oai.pl`` returns a plain-text "OAI-PMH service is disabled"
    body when the ``OAI-PMH`` system preference is off, but Apache's
    ErrorDocument handler replaces it with the full OPAC error page --
    so the observable signature of a disabled endpoint is a 404 carrying
    tens of kilobytes of HTML.  An XML parser reports only "mismatched
    tag: line 1", which tells an operator nothing useful.

    See docs/marc-field-analysis.md section 2.2.
    """
    body = xml if isinstance(xml, bytes) else xml.encode("utf-8", "replace")
    head = body[:2048].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")) or b"<html" in head:
        return (
            "The response is an HTML page, which usually means OAI-PMH is not "
            "enabled on this instance (ask the administrator to enable the "
            "'OAI-PMH' system preference), or the URL points somewhere other "
            "than the OAI endpoint."
        )
    if not body.strip():
        return "The response body is empty."
    return f"First bytes: {body[:120]!r}"


def _diagnose_no_marc(root: ET.Element) -> str:
    """Explain a response that parsed but held no MARCXML.

    The likeliest cause is a repository serving a format this adapter
    does not read. PMB, for instance, offers "UNIMARC PMB XML" and
    Dublin Core rather than MARC21slim -- both valid OAI-PMH, neither
    parseable here. Reporting "no MARCXML records" alone would send an
    operator looking for a fault in their catalogue rather than telling
    them the format is unsupported.
    """
    error = root.find(f"{{{OAI_NS}}}error")
    if error is not None:
        return (f"The repository returned an OAI-PMH error "
                f"[{error.get('code', 'unknown')}]: {(error.text or '').strip()}")

    # What namespaces did the metadata actually use?
    namespaces = sorted({
        el.tag.split("}")[0].lstrip("{")
        for parent in root.iter(f"{{{OAI_NS}}}metadata")
        for el in parent
        if el.tag.startswith("{")
    })
    if not namespaces:
        headers = root.findall(f".//{{{OAI_NS}}}header")
        if headers and all(h.get("status") == "deleted" for h in headers):
            return ("Every record in this response is a deleted-record "
                    "header, which carries no metadata.")
        return "No metadata elements were present in the response."

    return (
        f"The metadata is in {', '.join(namespaces)} rather than "
        f"{MARCXML_NS}. This adapter reads MARC21slim only, which Koha "
        f"serves under the marc21 and marcxml prefixes. Check "
        f"ListMetadataFormats for a MARC option; a repository offering "
        f"only Dublin Core or a vendor-specific schema is not yet "
        f"supported."
    )


def _datafield_tags(marc_record: ET.Element) -> set[str]:
    return {
        tag
        for el in marc_record.findall(f"{{{MARCXML_NS}}}datafield")
        if (tag := el.get("tag")) is not None
    }


def detect_flavour(marc_record: ET.Element) -> Flavour:
    """Determine the MARC flavour of a single parsed ``<record>`` element.

    Expects the MARCXML record, not the OAI envelope.  Note that
    ``<record>`` appears in both namespaces, so callers must select the
    MARCXML one.
    """
    tags = _datafield_tags(marc_record)
    has_marc21 = MARC21_TITLE_TAG in tags
    has_unimarc = UNIMARC_TITLE_TAG in tags

    if has_marc21 and has_unimarc:
        raise FlavourDetectionError(
            f"Record contains both {MARC21_TITLE_TAG} and "
            f"{UNIMARC_TITLE_TAG}; flavour is ambiguous."
        )
    if has_marc21:
        return Flavour.MARC21
    if has_unimarc:
        return Flavour.UNIMARC
    raise FlavourDetectionError(
        f"Record has neither {MARC21_TITLE_TAG} (MARC21) nor "
        f"{UNIMARC_TITLE_TAG} (UNIMARC); cannot determine flavour. "
        f"Tags present: {sorted(tags) or 'none'}"
    )


def detect_flavour_from_response(xml: bytes | str, sample_size: int = 5) -> Flavour:
    """Determine flavour from a ``ListRecords`` or ``GetRecord`` response.

    Samples several records rather than trusting the first, since a
    single malformed record should not decide the flavour for an entire
    harvest.  Disagreement between records is fatal: a mixed-flavour feed
    needs per-record handling, which this function cannot express.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FlavourDetectionError(
            f"Response is not well-formed XML ({exc}). {_diagnose_non_xml(xml)}"
        ) from exc

    marc_records = root.findall(
        f".//{{{OAI_NS}}}record/{{{OAI_NS}}}metadata/{{{MARCXML_NS}}}record"
    )
    if not marc_records:
        raise FlavourDetectionError(
            "Response contains no MARCXML records. "
            + _diagnose_no_marc(root)
        )

    seen: dict[Flavour, int] = {}
    errors: list[str] = []
    for record in marc_records[:sample_size]:
        try:
            flavour = detect_flavour(record)
            seen[flavour] = seen.get(flavour, 0) + 1
        except FlavourDetectionError as exc:
            errors.append(str(exc))

    if not seen:
        raise FlavourDetectionError(
            "No sampled record yielded a flavour. First error: "
            + (errors[0] if errors else "unknown")
        )
    if len(seen) > 1:
        raise FlavourDetectionError(
            f"Sampled records disagree on flavour: "
            f"{ {f.value: n for f, n in seen.items()} }. A mixed-flavour "
            f"feed requires per-record handling."
        )
    return next(iter(seen))
