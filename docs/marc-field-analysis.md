# MARC / OAI-PMH Field Analysis — Koha (MARC21 and UNIMARC)

**Evidence base for BookRS-Platform schema and ingestion design.**

Every number in this document was measured against running Koha
instances on 2026-08-23. Nothing here is inferred from documentation
alone; where documentation and observed behaviour disagree, the
disagreement is recorded explicitly.

Sections 2–9 describe a **MARC21** instance. Section 10 describes a
parallel **UNIMARC** instance and the cross-flavour findings, which
include the highest-severity issue in this document (§10.2).
Section 11 covers patron ratings, which require a separate read
channel from OAI-PMH. Section 12 records operational findings that arose
from mistakes made while building against these instances — several of
which produced plausible output rather than errors.

---

## 1. Provenance

| Component | Version / identifier |
|---|---|
| Host | WSL2 Ubuntu 26.04 on Windows 11 |
| Docker | 29.7.2 |
| Docker Compose | v5.4.0 (KTD requires ≥ 2.33.1) |
| koha-testing-docker | commit `1f48176`, branch `main`, 2026-08-10 |
| Koha source | commit `138d2b6`, branch `main`, 2026-03-27 (shallow clone) |
| Container images | `koha/koha-testing:main`, `mariadb:11.8`, `memcached` |
| Search engine | none (base stack; Elasticsearch/OpenSearch not enabled) |
| Proxy (for parallel instances) | `traefik:v3.7` via `ktd_proxy`, host port 80 |

Two instances were run in parallel from the same images:

| Instance | Flavour | Address | Corpus |
|---|---|---|---|
| `kohadev` | MARC21 | `localhost:8080` (OPAC), `:8081` (staff) | 436 records, 961 items |
| `unimarc` | UNIMARC | `unimarc.localhost`, `unimarc-intra.localhost` | 4,849 records |

The two sample corpora are **different sets of records**, not
translations of each other (`earliestDatestamp` 2014-05-07 vs
2016-06-15). Coverage percentages therefore describe different books
and are not directly comparable; the comparison here is of field
*structure*, not catalogue quality.

Timings, for deployment-effort estimation: image pull **8m17s**;
first-run provisioning to ready **88s** (`ktd --wait-ready`).

### Reproducing this

```bash
mkdir -p ~/projects && cd ~/projects
git clone https://gitlab.com/koha-community/koha-testing-docker.git
git clone --branch main --single-branch --depth 1 \
    https://git.koha-community.org/Koha-community/Koha.git koha

# env vars: PROJECTS_DIR, SYNC_REPO, KTD_HOME, PATH+=$KTD_HOME/bin, LOCAL_USER_ID
cp $KTD_HOME/env/defaults.env $KTD_HOME/.env
ktd pull && ktd up -d && ktd --wait-ready 600
```

Current `ktd` includes `compose/local_ports.yml` by default (see
`bin/ktd` line 606), so `localhost:8080` (OPAC) and `localhost:8081`
(staff) are published to the host. The README's container-IP discovery
procedure is **outdated**.

---

## 2. OAI-PMH endpoint behaviour

### 2.1 Endpoint and activation

- Path: `/cgi-bin/koha/oai.pl` on the OPAC vhost.
- The `OAI-PMH` system preference defaults to **`0`** (off).
- Enabling it: `C4::Context->set_preference('OAI-PMH', 1)`. Note that
  Koha caches preferences in memcached — a direct SQL `UPDATE` on
  `systempreferences` leaves the cache stale and the endpoint keeps
  returning 404.

### 2.2 Disabled-state signature — adapter requirement

`opac/oai.pl` returns a deliberate diagnostic when the preference is
off:

```perl
unless ( C4::Context->preference('OAI-PMH') ) {
    print header(
        -type   => 'text/plain; charset=utf-8',
        -status => '404 OAI-PMH service is disabled',
    ), "OAI-PMH service is disabled";
    exit;
}
```

**Observed behaviour differs from the source's intent.** The client
receives:

| | Script intends | Actually observed |
|---|---|---|
| Status | 404 | 404 |
| Content-Type | `text/plain` | **`text/html`** |
| Body length | 27 bytes | **28,678 bytes** |
| Body | "OAI-PMH service is disabled" | Full Koha OPAC error page |

Apache's `ErrorDocument` handler substitutes its own body for the 404,
discarding the developers' message.

> **Adapter requirement.** Check status code *and* content-type before
> parsing. On `404 + text/html`, report *"OAI-PMH appears to be disabled
> on this Koha instance; ask the administrator to enable the `OAI-PMH`
> system preference"* rather than surfacing an XML parse error. The
> useful diagnostic never reaches the wire.

### 2.3 `Identify` response

```
repositoryName      (empty)
baseURL             http://localhost:8080/opac/oai.pl
protocolVersion     2.0
adminEmail          root@localhost
earliestDatestamp   2014-05-07T13:36:23Z
deletedRecord       persistent
granularity         YYYY-MM-DDThh:mm:ssZ
compression         gzip
```

**The advertised `baseURL` is wrong.** `/opac/oai.pl` returns HTTP 404;
only `/cgi-bin/koha/oai.pl` serves. The same incorrect URL is echoed in
every `<request>` element. A spec-conforming harvester that trusts
`baseURL` for subsequent requests **fails against this instance**.

> **Adapter requirement.** Always use the operator-configured URL. Never
> re-derive the endpoint from `baseURL` or `<request>`.

**Confirmed on both instances** — the UNIMARC instance advertises
`http://unimarc.localhost/opac/oai.pl` and serves only on
`/cgi-bin/koha/oai.pl`. So this is not a per-instance misconfiguration.
Whether it also affects production deployments (where `OPACBaseURL` is
normally set) is untested; the defensive behaviour is the same either
way.

Also of note: `repositoryName` is empty and `adminEmail` is a
placeholder. The adapter must not depend on either being meaningful.

`granularity` is second-level, so incremental `from`/`until` can use
full timestamps. `deletedRecord: persistent` means deletions are
tracked permanently and **will appear in incremental harvests** — the
ingestion design must handle deleted-record headers, not only
additions and updates.

### 2.4 Pagination

`OAI-PMH:MaxCount = 50`. The full corpus harvested in **9 pages**
(8 × 50 + 1 × 36 = 436).

Observed tokens:

```
marc21/50////0/0/51
marc21/100////0/0/101
marc21/150////0/0/152
...
marc21/400////0/0/403
(page 9: no resumptionToken element at all)
```

- **No `completeListSize` attribute.** Both `completeListSize` and
  `cursor` are optional per spec; Koha emits only `cursor`. The
  harvester **cannot know the total in advance** — no progress bar, no
  completion check against an expected count.
- **Termination is by absence of the token element**, not an empty one.
  Handle both; do **not** use `records < pageSize` as the signal, since
  a full final page is possible.
- The token is structurally readable (offset advances by exactly 50),
  but per spec it must be treated as opaque and echoed verbatim. The
  trailing value drifts (`51, 101, 152, 202, … 403`) — consistent with a
  biblionumber high-water mark, confirming **biblionumbers are not
  contiguous**.
- Tokens contain `/` and must be URL-encoded when sent as a query
  parameter.

### 2.5 Sets

`ListSets` returns `<error code='noSetHierarchy'>`. No sets are defined
by default (`OAI-PMH:AutoUpdateSets = 0`).

> **Adapter requirement.** Treat `noSetHierarchy` as a normal condition,
> not an error. Set-scoped harvesting is optional.

---

## 3. Metadata formats

Default (no `OAI-PMH:ConfFile`):

| Prefix | Schema |
|---|---|
| `oai_dc` | oai_dc.xsd |
| `marc21` | MARC21slim.xsd |
| `marcxml` | MARC21slim.xsd |

`marc21` and `marcxml` declare the **identical schema and namespace**.
On Koha they appear to be the same serialization under two names,
retained for backward compatibility.

### 3.1 Extended mode removes formats — documented gotcha

Setting `OAI-PMH:ConfFile` switches the server to extended mode, where
available formats are read from the YAML file. Neither of Koha's own
test fixtures lists `oai_dc`.

**Measured**: after pointing `ConfFile` at a conf declaring only
`marcxml` and `marc21`, `ListMetadataFormats` returned **two** formats.
`oai_dc` disappeared.

> Enabling item output — an apparently additive change — **silently
> removes Dublin Core support** for every other harvester pointed at
> that library. Installation documentation must state this, and the
> recommended conf should include an `oai_dc` block to preserve it.

### 3.2 Enabling items

`Koha/OAI/Server/Repository.pm`:

```perl
sub items_included {
    my ( $self, $format ) = @_;
    if ( my $conf = $self->{conf} ) {
        return $conf->{format}->{$format}->{include_items};
    }
    return 0;
}
```

`include_items` is **per-format**, not global. Default without a conf
file is `0`, confirming that a stock Koha exposes bibliographic data
only.

Working conf, written to `/etc/koha/sites/kohadev/oaiconf.yaml`
(writable by the instance user, beside `koha-conf.xml`, readable by the
web server):

```yaml
format:
    marcxml:
      metadataPrefix: marcxml
      metadataNamespace: http://www.loc.gov/MARC21/slim http://www.loc.gov/standards/marcxml/schema/MARC21slim
      schema: http://www.loc.gov/MARC21/slim http://www.loc.gov/standards/marcxml/schema/MARC21slim.xsd
      include_items: 0
    marc21:
      metadataPrefix: marc21
      metadataNamespace: http://www.loc.gov/MARC21/slim http://www.loc.gov/standards/marcxml/schema/MARC21slim
      schema: http://www.loc.gov/MARC21/slim http://www.loc.gov/standards/marcxml/schema/MARC21slim.xsd
      include_items: 1
```

Caution: Koha's shipped fixture `t/db_dependent/OAI/oaiconf.yaml`
contains a typo (`metadataPrefix: marxml` under the `marcxml` key). The
YAML key and the declared `metadataPrefix` are separate values that can
disagree.

> **Adapter requirement.** Trust what `ListMetadataFormats` reports over
> what any configuration file claims.

---

## 4. Record identity — three distinct ID spaces

This is the highest-risk area for the adapter, and the reason for the
join-verification discipline in the project brief.

| Source | Record 1 value | Coverage | Suitable as key? |
|---|---|---|---|
| OAI `<identifier>` | `KOHA-OAI-TEST:1` | 100% | **Yes** |
| MARC `999$c` | `1` | 100% | Yes (as cross-check) |
| MARC `001` | `17446121` | **87.8%** | **No** |

`001` on record 1 is the **Library of Congress control number** carried
in with the imported record — not Koha's biblionumber. It is a foreign
system's identifier, not guaranteed unique across sources or stable,
and it is **absent from 53 of 436 records**.

`999$c` matched the biblionumber parsed from the OAI identifier in
**436/436** records.

> **Adapter requirements.**
> - Key on the OAI identifier. It is already namespaced by
>   `OAI-PMH:archiveID` and is the only universally present identifier.
> - Parse the biblionumber by splitting on the **last** colon — the
>   archiveID prefix is operator-configurable and may itself contain
>   punctuation.
> - Store the **full** OAI identifier as `source_record_id`, not the
>   bare biblionumber. `GetRecord` requires the full form.
> - Assert `999$c == biblionumber` at ingest as an integrity check;
>   a mismatch indicates an upstream problem.
> - Never join on `001`.

---

## 5. Bibliographic field coverage — MARC21 (n = 436)

### 5.1 Selected frequencies

| Tag | Field | % of records | Multi-valued in |
|---|---|---|---|
| 245 | Title | **100.0%** | 0 |
| 942 | Koha added-entry | 100.0% | 0 |
| 999 | Koha internal | 100.0% | 0 |
| 260 | Publication (AACR2) | 92.0% | 1 |
| 008 | Fixed-length data | 91.3% | 0 |
| 300 | Physical description | 89.9% | 2 |
| 001 | Control number | 87.8% | 0 |
| 020 | ISBN | 77.8% | 123 |
| 100 | Main author | 75.2% | 0 |
| **650** | **Topical subject** | **59.6%** | **146** |
| 082 | Dewey | 54.4% | 1 |
| 700 | Added author | 40.1% | 67 |
| 504 | Bibliography note | 31.2% | 2 |
| 041 | Language code | 22.9% | 0 |
| 505 | Contents note | 11.7% | 0 |
| **520** | **Summary** | **8.9%** | 1 |
| 264 | Publication (RDA) | 2.3% | 4 |
| 880 | Alternate script | 0.2% | 1 |

Full table available by re-running the profiling script (§14.2).

### 5.2 Cataloguing-standard drift

The corpus mixes AACR2 and RDA records:

- **`260` 92.0%** vs **`264` 2.3%** — publisher and date live in
  different fields depending on record vintage.
- **`440` 12.2%** (obsolete) vs **`490` 14.7%** — same split for series.

> **Adapter requirement.** Field mapping must read both, with a defined
> precedence. A mapping that handles only the modern field silently
> loses data on the majority of records here.

### 5.3 Koha-specific extensions

`$9` subfields appear on `100` and `600` (values 1702, 1703) carrying
Koha's internal authority linkage. These are **not standard MARC21**
and must be ignored rather than treated as bibliographic content.

`245$a` ends `" :"` and `$b` ends `" /"` — ISBD punctuation is baked
into the stored data. Title normalization must strip it.

---

## 6. Text availability for embedding — decisive for Decision 3/4

| Available text | Records | % |
|---|---|---|
| Has `520` summary | 39 | 8.9% |
| Has `650` subject headings | 260 | 59.6% |
| Has `505` contents note (with `$a`) | 33 | 7.6% |
| **`520` OR `650`** | 276 | **63.3%** |
| `520` OR `650` OR `505` | 285 | 65.4% |
| **TITLE ONLY — none of the above** | **151** | **34.6%** |

Subject headings where present: mean **2.90**, max **48**.

### Implications

1. **The premise holds.** Real MARC catalogues lack descriptions —
   8.9% coverage for `520` versus the description-rich Goodreads data
   the thesis system was built on.

2. **`505` is not a rescue.** Adding contents notes moves coverage from
   63.3% to only 65.4%; the fields overlap heavily. (Note: 51 records
   carry a `505` tag but only 33 yield `$a` text.)

3. **34.6% of records have nothing but a title.** This is the finding
   that reshapes the planned experiment. The three-arm design
   (description-only / description+subjects / subjects-only) does not
   address the largest single cohort. For a third of the catalogue,
   SBERT has only a title string to encode — a far weaker semantic
   signal than the thesis ever operated on.

   > A fourth arm is required: what to do for title-only records. The
   > honest possible outcome is that content-based recommendation is
   > simply weak for them, and the system should either lean on other
   > signals or say so.

4. **`650` array cardinality is justified** — multi-valued in 146 of
   the 260 records that have it. But the max of 48 is an outlier that
   would dominate any naive concatenation; cap or weight accordingly.

---

## 7. Language detection — Decision 4

| Signal | Records | % |
|---|---|---|
| `008/35-37` usable (3 chars) | 372 | 85.3% |
| No `008`, but `041` present | 16 | 3.7% |
| **No language signal at all** | **48** | **11.0%** |

Top values: `eng` 324, `grc` 8, `por` 6, `gla` 5, `dut` 5, `||e` 4,
`gae` 4, `spa` 3.

Two observations:

- **`||e` is malformed** — MARC fill characters (`|`) leaking into the
  language position. Validation against ISO 639-2 is required; a naive
  three-character slice yields garbage.
- **The corpus is genuinely multilingual.** Greek, Portuguese, Gaelic,
  Dutch and Spanish all appear in 436 sample records, plus one `880`
  alternate-script field. This is empirical support for configurable
  embedding models rather than a hypothetical concern.

> **Detection chain**: `008/35-37` → `041$a` → validate against
> ISO 639-2 → fall back to a configured instance default.

---

## 8. Items / holdings — MARC 952

Measured with `include_items: 1` on `marc21`, `0` on `marcxml`, from a
single instance in a single state (controlled A/B).

| | `marc21` (items on) | `marcxml` (items off) |
|---|---|---|
| Records | 436 | 436 |
| Pages | 9 | 9 |
| Records with ≥1 `952` | **411** | **0** |
| Payload | 2,135,672 bytes | 1,575,146 bytes |

Payload overhead: **+35.6%**.

Control validation: the `marcxml` harvest differs from the pre-conf
baseline (1,575,129 bytes) by exactly 17 bytes — accounted for by
`marcxml` being one character longer than `marc21` in each of nine
`<request metadataPrefix='…'>` echoes. Nothing else changed.

Pagination is computed from the bibliographic list and is unaffected by
item embedding.

### 8.1 Items per bibliographic record

Total **961** items; mean **2.20**; max **4**.

| Items | Records |
|---|---|
| **0** | **25 (5.7%)** |
| 1 | 130 |
| 2 | 101 |
| 3 | 91 |
| 4 | 89 |

> **25 records have no items at all.** Bibliographic records without
> holdings are normal in real catalogues (on-order, electronic
> resources, catalogue-only entries). The schema must permit a `works`
> row with zero `items`, and the recommender must not treat itemless
> works as errors. This is the bib/item separation earning its keep on
> the first real dataset.

### 8.2 Subfield coverage (n = 961 items)

| Subfield | Meaning | % of items |
|---|---|---|
| `$a` | Owning branch | 100% |
| `$b` | Holding branch | 100% |
| `$c` | Shelving location | 100% |
| `$d` | Date acquired | 100% |
| `$p` | **Barcode** (item unique key) | 100% |
| `$r` | Date last seen | 100% |
| `$w` | Price effective date | 100% |
| `$y` | **Item type** | 100% |
| `$0` | Withdrawn flag | 100% |
| `$1` | Lost flag | 100% |
| `$4` | Damaged flag | 100% |
| `$7` | Not-for-loan flag | 100% |
| `$8` | Collection code | 99.5% |
| `$2` | Classification scheme | 0.7% |
| `$6`, `$9`, `$o` | (sparse) | < 0.4% |

`$a` and `$b` are both present and both needed — an item owned by one
branch can be held at another.

`$y` (item type) is significant beyond storage: it drives circulation
rules and distinguishes a lendable book from reference-only stock or
equipment. It bears directly on whether a checkout signal is
meaningful for a given item.

Status flags `$0/$1/$4/$7` were all `0` across the sample — every item
available and lendable. Real catalogues will not be this uniform.

### 8.3 Cross-flavour item subfield map

Measured against both corpora (961 MARC21 items, 1,633 UNIMARC items
sampled). The letters differ for the same concept, and two collide
outright.

| Concept | MARC21 `952` | UNIMARC `995` |
|---|---|---|
| Barcode | `$p` | `$f` |
| Owning branch | `$a` | **`$c`** |
| Holding branch | `$b` | `$b` |
| Shelving location | **`$c`** | `$e` |
| Call number | *(absent)* | `$k` (100%) |
| Item type | `$y` | **`$r`** |
| Due date / on loan | `$q` | `$n` |
| Cumulative issues | `$l` | *(absent)* |
| Date acquired | `$d` | `$5` |
| Serial enumeration | — | `$v` |

> **Two collisions worth naming.** `$c` is the shelving location in
> MARC21 and the owning branch in UNIMARC. `$r` is a timestamp
> (datelastseen) in MARC21 and the **item type** in UNIMARC. Reusing one
> subfield map across flavours would store `2014-05-07 00:00:00` in the
> item_type column and raise nothing — the same silent-corruption shape
> as the tag-level collisions of §10.2, one layer down.

Both maps were written from the specification first and both were wrong
until measured. The specification describes what a subfield *means*;
only the data shows what a given system actually puts there.

---

## 9. Circulation state — the sync-design finding

### 9.1 Experiment

Item barcode `3999900000001` (biblionumber 1, item type `BK`) was
checked out via the staff interface to patron 21, due 2026-08-28 — a
5-day loan derived from the circulation rules for `BK`. The same record
was fetched via `GetRecord` before and after.

| | Before | After | Changed |
|---|---|---|---|
| OAI `<datestamp>` | `2026-08-23T00:51:47Z` | `2026-08-23T01:33:08Z` | **YES** |
| MARC `005` | `20200129130600.0` | `20200129130600.0` | **NO** |

952 subfield changes:

| Subfield | Before | After | Meaning |
|---|---|---|---|
| `$q` | *absent* | `2026-08-28` | **onloan — due date** |
| `$l` | *absent* | `1` | **cumulative issue count** |
| `$s` | *absent* | `2026-08-23` | date last borrowed |
| `$r` | `2014-05-07 00:00:00` | `2026-08-23 01:33:08` | date last seen |

### 9.2 What this means

**A circulation event bumps the OAI datestamp while MARC `005` stays
unchanged.** Koha derives the OAI datestamp from `biblio.timestamp`,
which item modifications propagate to; the bibliographic record itself
was never touched. The two disagree by design.

Three consequences:

1. **Incremental harvests surface circulation churn as bibliographic
   updates.** Every checkout, return and renewal makes that work
   reappear in the next `ListRecords?from=…`, with byte-identical
   bibliographic content and only the 952 block differing. Sync volume
   is driven by **circulation, not cataloguing** — a 100k-title library
   circulating 2k items/day pushes ~2k records per incremental harvest,
   nearly all bibliographically unchanged. Sizing assumptions must
   reflect this.

2. **`005` is the correct dirty-check for the ML pipeline.** Because it
   moves only when the bibliographic record genuinely changes, the
   adapter should compare `005` (or hash the record excluding `952`) to
   decide whether to **re-encode SBERT embeddings**. Re-embedding on
   every checkout would be enormously wasteful. This is a cheap,
   principled optimisation derived directly from measurement.

3. **Availability is harvestable.** This partially revises the
   integration design: the live read-only API is not strictly required
   for availability, since checkouts propagate through OAI-PMH. It
   remains necessary for *real-time* accuracy between sync cycles, but
   its status shifts from "essential" to "freshness upgrade."

### 9.3 `$l` — an item-level popularity signal (MARC21 only)

`$l` is the item's **cumulative issue count**, delivered through the
standard read-only integration path with no circulation-log access
required.

It is aggregate, not per-patron, so it **cannot** build a user–item
matrix for collaborative filtering. But it is a legitimate item-level
popularity signal — directly relevant to the confidence-formula work,
and to any popularity-based cold-start stage, which otherwise has no
`avg_rating` or `ratings_count` equivalent in library data.

> **Availability caveat — see §10.9.** This subfield has **no
> counterpart in Koha's default UNIMARC framework**. A checkout on the
> UNIMARC instance produced only a due date, no counter. Any design that
> depends on `$l` must degrade gracefully where it is absent, which
> includes the French-language catalogues this project prioritises.

---

## 10. UNIMARC — cross-flavour findings

A second instance was started with
`KOHA_MARC_FLAVOUR=unimarc ktd --proxy --name unimarc up -d`,
confirmed via `C4::Context->preference('marcflavour') == 'UNIMARC'`.
Corpus: **4,849 records**, harvested in 97 pages.

> The 100-page safety limit in the harvest script (§14.1) was nearly
> reached by a *sample* corpus. A real library would blow straight
> through it. The production adapter needs a generous bound or, better,
> a time budget rather than a page count.

### 10.1 The metadata prefix and XML namespace are both wrong

`ListMetadataFormats` on the UNIMARC instance advertises the same three
prefixes as MARC21: `oai_dc`, `marc21`, `marcxml`. Requesting
`metadataPrefix=marc21` returns records in the
`http://www.loc.gov/MARC21/slim` namespace.

**The content is unambiguously UNIMARC:**

```
prefix requested : marc21
namespace served : http://www.loc.gov/MARC21/slim
tags present     : 001 011 090 099 100 101 110 200 210 606 615 700 801
title            : 200$a "La Recherche"
language         : 101$a "Fre"
```

No `245`, no `650`, no `008`. Both the prefix name and the namespace
declaration misrepresent the payload.

> **Adapter requirement.** Detect flavour by **probing record
> structure** — presence of `200` versus `245` in the first harvested
> record — and never from the metadata prefix name or the XML
> namespace. Fail loudly if neither tag appears.

### 10.2 Tag-number collisions — silent corruption risk

**This is the highest-severity finding in this document.** Three tag
numbers exist in both standards with entirely different meanings:

| Tag | MARC21 meaning | UNIMARC meaning | Present in UNIMARC corpus |
|---|---|---|---|
| `100` | Main author | General processing data (dates/codes) | **4,849 (100.0%)** |
| `300` | Physical description | General note | 1,031 (21.3%) |
| `020` | ISBN | Country of publication code | 115 (2.4%) |

`245`, `650`, `008` and `260` correctly return **zero** — they do not
exist in UNIMARC, so a MARC21 map would yield empty values and the
problem would be visible.

The three colliding tags are the danger. A MARC21 field map run against
this feed extracts `100$a` = `"20070130              frey50        "` as
the **author name** for every record, raising no error. Not a crash,
not an empty result — confident nonsense.

This is the same failure class as joining two datasets on a shared
column name that means different things in each. Structural sniffing
(§10.1) is a hard requirement, not a refinement.

### 10.3 Field mapping, measured on both sides

| Purpose | UNIMARC | Coverage | MARC21 | Coverage |
|---|---|---|---|---|
| Title | `200` | **100.0%** | `245` | **100.0%** |
| Author (main) | `700` | 97.9% | `100` | 75.2% |
| Author (added) | `701` | 34.7% | `700` | 40.1% |
| Publication | `210` | 99.7% | `260`/`264` | 92.0% / 2.3% |
| Physical description | `215` | 97.2% | `300` | 89.9% |
| Language | `101` | 96.7% | `008/35-37` | 91.3% |
| Subject (topical) | `606` | 49.9% | `650` | 59.6% |
| Subject (category) | `615` | 99.9% | — | — |
| Dewey | `676` | 54.2% | `082` | 54.4% |
| Summary / abstract | `330` | **16.1%** | `520` | **8.9%** |
| Contents note | `327` | 16.7% | `505` | 11.7% |
| Series | `225` | 41.0% | `490`/`440` | 14.7% / 12.2% |
| ISBN | `010` | 85.7% | `020` | 77.8% |

Note `606` is heavily multi-valued (630 of 2,422 records), matching
`650`'s behaviour — the `subject_headings[]` array design holds for
both flavours.

### 10.4 Corpus composition — not all records are books

Leader position 06 (record type) and 07 (bibliographic level):

| Leader/06 | Count | | Leader/07 | Count |
|---|---|---|---|---|
| `a` textual | 4,347 | | `m` monograph | 4,401 |
| **`j` musical sound recording** | **416** | | `n` (unspecified) | 294 |
| ` ` (blank) | 83 | | ` ` (blank) | 83 |
| `g` projected medium | 3 | | `s` serial | 71 |

**8.6% of this catalogue is not books** — sound recordings, with a
small number of serials and projected media. A library recommender will
be asked to handle CDs, DVDs and equipment alongside books.
`leader/06` is the field to filter or segment on, and the schema should
carry it rather than assuming every `works` row is a book.

### 10.5 `615` is present but low-entropy — do not overcount it

`615$a` appears on 4,846 of 4,849 records (99.9%), which naively drives
"title only" down to **2 records (0.0%)** versus MARC21's 34.6%. That
reading is misleading.

Sampled values are **mixed text and bare codes** in the same catalogue:

```
615$a = "Documentaire"
615$a = "44"
615$a = "Documentaire"
615$a = "45"
```

A record whose only supplementary text is `Documentaire` has gained
almost nothing semantically — it is a broad category, not a topic. A
record whose `615$a` is `44` has gained nothing at all; a bare integer
encodes as noise.

**Genuinely useful text coverage:**

| | UNIMARC | MARC21 |
|---|---|---|
| Summary field | 16.1% (`330`) | 8.9% (`520`) |
| Topical subjects | 49.9% (`606`) | 59.6% (`650`) |
| Contents note | 16.7% (`327`) | 7.6% (`505`) |
| **Any of the above** | **61.5%** | **65.4%** |

The two flavours tell the same story: roughly a third of records have
nothing but a title to embed. §6's conclusion stands unchanged.

> **Design decision.** Treat `615` as a **coarse categorical feature** —
> route it through `subject_genre_mapping` as a genre label or filter,
> and exclude it from embedding input. Do not count it toward text
> coverage.

The one real difference is `330` at 16.1% versus `520` at 8.9%.
Different corpora, so not a controlled comparison, but consistent with
European cataloguing practice including abstracts more often.

### 10.6 Duplicate records — a single catalogue is not duplicate-free

An earlier working assumption held that a single professionally-managed
catalogue would not contain meaningful duplication, unlike crowdsourced
data. That is too strong.

**52 ISBNs in the UNIMARC corpus are shared by 111 works** — 2.3% of the
catalogue. One shared ISBN in the MARC21 corpus. Inspection confirms
these are genuine duplicate records rather than distinct works:

```
ISBN 2234011752
  KOHA-OAI-TEST:185   "La femme des sables"  Abe Kobo  1990  Stock
  KOHA-OAI-TEST:997   "La femme des sables"  Abe Kobo  1990  Stock
```

One pair is more instructive than the identical ones:

```
ISBN 2081603810
  KOHA-OAI-TEST:343   "Le beau chardon d'Ali Boron"   Flammarion
  KOHA-OAI-TEST:2245  "Le beau chardond'Ali Boron"    Garnier-Flammarion
```

Same book, a missing space in the title, and the publisher recorded two
different ways. **A title+author key would treat these as distinct
works; the ISBN catches them.** That is direct evidence for ISBN-first
entity resolution rather than the title-normalisation approach, and it
was previously argued from first principles alone.

> **Schema consequence.** ISBN is the entity-resolution key but cannot
> be a uniqueness constraint. It is indexed; merging belongs in
> application code, where the ambiguous cases can be judged.

Scope this honestly: 2.3% is far lighter than crowdsourced duplication,
and entity resolution here remains a light sanity check rather than the
heavy deduplication pass the thesis dataset required. But it is not
zero, and a design that assumed zero would leave duplicate works with
split circulation signal.

### 10.7 Language codes — the same problems, plus case

`101$a` values across 4,849 records:

```
Fre 3673 | fre 955 | (none) 158 | Eng 54 | und 3 | Lan 2
Ita 1 | Por 1 | Spa 1 | Ger 1
```

- **Case is inconsistent within a single catalogue** — `Fre` and `fre`
  both appear (79% / 21%). Case-fold before any comparison.
- **`Lan` is not a valid ISO 639-2 code.** Junk data, same class as
  MARC21's `||e` fill characters (§7).
- **`und` = "undetermined"** is a legitimate code meaning the cataloguer
  did not know. It must not be treated as a real language.
- **3.3% carry no `101` at all.**

This corpus is **94.6% French**. It is therefore a genuinely useful test
bed for the configurable-embedding-model decision:
`all-MiniLM-L6-v2` applied here would produce near-noise embeddings for
3,673 records.

### 10.8 Instance identity is not self-namespacing

Both instances report `OAI-PMH:archiveID = KOHA-OAI-TEST` — the shipped
default. Both therefore emit `KOHA-OAI-TEST:1` as the identifier for
entirely different records.

> **Adapter requirement.** Scope `source_record_id` uniqueness by the
> configured source, not by the identifier prefix. The prefix is an
> operator-set default that libraries frequently leave unchanged.

### 10.9 Items under UNIMARC — field 995, not 952

Enabling `include_items: 1` on the UNIMARC instance (same conf file,
same path — `KOHA_INSTANCE` remains `kohadev` regardless of the
Compose project name) produced items under **995**. Zero 952 fields.
`oai_dc` dropped from `ListMetadataFormats` exactly as on MARC21, so
the extended-mode behaviour of §3.1 is flavour-independent.

**911 items across 50 records — 18.2 per record**, versus MARC21's 2.20.
This is a periodicals-heavy corpus: `$v = "Fascicule;406;01/03/2007"`
is a serial issue enumeration, `$r = CR` a serial item type, and
`$e = "Revues adultes"` an adult-magazines collection. Each item is one
issue of a magazine. Record 1 alone carries 47 items.

Subfield coverage (n = 911):

| Subfield | Coverage | Reading |
|---|---|---|
| `$2`, `$9`, `$b`, `$c`, `$f`, `$k`, `$o`, `$r` | 100% | — |
| `$v` | 96.9% | serial enumeration |
| `$5` | 68.9% | date acquired |
| `$e` | 86.8% | collection / shelving |
| `$x` | 33.3% | — |
| `$n` | 1.5% | **on loan (due date)** |

Mapping against MARC21:

| Concept | MARC21 952 | UNIMARC 995 |
|---|---|---|
| Owning branch | `$a` | **`$c`** |
| Holding branch | `$b` | `$b` |
| Shelving location | `$c` | `$e` |
| Barcode | `$p` | `$f` |
| Call number | — | `$k` |
| Item type | `$y` | `$r` |
| Date acquired | `$d` | `$5` |
| **Due date / on loan** | **`$q`** | **`$n`** |
| **Cumulative issue count** | **`$l`** | **absent** |
| Date last borrowed | `$s` | absent |
| Date last seen | `$r` | absent |
| Serial enumeration | — | `$v` |

> **`$c` means shelving location in MARC21 and owning branch in
> UNIMARC.** A subfield-level collision inside the item field, mirroring
> the tag-level collisions of §10.2. The same silent-corruption risk
> applies one layer down: the `items` table needs a per-flavour subfield
> map, not just a per-flavour tag map.

### 10.10 Circulation under UNIMARC — what generalises and what doesn't

Same experiment as §9.1: item `bc_1` (item type `CR`, a periodical
issue) checked out via the staff interface to patron 45, due
2026-08-28. The `CR` type proved lendable here — a 5-day loan, matching
MARC21's `BK`. Note this is not guaranteed in production; many libraries
make periodicals reference-only, and item type is what governs it.

| | Before | After | Changed |
|---|---|---|---|
| OAI `<datestamp>` | `2026-08-23T02:33:32Z` | `2026-08-23T05:10:54Z` | **YES** |
| UNIMARC `005` | *absent* | *absent* | n/a |
| Items on record | 47 | 47 | — |
| Items with any change | — | **1 of 47** | — |

The only subfield change: **`$n`: absent → `2026-08-28`**.

**Two consequences:**

1. **§9.2's sync-design finding generalises.** Circulation bumps the OAI
   datestamp on both flavours, so incremental harvest volume is driven
   by circulation regardless of flavour.

2. **The `005` dirty-check does not generalise.** UNIMARC records carry
   **no `005` field at all** — it is absent from the §10.3 tag inventory
   entirely, not merely unchanged. The flavour-neutral implementation is
   therefore to **hash the record with the item field (952 or 995)
   excluded**, and use `005` only as a fast path where present.

**On the missing issue counter.** The 995 inventory contains no
counter-like subfield, and the checkout created none. Scope this
claim carefully: this is a property of **Koha's default UNIMARC
framework mapping**, not of UNIMARC the standard. Koha's `items` table
has an `issues` column regardless of flavour; it simply is not mapped
into 995 by default. A library could add the mapping, and PMB may
expose it differently. The accurate statement is *"not exposed via
OAI-PMH under Koha's default UNIMARC framework"* — verify against any
specific deployment rather than assuming absence.

---

## 11. Patron ratings — a second read channel

### 11.1 Koha collects ratings natively

Relevant system preferences on the MARC21 instance, at KTD defaults:

| Preference | Value | Meaning |
|---|---|---|
| `OpacStarRatings` | `all` | Patron star ratings enabled throughout the OPAC |
| `OPACComments` | `1` | Patron comments enabled |
| `ShowReviewer` | `full` | Reviewer name displayed |

`reviewson` does not exist as a preference in this version — comments
are governed by `OPACComments`.

This resolves the question left open in earlier drafts. **Explicit
patron ratings are a native Koha feature, enabled by default.**
BookRS-Platform should read them, not build a parallel rating widget:
duplicating the feature would split the data across two systems and
present patrons with two rating controls on the same page.

### 11.2 Schema

```
MariaDB> DESCRIBE ratings;
Field           Type          Null  Key  Default
borrowernumber  int(11)       NO    PRI  NULL
biblionumber    int(11)       NO    PRI  NULL
rating_value    tinyint(1)    NO         NULL
timestamp       timestamp     NO         current_timestamp()
```

A `reviews` table exists alongside it, holding free-text comments.

Two properties worth noting:

- **The composite primary key `(borrowernumber, biblionumber)`** means
  one rating per patron per work — no deduplication needed on ingest.
- **`biblionumber` is the same key the OAI identifier carries** (§4).
  Ratings therefore join to harvested works with no ID translation. In
  a project where an ID-space mismatch caused a serious defect, this is
  worth stating explicitly rather than assuming.

Both tables were empty in the sample data (`rating_rows = 0`,
`review_rows = 0`), so only the schema is verified here, not the
behaviour of populated data.

### 11.3 Consequences

**The integration is two channels, not one.** Ratings are not part of
the MARC record and will never arrive via OAI-PMH. Reaching them
requires Koha's REST API (`/api/v1/`) or another read path. This is the
only finding in this document that *adds* scope rather than clarifying
or constraining it.

**The valence problem is per-deployment, not universal.** The working
assumption had been that library data structurally lacks preference
direction, making the thesis's Config D result (−22.1% P@10 without
explicit ratings) a standing risk. Since Koha ships a rating feature
enabled by default, a library running it for several years may hold
substantial rating data — or none, if its patrons never used it.

> **Design requirement.** The adapter should **count rating rows at
> setup and report the figure to the operator**, rather than assuming
> either abundance or absence. A deployment with 50,000 ratings and one
> with zero need different confidence formulas, and the difference is
> knowable in one query.

**`reviews` is a possible embedding input.** Free-text patron comments
could supplement sparse bibliographic metadata for works that have
them — partially addressing the title-only cohort of §6. Unverified and
not scoped; recorded for later consideration.

---

## 12. Operational notes

Two findings from building against these instances, both arising from
mistakes made while doing so. Neither is a property of the OAI-PMH
specification; both are properties of working with real deployments.

### 12.1 A restart can silently drop OAI configuration

The MARC21 instance was restarted mid-session. Afterwards:

- `OAI-PMH` had reverted to `0` — endpoint returning 404
- `OAI-PMH:ConfFile` was unset — no item output
- The database was rebuilt from scratch (436 biblios, 961 items again,
  but a fresh `earliestDatestamp`)

In KTD this is by design: the database is ephemeral unless
`--persistent-db` is passed. But the underlying exposure generalises.
A library completing an upgrade, restoring from backup, or reverting a
configuration change could reset the same preferences, and **the
failure is quiet**. The endpoint still answers, `ListRecords` still
returns 200, records still parse — they simply arrive without any 952
or 995 holdings.

An adapter that verified configuration only at setup would harvest
bibliographic-only records indefinitely and discard every item, without
raising anything.

> **Adapter requirement.** At the start of **every** harvest run, not
> only at setup: confirm the endpoint answers `Identify`, confirm the
> requested prefix appears in `ListMetadataFormats`, and confirm the
> expected item field (952 or 995, per detected flavour) is present in
> the first page. If items were present on the previous run and are
> absent now, stop and report rather than proceeding — a sudden loss of
> holdings is far more likely to be a configuration regression than a
> genuine emptying of the catalogue.

### 12.2 Do not pattern-match MARC XML with regular expressions

Koha emits attributes with **single quotes**:

```xml
<datafield ind1=" " tag='952' ind2=" ">
```

A `grep` for `tag="952"` returns zero matches against a response
containing 93 item fields. Attribute **order** also varies between
records (`ind1` before `tag` in some, after in others), and whitespace
is inconsistent.

Every `xml.etree.ElementTree` parse performed during this analysis was
correct. Only ad-hoc shell patterns failed — and they failed by
returning **zero**, which reads as a legitimate absence rather than a
broken measurement.

> **Adapter requirement.** All record content is read through an XML
> parser. No regular expressions over MARC XML, including for quick
> checks or diagnostics. The `<header>` count in the harvest script
> (§14.1) is the sole exception: it counts a structural element in the
> OAI envelope for progress reporting only, never record content, and
> is not used for any decision.

This is the same class of error as the ID-space mismatch that motivated
§4: an assumption about data format that looks obviously true, is
never checked, and fails silently rather than loudly.

### 12.3 Attribute order is nondeterministic — canonicalise before hashing

§12.2 records that Koha emits single-quoted attributes in inconsistent
order, as a reason not to use regular expressions. There is a second and
sharper consequence.

Two consecutive `GetRecord` calls for the same unmodified record
returned **identical byte counts with the attributes in different
sequence**:

```
a: <datafield tag='020' ind2=' ' ind1=' '>
b: <datafield ind2=' ' tag='020' ind1=' '>
```

This is Perl hash iteration order surfacing in the output, and it varies
per worker process rather than per request — so it is intermittent,
which is worse than constant. `ElementTree` preserves the order
faithfully, so `tostring()` produces different bytes for identical
content.

Any hash used to detect whether a record has changed is therefore
meaningless unless the serialisation is canonical. Without it, every
record hashes differently on every harvest, a nightly sync rewrites the
entire catalogue and re-embeds it, and **nothing about the system
appears to be failing**. The first full load reported 436 updates where
it should have reported 436 unchanged.

> **Requirement.** Canonicalise (C14N — `ET.canonicalize`, which sorts
> attributes and normalises namespaces) before hashing. Verified stable
> across 436 records over two full harvests spanning 18 requests and
> multiple worker processes.

### 12.4 Two hashes, not one

A single content hash forces a choice between two failures. Circulation
bumps a record's OAI datestamp without changing its bibliography
(§9.2), so a hash covering the whole record re-embeds the catalogue on
every loan; a hash excluding holdings leaves availability stale between
full syncs.

Keeping them separate — one over the bibliography, one over the 952/995
block — lets each change independently. Verified by putting two items on
loan: the following harvest reported 436 works unchanged (no
re-embedding) and exactly 2 works with refreshed holdings, writing 3
item rows, since one of the two works has two copies.

### 12.5 Fixed-width character types compare unequally in application code

Not a MARC finding, but the same class of failure and it cost real time.

PostgreSQL pads `CHAR(n)` to its declared width and the driver returns
the padding. The value therefore compares **equal in SQL and unequal in
Python**:

```
SELECT length(h), h = 'hash-a' FROM t   ->   6, true
python: repr(h) -> 'hash-a' + 58 spaces
        h == 'hash-a' -> False
```

`content_hash CHAR(64)` worked in production data only because SHA-256
digests are exactly 64 characters and fill the column. A shorter value
would silently never match, and every record would look modified
forever — the same symptom as §12.3, from an unrelated cause.
`languages CHAR(3)` had the same latent problem: MARC codes are three
characters, but malformed two-character ISO 639-1 codes occur in real
data (`ru` appears in the reference corpus).

> **Requirement.** Use `VARCHAR` for any value compared in application
> code. Fixing the type rather than stripping at every read, because
> stripping relies on every future caller remembering to.

### 12.6 A test suite that can reach the development database

The loader tests read `DATABASE_URL` and begin with
`TRUNCATE ... CASCADE`, so running the suite after a load silently
destroyed the catalogue. It surfaced only as a one-row discrepancy —
5,286 works where 436 + 4,849 is 5,285 — from a fixture row left by the
last test to run. Earlier runs looked correct because a reload always
happened to follow.

The suite now requires a separate `TEST_DATABASE_URL`, deliberately a
different variable rather than a different default. A test suite that
can reach real data through an ambient environment variable is a hazard
whether or not it currently does.

---

## 13. Limitations of this analysis

Stated plainly, because these bound what the findings support:

1. **Sample data, not a real catalogue.** Nearly every 952 subfield sits
   at exactly 100% coverage — implausibly clean. Real library data will
   be patchier, and coverage figures here are likely optimistic.
2. **Small corpus** (436 records). Percentages carry meaningful sampling
   error; the 0.2% figures represent single records.
3. **One instance, one Koha version** (`main`, March 2026). Behaviour on
   older stable releases is untested — relevant because `marc21` as a
   metadata prefix only became available in 17.05 and was absent from
   the sample conf before 23.11.
4. **UNIMARC item percentages come from the head of the corpus.**
   MARC21 items (§8) were measured across the full 436 records. UNIMARC
   subfield coverage (§10.9, §8.3) was measured across the first 300
   records, which are periodicals-heavy at 5.4 items each; the full
   4,849 records yield 6,202 items, or 1.28 per work, because the tail
   is single-copy monographs. Extrapolating from the head of a sorted
   corpus was wrong by a factor of four when first attempted, and the
   subfield percentages carry the same bias.
5. **One circulation event tested per flavour.** Returns, renewals,
   holds, and deleted records were not exercised on either. The
   `deletedRecord: persistent` behaviour in particular remains
   unverified against real deletions — and deletions are the case most
   likely to be handled incorrectly by an adapter that has never seen
   one.
6. **Rating tables verified as schema only.** Both `ratings` and
   `reviews` were empty in the sample data (§11.2), so the shape is
   confirmed but nothing about real rating density, distribution or
   patron uptake is known. The REST read path for them (§11.3) has not
   been exercised at all.
7. **Findings in §12 are single incidents.** The configuration loss was
   observed once, on an instance designed to be ephemeral. Whether real
   Koha deployments lose OAI preferences on upgrade or restore is
   untested and should not be assumed from this.
8. **Both corpora are Koha sample data.** The UNIMARC set is larger
   (4,849) and so carries less sampling error than the MARC21 set, but
   it is still synthetic and skewed — 94.6% French, 8.6% sound
   recordings.

---

## 14. Appendix — scripts

### 14.1 Harvest loop

```bash
#!/bin/bash
BASE="http://localhost:8080/cgi-bin/koha/oai.pl"
PREFIX="${1:-marc21}"
page=1; token=""
while :; do
  if [ -z "$token" ]; then
    url="${BASE}?verb=ListRecords&metadataPrefix=${PREFIX}"
  else
    url="${BASE}?verb=ListRecords&resumptionToken=$(printf '%s' "$token" | sed 's|/|%2F|g')"
  fi
  out=$(printf 'page_%03d_%s.xml' "$page" "$PREFIX")
  curl -s "$url" -o "$out"
  n=$(grep -o '<header>' "$out" | wc -l)
  token=$(grep -o "<resumptionToken[^>]*>[^<]*</resumptionToken>" "$out" \
          | sed 's|.*>\(.*\)</resumptionToken>|\1|')
  echo "$out  records=$n  next_token='${token}'"
  [ -z "$token" ] && break
  page=$((page+1))
  [ "$page" -gt 100 ] && echo "SAFETY STOP at 100 pages" && break
done
echo "--- total records: $(cat page_*_${PREFIX}.xml | grep -o '<header>' | wc -l)"
```

Two production-relevant details: the token is URL-encoded because it
contains slashes, and the safety stop prevents a malformed token from
looping forever.

### 14.2 Field profiling

The frequency table (§5.1) and coverage analysis (§6, §7) were produced
by Python scripts using `xml.etree.ElementTree` against the harvested
pages. Both parse the OAI namespace
(`http://www.openarchives.org/OAI/2.0/`) and the MARC21 slim namespace
(`http://www.loc.gov/MARC21/slim`) separately — note that `<record>`
appears in **both**, so unqualified tag matching produces wrong results.

---

*Compiled 2026-08-23. All figures measured, not estimated.*
