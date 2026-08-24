"""Translate PMB's UNIMARC XML into MARCXML.

PMB serves records under the ``pmb_xml_unimarc`` metadata prefix in its
own XML shape rather than MARC21slim. The structure is the same
information under different element names, so translating it lets the
rest of the pipeline -- flavour detection, the UNIMARC field map,
language detection, item extraction -- work unchanged. The tag numbers
are identical UNIMARC.

PMB emits::

    <notice>
      <rs>c</rs><dt>a</dt><bl>m</bl><hl>0</hl><el>1</el><ru>*</ru>
      <f c="001">12345</f>
      <f c="200" ind="1 "><s c="a">Titre</s></f>
    </notice>

Four differences from MARCXML, each verified against PMB's own
``admin/convert/xml_unimarc.class.php``:

* ``notice`` / ``f`` / ``s`` rather than ``record`` / ``datafield`` /
  ``subfield``, and ``c`` rather than ``tag`` / ``code``.
* **No control-field element.** A control field is an ``<f>`` carrying
  text with no ``<s>`` children; a data field is an ``<f>`` with them.
  The distinction is by content, not by element name.
* **Indicators are a single ``ind`` attribute** holding both characters,
  not separate ``ind1`` and ``ind2``.
* **No leader.** Six named elements carry what a leader would:
  ``rs`` record status, ``dt`` type, ``bl`` bibliographic level,
  ``hl`` hierarchic level, ``el`` encoding level, ``ru``. These are
  reassembled into a leader-shaped string so that code reading
  ``leader[6]`` and ``leader[7]`` -- record type and bibliographic
  level, which distinguish books from sound recordings and serials --
  keeps working.
* **No XML namespace at all**, so there is nothing to map; the output is
  placed in the MARC21slim namespace the rest of the pipeline expects.
"""

from __future__ import annotations

import logging
from xml.etree import ElementTree as ET

MARCXML_NS = "http://www.loc.gov/MARC21/slim"
OAI_NS = "http://www.openarchives.org/OAI/2.0/"

# The prefix PMB advertises for this format.
PMB_PREFIX = "pmb_xml_unimarc"

# Leader positions PMB supplies, by name. Position numbers follow
# UNIMARC's record label.
_LEADER_POSITIONS = {
    "rs": 5,   # record status
    "dt": 6,   # type of record
    "bl": 7,   # bibliographic level
    "hl": 8,   # hierarchic level
    "el": 17,  # encoding level
}

log = logging.getLogger(__name__)


def _local(tag: str) -> str:
    """Element name without its namespace."""
    return tag.rpartition("}")[2]


def _children(parent: ET.Element, name: str) -> list[ET.Element]:
    """Direct children with this local name, namespaced or not.

    PMB writes its records without a namespace declaration, but they sit
    inside an OAI envelope whose default xmlns applies to every
    unprefixed descendant. Whether a parser sees ``notice`` or
    ``{OAI}notice`` therefore depends on how the repository serialises
    the envelope -- and both occur. Matching on the local name is
    correct either way.
    """
    return [child for child in parent if _local(child.tag) == name]


def looks_like_pmb(root: ET.Element) -> bool:
    """True when a parsed OAI response carries PMB records.

    Detected by structure rather than by metadata prefix, for the same
    reason flavour is: a prefix name is a label a repository chooses,
    and Koha already demonstrates that the label can be wrong.
    """
    for element in root.iter():
        if _local(element.tag) != "notice":
            continue
        if _children(element, "f"):
            return True
    return False


def _leader(notice: ET.Element) -> str:
    """Rebuild a leader from PMB's named elements.

    Only the positions PMB provides are filled; the rest are spaces.
    PMB writes ``*`` where the underlying byte was a space, so that is
    translated back.
    """
    leader = [" "] * 24
    for name, position in _LEADER_POSITIONS.items():
        found = _children(notice, name)
        if not found or not found[0].text:
            continue
        value = found[0].text.strip()
        leader[position] = " " if value == "*" else value[0]
    return "".join(leader)


def _translate_field(field: ET.Element, target: ET.Element) -> None:
    tag = field.get("c")
    if not tag:
        return

    subfields = _children(field, "s")
    if not subfields:
        # An <f> with no <s> children is a control field. PMB makes no
        # element-level distinction, so this is the only way to tell.
        control = ET.SubElement(target, f"{{{MARCXML_NS}}}controlfield")
        control.set("tag", tag)
        control.text = field.text or ""
        return

    datafield = ET.SubElement(target, f"{{{MARCXML_NS}}}datafield")
    datafield.set("tag", tag)

    # A single ind attribute holds both indicator characters. Missing or
    # short values pad with spaces, which is what MARC uses for "no
    # indicator" and keeps downstream attribute reads total.
    indicators = (field.get("ind") or "  ").ljust(2)
    datafield.set("ind1", indicators[0])
    datafield.set("ind2", indicators[1])

    for subfield in subfields:
        code = subfield.get("c")
        if not code:
            continue
        element = ET.SubElement(datafield, f"{{{MARCXML_NS}}}subfield")
        element.set("code", code)
        element.text = subfield.text or ""


def translate_notice(notice: ET.Element) -> ET.Element:
    """Convert one PMB ``<notice>`` into a MARCXML ``<record>``."""
    record = ET.Element(f"{{{MARCXML_NS}}}record")
    leader = ET.SubElement(record, f"{{{MARCXML_NS}}}leader")
    leader.text = _leader(notice)
    for field in _children(notice, "f"):
        _translate_field(field, record)
    return record


def translate_response(root: ET.Element) -> ET.Element:
    """Rewrite PMB metadata in place, in an OAI response.

    The OAI envelope is left alone -- identifiers, datestamps, deleted
    status and resumption tokens are protocol-level and already
    standard. Only the contents of each ``<metadata>`` element change.
    """
    translated = 0
    for metadata in root.iter(f"{{{OAI_NS}}}metadata"):
        notices = _children(metadata, "notice")
        if not notices:
            continue
        for notice in notices:
            metadata.remove(notice)
            metadata.append(translate_notice(notice))
            translated += 1
    if translated:
        log.debug("translated %d PMB notices to MARCXML", translated)
    return root
