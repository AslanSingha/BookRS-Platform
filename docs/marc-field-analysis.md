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
which produced plausible output rather than errors. Section 13 covers
embedding model selection, measured rather than assumed.

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

Full table available by re-running the profiling script (§17.2).

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

> The 100-page safety limit in the harvest script (§17.1) was nearly
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
the MARC record and will never arrive via OAI-PMH. This is the only
finding in this document that *adds* scope rather than clarifying or
constraining it.

> **Correction (2026-08-26).** This section previously said ratings are
> "reachable over Koha's REST API." They are not. `api/v1/swagger/paths`
> contains no rating or review route in Koha main; the claim was made
> from the `ratings` table existing rather than from the API surface.
> Reading them would require direct database access, which Decision 1
> rules out, or a Koha plugin.
>
> **Circulation history is available**, and it is the better signal
> anyway: `/api/v1/checkouts?checked_in=true` returns per-patron loans
> with `patron_id`, `item_id` and — via the `item.biblio` embed —
> `biblio_id`, along with checkout and checkin dates. That is a
> user-item matrix, which `952$l`'s aggregate count is not. See §14.

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

> **A worse variant, observed 2026-08-26.** The `OAI-PMH:ConfFile`
> preference lives in the database; the file it names lives in the
> container filesystem. They have different lifetimes, so recreating a
> container leaves the preference pointing at a file that no longer
> exists — and Koha then returns **HTTP 500 on every OAI request**
> rather than falling back to bibliographic-only output.
>
> Louder than the silent case below, but harder to diagnose: the
> adapter reports "Endpoint is not ready to harvest: HTTP 500" after
> exhausting its retries, which is accurate and tells an operator
> nothing about the cause. A 500 from a Koha OAI endpoint is worth
> checking `OAI-PMH:ConfFile` against the filesystem before anything
> else.

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
> (§17.1) is the sole exception: it counts a structural element in the
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

### 12.7 An SQL function inlined into an index cannot resolve unqualified names

Building the trigram indexes produced:

```
ERROR:  function immutable_unaccent(text) does not exist
CONTEXT:  SQL function "searchable_authors" during inlining
```

while `\df` showed the function plainly present. The `CONTEXT` line is
the only part that explains it: PostgreSQL inlines an SQL function into
the index expression, and resolves names **during inlining against a
restricted `search_path`**. An unqualified call that works in every
other context fails here.

The fix is to schema-qualify every reference inside a function body that
will be used in an index expression — `public.immutable_unaccent(...)`,
not `immutable_unaccent(...)`.

Two related traps in the same area:

- **`unaccent()` is `STABLE`, not `IMMUTABLE`**, because its dictionary
  can be changed at runtime, so it cannot appear in an index expression.
  Wrapping it in an `IMMUTABLE` function is the standard workaround, and
  is correct only as long as the dictionary is left alone.
- **`array_to_string()` is also `STABLE`**, so indexing a joined array
  needs its own wrapper rather than composing built-ins.

### 12.8 Trigram similarity penalises short queries against long fields

Plain `similarity()` divides by the union of trigrams in both strings,
so a short query scores low against a long field however exact the
match:

| | score |
|---|---|
| `similarity('kernighan, brian w. ritchie, dennis m.', 'kernighan')` | **0.294** |
| `word_similarity('kernighan', 'kernighan, brian w. ritchie, dennis m.')` | **1.000** |

0.294 is below pg_trgm's 0.3 default, so the record was not returned at
all — and a third author would push it lower still. This is the normal
case for library author search: patrons type a surname, and records hold
`Surname, Forename Initial.` for every author.

`word_similarity` measures the query against the best-matching *portion*
of the target instead. Measured on the reference catalogue: an exact
author or title match scores 1.000, a related title (`la mythologie
celte` for `mythologie grecque`) 0.579, and an unrelated one 0.000.

> **Two-stage query.** Scoring every row with `word_similarity` cannot
> use the GIN index — measured at 82–110 ms over 5,285 works, which
> would be seconds over 300,000. Using the `<%` operator to narrow
> candidates via the index and then scoring explicitly costs 4–22 ms,
> at the price of recall in the tail: `<%` applies its own stricter
> threshold, so `mythologie grecque` returns three titles where
> exhaustive scoring finds four. Top-ranked results are unaffected.

### 12.9 Accent folding is not optional in a multilingual catalogue

The UNIMARC reference corpus is 87.6% French. A patron typing
`mythologie grecque` without accents must still find `La mythologie
Grecque`, so `unaccent` is applied on both sides of every comparison and
indexed accordingly.

Trigram rather than full-text search, for the same reason: `tsvector`
needs a language configuration chosen per row, the corpus spans French,
English, Greek and Arabic, and 11% of records carry no language code at
all. Trigrams are language-agnostic and additionally tolerate the typos
real catalogues contain — one reference record reads `Le beau
chardond'Ali Boron`, missing a space.

---

## 13. Embedding model selection

### 13.1 Method

Model quality was measured by whether **works sharing a subject heading
embed closer together than unrelated works do**. Subject headings are a
cataloguer's professional judgement about what a work is about, so two
works sharing one are, by that judgement, related. The metric is the gap
between the mean cosine similarity of same-subject pairs and the mean of
all other pairs. A model encoding nothing useful gives a gap near zero.

Sample: 400 works per flavour carrying at least one subject heading.

### 13.2 Result

| Model | MARC21 (English) | UNIMARC (French) |
|---|---|---|
| `all-MiniLM-L6-v2` | +0.597 | +0.169 |
| `paraphrase-multilingual-MiniLM-L12-v2` | **+0.604** | **+0.301** |

**The multilingual model is equivalent on English and 78% better on
French.** There is no trade-off to weigh — only a larger download
(471 MB against 91 MB) and roughly a third of the throughput. Both are
384-dimensional, so the schema is indifferent to the choice.

This answers the open question in the platform's Decision 4, which
assumed a quality cost on English catalogues that does not appear.

### 13.3 Retrieval, checked against real records

Metrics can agree while the output is useless, so neighbours were read
directly. Probing with *The Thirty Years War : Europe's tragedy*:

```
0.715  Encyclopédie de la Grande Guerre, 1914-1918
0.663  A Paris sous l'occupation
0.646  The age of revolution : Europe, 1789-1848
0.630  La guerre de 39-45 vécue par un enfant
```

All European military history, three of four in French, **none sharing
vocabulary with the query**. An English-only model cannot connect a
17th-century English title to *La guerre de 39-45*.

### 13.4 Tokenisation is a second, independent penalty

French needs more tokens per character in an English-trained wordpiece
vocabulary, because the subwords are not in it:

| | chars per token |
|---|---|
| MARC21 (English) | 3.84 |
| UNIMARC (French) | **2.90** |

A 32% increase, and the fragments carry less meaning. Combined with the
multilingual model's shorter input window (128 tokens against 256), this
also determines how often text is truncated.

### 13.5 Truncation is concentrated where it matters most

Against the 128-token window:

| | over the limit | of those with a description |
|---|---|---|
| MARC21 | 24 of 436 (5.5%) | **19 of 39 (49%)** |
| UNIMARC | 13 of 4,849 (0.3%) | 12 of 783 (2%) |

Overall truncation looks negligible. But **half the MARC21 works that
have a description at all lose part of it** — the richest field
available, clipped precisely when it is long enough to be worth having.

> **Design consequence.** Core text (title and subject headings) and
> description are encoded separately and their unit vectors averaged,
> rather than concatenated. Nothing is discarded. The average of two
> unit vectors must be renormalised, since averaging alone leaves the
> unit sphere.

### 13.6 Vector storage and query cost

Vectors are stored as `REAL[]` rather than with pgvector, which is not
in the stock postgres image. Measured:

| | |
|---|---|
| Fetch 5,284 vectors from PostgreSQL | **432 ms** |
| Convert to an ndarray | 46 ms |
| Dot product against all of them | **1 ms** |

The database round trip is 98% of a similarity request. Caching the
matrix in the API process takes an end-to-end query from 545 ms to
8–12 ms, and the cache is invalidated by a cheap probe — row count and
latest `created_at` — rather than a signal from the embedding service,
which runs in a different container and may not be running at all.

A full all-pairs matrix over 5,284 works takes 145 ms, but that is not a
query-time operation and does not scale: all pairs over 300,000 works
would be 100 GB. A single query vector against 300,000 is roughly 30 ms,
which is why pgvector is not yet needed.

---

## 14. Circulation history — the collaborative signal

### 14.1 Why OAI-PMH is not enough

Collaborative filtering factorises a user–item matrix, which needs
`(patron, work)` pairs. OAI-PMH carries none: patron data is the
privacy-sensitive part of a library's records and the protocol
deliberately does not expose it.

Item-level checkout totals *do* arrive, as MARC `952$l` (§9.3). But they
are aggregate — "this copy was borrowed 47 times" cannot be decomposed
into who borrowed it — so they support a popularity ranking and nothing
more.

### 14.2 What Koha's REST API offers

`api/v1/swagger/paths` in Koha main contains **no rating or review
route**. The `ratings` table exists and `OpacStarRatings` defaults to
enabled, but there is no supported way to read them over the API.

`/api/v1/checkouts` is available and gives what is actually needed:

| Field | Notes |
|---|---|
| `checkout_id` | the loan's own identifier |
| `patron_id` | Koha's `borrowernumber` |
| `item_id` | the physical copy |
| `item.biblio.biblio_id` | via the `x-koha-embed: item.biblio` header |
| `checkout_date`, `checkin_date` | loan duration is derivable |
| `renewals_count` | a candidate confidence signal |

Two mechanical details:

- **`checked_in=true`** returns returned loans; the default returns only
  current ones. Both are needed.
- **Pagination uses `_page` and `_per_page`** — the underscore prefix is
  required, and unprefixed names produce
  `400 Malformed query string`. Koha sends RFC 5988 `Link` headers, so
  `rel="next"` is the termination signal. Unlike OAI-PMH, an
  `X-Base-Total-Count` header is also provided.

Authentication is HTTP Basic when the `RESTBasicAuth` preference is
enabled, which is considerably simpler than the OAuth2 client-credentials
flow.

### 14.3 Patron identifiers are not stored

`patron_id` is Koha's `borrowernumber`. Storing it would link this
database to named library members and their complete borrowing history —
among the most sensitive data a library holds, and something many
libraries deliberately purge.

ALS needs identifiers that are **stable and distinct**. It does not need
to know who they are. So each is replaced by

```
patron_ref = HMAC-SHA256(borrowernumber, deployment_secret)[:32]
```

- **HMAC rather than a plain hash**, because a bare SHA-256 over a few
  hundred thousand integers is reversible by enumeration in seconds.
- **The secret has no default and its absence is fatal.** A shared
  default would make every deployment produce identical references,
  which would let two libraries' data be correlated — exactly what this
  is meant to prevent.

> **This is pseudonymisation, not anonymisation.** Anyone holding both
> the secret and the library's own database can re-identify. It bounds
> the damage from a leak of this database alone. It does not make the
> data non-personal, and a library's privacy policy still has to cover
> holding borrowing history at all.

### 14.4 Two identifier spaces, again

The REST API identifies a record by **biblionumber** (`345`). The OAI
harvest identifies the same record as **`KOHA-OAI-TEST:345`**, prefixed
by the operator-configurable archiveID.

This is the same shape as §4, and the same failure mode if joined
carelessly: loans attached to the wrong works, producing recommendations
that look plausible and are wrong. The mapping is therefore built
explicitly, keyed on the identifier suffix, with duplicate suffixes
counted rather than silently resolved to whichever row came first.

### 14.5 A collision that would have erased most of the signal

`source_loan_id` was initially populated from `item_id`. A copy is
borrowed repeatedly over its life, so two patrons' separate loans of the
same copy would have collided on the table's unique constraint and
overwritten each other.

The table would have filled. The row counts would have looked plausible.
Every copy would have shown exactly one loan — by whoever borrowed it
last — and most of the collaborative signal would have been silently
erased. `checkout_id` is the loan's own identifier.

### 14.6 What the test data does and does not show

Two generators were run against the MARC21 instance.

The **first cycled patrons and items round-robin**: 120 loans across 12
patrons. It verified the harvest path — 120 harvested, 120 stored, zero
unresolved, 436 catalogue records mapped, every `patron_ref` exactly 32
hex characters with no identifier leaked — and nothing beyond it,
because a perfectly uniform matrix has no popular titles, no heavy
borrowers and no renewals, which is to say none of the structure
collaborative filtering exploits.

The **second draws titles by Zipf rank across 50 patrons** and issues
renewals and occasional repeat borrows, so every branch of the
confidence formula has something to weigh:

| | Round-robin | Zipf | Real circulation |
|---|---|---|---|
| Loans | 120 | 338 | millions |
| Patrons | 12 | 50 | thousands |
| Loans per patron | exactly 10 | 50 down to 1 | heavily skewed |
| Matrix density | **17.9%** | **3.9%** | nearer 0.01% |
| Renewals | 0 | 52 | routine |
| Repeat pairs | 0 | 23 | common |
| Co-borrowed pairs | 34 | 191 | — |
| Loan duration | ~0 seconds | ~0 seconds | days to weeks |

Both generators issue and return within the same run, so loan *duration*
carries no signal in either and cannot be tested here at all.

A clean rebuild on the Zipf data alone gives the funnel that matters:
436 catalogue works, 218 loans, **87 interactions surviving filtering**,
25 patrons and 30 works factorised — **6.9% collaborative coverage**.
The other 93% has no collaborative signal and falls back to embeddings
entirely. That is the cold-start split the thesis staged for, in real
proportions.

### 14.7 The metric is positive and the recommendations are meaningless

Both at once, and the second is only visible by reading the output.

Co-borrowed pairs embed at +0.145 against −0.076 for unrelated pairs —
a **+0.221 gap**, so ALS learned real structure from 87 observations and
the sign is right. But the nearest neighbours of a Springsteen biography
are a special-education dissemination model and two C++ books. The
generator picks titles by rank with no notion of subject, so "patrons
who borrowed A also borrowed B" encodes random co-occurrence and nothing
else.

Had the gap been checked without reading the neighbours, this would have
passed as working.

The pipeline computes correctly. Output quality is a property of the
borrowing behaviour behind it, and real patrons borrow along themes that
no generator supplies. Nothing here predicts how the recommender
performs on a real catalogue. That needs a pilot library.

---

## 15. Ranking, and three things too thin to summarise

### 15.1 Collaborative filtering re-ranks; it does not retrieve

§14.7 recorded factors that carry a positive co-borrowing signal and
neighbours no reader would accept. The mechanism deserves naming,
because it determines where the collaborative layer can safely sit.

Those neighbours were produced by querying the factor space directly.
A work with no vocabulary in common with the query can rank first, if
the same patrons happened to borrow both — which, under a generator
that picks titles by popularity rank, is co-occurrence and nothing more.

So the layer re-ranks a content-retrieved pool instead. It reorders
candidates the embeddings already judged related; it cannot introduce
one they did not. With a noisy signal that shuffles plausible results
rather than surfacing implausible ones, and at zero coverage it reduces
to the content-only behaviour through the same code path rather than a
parallel one.

The cost is real and worth stating. Collaborative retrieval's
distinctive value is finding works that share readers but not
vocabulary, and a re-ranker cannot reach those. The candidate pool is
configuration so a deployment with real circulation can widen it, once
there is evidence to justify doing so.

### 15.2 Three floors, one reason

Every parameter added to the ranking layer exists because a statistic
computed over too little data is indistinguishable from one computed
over enough.

**`min_patrons`** — the only two collinear factor pairs in the corpus
(cosine 1.0000 and 0.9995) are works sharing their sole two borrowers.
ALS had no information to separate them, so it did not. Read as a
similarity that is the strongest possible endorsement; it is the
weakest possible vector.

**`min_scored`** — an absent collaborative score is imputed with the
median of the present ones, per query. Measured against the wired
endpoint, the candidate pool around the highest-evidence query held
**exactly one** work with usable factors. The median of one observation
is that work's own score, inherited by every other candidate: a
constant added to all six results, incapable of changing their order
and capable of moving every score arbitrarily. Its −0.123 had shifted
the entire response. Below the floor the layer abstains instead.

**`pool`** — bounds how far down the content ranking the collaborative
signal can reach, and so how close the layer sits to the retriever
§15.1 rejects.

None is tuned. `min_scored` was set to 3 on the reasoning that one or
two observations are not a distribution, *before* measuring how many
queries would clear it. That ordering matters: 4 of 11 eligible queries
reach the blend at 3, and none would at 5. A reader could reasonably
suspect the number was chosen to make the branch execute, and it was
not.

### 15.3 What the wired endpoint actually does

Against the 436-work corpus, with defaults:

| | |
|---|---|
| Works with factors | 30 (6.9%) |
| Clearing `min_patrons` | 11 |
| Scored candidates per pool | 0, 1, 2, 2, 2, 2, 2, 3, 3, 3, 4 |
| Queries reaching the blend | 4 |
| Largest sample any query gets | 4 observations |

Seven of eleven eligible queries abstain. Where the blend does run, the
median rests on three or four numbers. The visible ordering is content
order almost throughout.

The path executes end to end. Nothing here says it recommends well, and
§14.7's argument applies unchanged.

**Two questions, reported separately.** `hybrid_count` counts results
carrying collaborative evidence of their own; `collaborative_applied`
says whether the blend ran at all. They come apart: work 107 returns
`hybrid_count: 0` with every score blended, because it cleared
`min_scored` while none of its own top six carries factors. A single
count would have told a library circulation had no effect on results it
had moved.

### 15.4 Duplicate records, and why nothing was done about them

The nearest neighbour of a Springsteen biography is the same
Springsteen biography under a different `work_id` — a distinct
bibliographic record, so the query's own exclusion never sees it. On a
patron-facing catalogue that is a visible embarrassment.

Four instruments were tried against the 12 title-collision pairs in the
corpus, and each fails on a case another handles:

| Instrument | Fails on |
|---|---|
| Embedding cosine | Genuine duplicates where one record is title-only, which embed apart (0.862, 0.859). The metadata asymmetry that produces duplicate records is what makes their embeddings diverge. |
| ISBN match | Two editions of one work carry different ISBNs — `9780670026623` and `9781780335797` for the same book. |
| Title + author | Two volumes of *The art of computer programming* share both. Suppression would silently hide a distinct book. |
| Publication year | 1997/2005 for the volumes, `None`/2012 for the editions. Separates neither. |

What distinguishes them is **`245$n` and `$p`** — volume number and
part name. `fieldmap.py` builds `title` from `245$a` and `$b` (UNIMARC
`200$a`/`$e`), so the part designators never enter the schema, and raw
MARC is not retained, so they cannot be backfilled without a
re-harvest.

No suppression rule was written. Every available heuristic produces
either false positives on Knuth or false negatives on Springsteen, and
a ranker quietly hiding real books is worse than one showing a
duplicate. This is an ingestion-layer gap, and the frequency of
multi-volume sets in a real catalogue remains unmeasured.

There is a deeper point underneath. The Springsteen pair is not a
duplicate record at all — it is one work with two manifestations,
sitting in a table whose premise is that a row is the intellectual
work. That is the work-clustering problem, and it is not solvable with
a threshold.

### 15.5 A normaliser that erased 3.9% of the catalogue

While measuring the above, a title normaliser stripped everything
outside `[a-z0-9 ]`. Seventeen records — in Russian, Chinese and Arabic
— normalised to the empty string and collided with one another,
reported as a 17-way duplicate group. **3.9% of this corpus**, declared
identical on the basis of having no Latin characters.

The measurement was wrong and the mechanism matters more than the
correction. This system's stated purpose includes Cambodian libraries,
where Khmer script is not an edge case, and §13 argues for multilingual
embeddings on precisely those grounds. Had that normaliser reached
ingestion or a deduplication pass, every Khmer-titled work would have
been declared a duplicate of every other Khmer-titled work — silently,
with plausible row counts.

Normalisation must be script-agnostic (`str.isalnum()`, not an ASCII
range), and any grouping key must refuse degenerate values rather than
treat them as a match. The 3.9% figure is worth keeping as a rough
estimate of what an ASCII assumption destroys in a collection of this
kind.

Note that even the corrected normaliser still collapses `C++` to `c`,
because `+` is not alphanumeric. That pair was caught by a second
check, not the normaliser — which is the argument for corroboration
over a single instrument, in miniature.

---

## 16. Limitations of this analysis

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
8. **The circulation data is synthetic, and the ALS output is
   meaningless.** The better of two generators reaches 3.9% matrix
   density against a real library's ~0.01%, returns every loan within
   the same second, and picks titles by popularity rank with no notion
   of subject. Factorisation over it produces a positive co-borrowing
   gap (+0.221) and neighbours no reader would accept (§14.6). It
   demonstrates that harvesting, storage and factorisation run; it
   supports no claim whatever about recommendation quality.
9. **The model comparison uses subject-heading agreement as a proxy for
   quality.** Works sharing a cataloguer-assigned heading should embed
   closer together, which is a reasonable signal but not the same as
   measuring whether patrons find the recommendations useful. That needs
   a deployed system and real patron behaviour; the thesis's Future Work
   item on a user satisfaction study is the right instrument, and none
   of the retrieval quality claimed here has been validated against
   actual readers.
10. **Retrieval was inspected on a handful of probes.** The neighbours in
   §13.3 are convincing, but they are three queries chosen by someone
   who wanted the model to work. No systematic evaluation was run.
11. **Ranking parameters are unvalidated.** Three floors and a pool
    size, all configuration, none tuned against data that could tune
    them. The defaults are reasoning, not results (§15.2).
12. **The hybrid path is barely exercised.** 4 queries of 436 works
    reach the blend, over samples of 3 or 4 observations. That the
    branch runs is established; its behaviour at realistic coverage is
    not (§15.3).
13. **Duplicate frequency is unmeasured.** 12 title collisions in 436
    records, but the instrument that found them misses the case that
    prompted the search, and `245$n`/`$p` is absent from the schema
    entirely (§15.4).
14. **Both corpora are Koha sample data.** The UNIMARC set is larger
   (4,849) and so carries less sampling error than the MARC21 set, but
   it is still synthetic and skewed — 94.6% French, 8.6% sound
   recordings.

---

## 17. Appendix — scripts

### 17.1 Harvest loop

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

### 17.2 Field profiling

The frequency table (§5.1) and coverage analysis (§6, §7) were produced
by Python scripts using `xml.etree.ElementTree` against the harvested
pages. Both parse the OAI namespace
(`http://www.openarchives.org/OAI/2.0/`) and the MARC21 slim namespace
(`http://www.loc.gov/MARC21/slim`) separately — note that `<record>`
appears in **both**, so unqualified tag matching produces wrong results.

---

*Compiled 2026-08-23. All figures measured, not estimated.*
