"""REAL[] columns read straight into float32 arrays, never as Python floats.

psycopg returns a REAL[] as a list of Python floats: 32 bytes of objects
for every 4-byte number, which numpy then copies out again. For one book
that is nothing. For the full UCSD catalogue's embeddings, 883,468 rows
of 384 numbers, it is about 11 GB on the way to a 1.36 GB matrix.

In PostgreSQL's binary format an array arrives as a short header and
then, for each element, its length (always 4 here) and its 4 bytes, so
numpy can read the numbers where they lie.
"""

from __future__ import annotations

import struct

import numpy as np
from psycopg.adapt import Loader
from psycopg.pq import Format

#: real[] in PostgreSQL's catalogue (the array type of float4); fixed.
FLOAT4_ARRAY_OID = 1021

_ELEMENT = np.dtype([("length", ">i4"), ("value", ">f4")])


def decode_float4_array(data) -> np.ndarray:
    """One REAL[] in binary wire format, as a float32 vector."""
    buf = memoryview(data)
    ndim, has_nulls, _element_oid = struct.unpack_from("!iiI", buf)
    if ndim == 0:
        return np.zeros(0, dtype=np.float32)
    if ndim != 1:
        raise ValueError(f"a vector has one dimension, not {ndim}")
    if has_nulls:
        raise ValueError("a vector cannot contain NULL")
    n, _lower_bound = struct.unpack_from("!ii", buf, 12)
    elements = np.frombuffer(buf, dtype=_ELEMENT, count=n, offset=20)
    if not (elements["length"] == 4).all():
        raise ValueError("not an array of 4-byte reals")
    return elements["value"].astype(np.float32)


class Float4ArrayLoader(Loader):
    format = Format.BINARY

    def load(self, data) -> np.ndarray:
        return decode_float4_array(data)


def vector_cursor(conn, name: str = ""):
    """A cursor that returns REAL[] columns as float32 arrays.

    Binary, so every other column comes back as psycopg normally gives
    it. With a name it is a server-side cursor, which hands rows over a
    page at a time instead of all at once; it must be used inside a
    transaction.
    """
    cur = conn.cursor(name, binary=True) if name else conn.cursor(binary=True)
    cur.adapters.register_loader(FLOAT4_ARRAY_OID, Float4ArrayLoader)
    return cur
