# BookRS-Platform

> A recommendation service for libraries running open-source ILS
> platforms — Koha, PMB, and other MARC / OAI-PMH-compliant systems.

**Status: early development.** Schema and ingestion architecture are
being validated against real Koha instances before implementation
begins. No application code has been written yet.

---

## What this is

BookRS-Platform is **software a library self-hosts**, not a service
operated by a third party. A library's IT staff clone this repository,
point it at their own ILS, and run it on their own infrastructure via
Docker Compose. Catalogue data never leaves the library's control.

The service harvests bibliographic records from the library's existing
system over OAI-PMH, builds semantic and collaborative recommendation
models from them, and exposes recommendations through a small
JavaScript widget embedded in the library's public catalogue (OPAC).

**Integration is read-only.** BookRS-Platform is never given write —
or even direct read — access to a library's production database. This
is the same integration pattern used by established products in this
space.

## Prior work

Building on research and findings from
**[BookRS-System](https://github.com/AslanSingha/BookRS-System)** —
RIN SINGH's Engineering thesis at the Institute of Technology of
Cambodia (2026), supervised by M. SOK Kimheng. That system combined
SBERT semantic embeddings with ALS collaborative filtering.

BookRS-Platform is a **separate codebase**, with its own schema,
integration layer and deployment model.

## Differences from BookRS-System

| | BookRS-System | BookRS-Platform |
|---|---|---|
| Data source | Static research dataset | Live ILS via OAI-PMH |
| Schema | Books table | Bibliographic / item separation |
| Signals | Explicit ratings | Circulation signals, then ratings |
| Record format | Normalised CSV | MARC21 and UNIMARC |
| Deployment | Local development | Docker Compose, self-hosted |

**Bibliographic / item separation.** Real library data needs two
layers: a `works` record (the intellectual work — title, authors,
subject headings, one embedding, one factor row) and `items` records
(physical copies — branch, call number, availability). Multiple copies
of a book are real inventory, not duplicates to merge.

**Two sources of preference signal.** Circulation records that a book
was borrowed, not whether it was enjoyed, so implicit signals
(checkouts, renewals, holds) are weighted through an extended
confidence formula. Koha additionally provides a native patron star
rating feature, enabled by default, storing
`(borrowernumber, biblionumber, rating_value)` triples. Where a
library's patrons use it, those ratings supply the preference
direction circulation data cannot express. How much rating data
exists varies by deployment and is measured at setup rather than
assumed.

**Graceful degradation on sparse metadata.** Many catalogue records
carry little beyond a title. The content-based layer is designed
around that constraint rather than assuming rich descriptions.

## Evidence base

Design decisions here are grounded in measurements against running
Koha instances rather than documentation alone:

**[`docs/marc-field-analysis.md`](docs/marc-field-analysis.md)** —
field coverage, identifier semantics, holdings structure and
circulation behaviour, measured across a MARC21 instance (436 records)
and a UNIMARC instance (4,849 records).

Findings that directly shape the architecture:

- **Metadata prefix and XML namespace do not identify MARC flavour.**
  A UNIMARC Koha serves UNIMARC records under a prefix named `marc21`,
  in the `MARC21/slim` namespace. Flavour must be detected from record
  structure.
- **Tag numbers collide between standards.** `100`, `300` and `020`
  exist in both MARC21 and UNIMARC with entirely different meanings.
  A MARC21 field map applied to a UNIMARC feed produces plausible
  nonsense rather than an error.
- **Roughly a third of records have nothing but a title to embed** —
  65.4% of MARC21 and 61.5% of UNIMARC records carry any summary,
  subject heading or contents note.
- **Circulation activity drives incremental sync volume**, not
  cataloguing. A checkout updates a record's OAI datestamp while
  leaving its bibliographic content unchanged.
- **Patron ratings exist but are not in the MARC record.** Koha stores
  them in its own tables, so reaching them requires a second read
  channel alongside OAI-PMH.

## Architecture (planned)

```
bookrs-ingestion   OAI-PMH harvest, MARC parsing, scheduled sync
                   + REST read channel for patron ratings
bookrs-api         FastAPI — recommendations, search, availability
bookrs-db          PostgreSQL — works, items, interactions
bookrs-widget      Embeddable JS for the library's OPAC
```

## Roadmap

1. MARC21 / UNIMARC ingestion adapter (OAI-PMH)
2. Bibliographic / item schema and field-mapping layer
3. Embedding layer (English-language catalogues first)
4. Extended implicit-signal confidence formula
5. OPAC widget; reading Koha's native ratings via REST
6. Scheduled retraining
7. Exact / keyword search endpoint
8. Production hardening — once a pilot library exists

## License

GNU General Public License v3.0 — matching Koha and the wider
open-source ILS ecosystem this integrates with.

---

<div align="center">
  <strong>BookRS-Platform</strong> · 2026<br>
  RIN SINGH · Institute of Technology of Cambodia
</div>
