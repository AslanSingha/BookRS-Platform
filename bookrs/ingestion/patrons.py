"""Patron identifiers, made pseudonymous.

Patron identifiers are never stored. Each one is replaced by an HMAC
keyed on a per-deployment secret (BOOKRS_PATRON_SECRET), so the same
patron always maps to the same reference and the factorisation works
identically, while this service holds nothing that identifies a person.
The Koha plugin computes the same reference from the patron's session,
so the platform can answer for a patron it cannot name.

This is pseudonymisation, not anonymisation: anyone holding both the
secret and the library's own database could re-identify. It removes
casual exposure and bounds the damage from a leak of this database
alone.
"""

from __future__ import annotations

import hmac
from hashlib import sha256


def patron_reference(patron_id: int | str, secret: str) -> str:
    """Map a patron identifier to an opaque, stable reference.

    HMAC rather than a plain hash: a bare SHA-256 of a small integer
    space is trivially reversible by enumeration, since a library has
    at most a few hundred thousand patrons and computing every hash
    takes seconds.
    """
    digest = hmac.new(secret.encode("utf-8"), str(patron_id).encode("utf-8"), sha256)
    return digest.hexdigest()[:32]
