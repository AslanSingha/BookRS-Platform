# BookRS-Platform — figure descriptions and presentation notes

Four figures. For each: a caption, a written description for the report, what to
say when showing it, and the questions it invites.

Current as of commit `b2530e6`. A 125,333-work catalogue across three live
library systems.

Order matters. Figure 1 answers "what does a library install", Figure 2 "does
it work against real systems", Figure 3 "how is it built", Figure 4 "what does
a patron get, and is it any good". Figure 4 carries the only measured quality
result in the project, so if there is time for one figure, show that one.

---

# Figure 1 — Deployment architecture

## Caption

**Figure 1 — BookRS-Platform deployment architecture.** Five services under
Docker Compose, self-hosted by the library. The library's own system is read
through its published interfaces only; no component writes to it, and none
holds a direct database connection.

## Description for the report

BookRS-Platform runs as five containers alongside the library's existing
system. Cataloguing enters through OAI-PMH and circulation through Koha's REST
API, both read-only. Everything derived — normalised works and items, sentence
embeddings, collaborative factors — is held in the platform's own PostgreSQL
database, the single point of contact between services.

The stage separation is deliberate. Ingestion runs on demand or on a schedule,
embedding after a harvest, the recommend service as a periodic batch, and only
the API runs continuously. Embedding selects what is stale rather than
re-encoding everything: each vector row carries the source hash, the model name
and the embedder version, so a second run over an unchanged catalogue does
nothing.

The API is 516 MB against the embedding service's 1.96 GB. The stored-vector
endpoints — similarity, exact search, availability — need no model at all.
Semantic search does, and it uses an ONNX Runtime export of the same model the
embedding service runs, loaded lazily on first request and kept in process:
roughly a second to load, tens of milliseconds per query, and no PyTorch in the
image. `tools/parity_test.py` verifies the ONNX encoder produces the same
vectors as the SentenceTransformer one, which is what makes the quantised path
trustworthy.

## What to say

> Five containers a library installs beside their existing catalogue. They read
> it over OAI-PMH and the REST API, read-only, and write nothing back. No
> direct database connection — that is the decision that makes this installable
> at all, because a library will not grant credentials to the server holding
> patron records.
>
> The services run on different schedules. Only the API runs continuously, and
> most of what it does needs no model — similarity is a matrix multiply against
> vectors another service already wrote. Semantic search needs the query
> encoded, so there is an ONNX export of the same model in that image. No
> PyTorch, about 155 megabytes, and a parity test that checks it produces the
> same vectors as the full model.

## Questions it invites

**"Why not read our database directly? It would be faster."**
It would, and it would make us responsible for your schema and your patron
data. OAI-PMH and the REST API are published interfaces with stability
guarantees; a database schema is not, and a direct connection would break on
your next upgrade.

**"What happens if your service goes down?"**
The catalogue is unaffected. The widget fails silently and the page renders as
it always did.

**"Two gigabytes for embedding — can our server handle it?"**
It runs during a harvest or a refit. The always-running part is the API at
516 MB plus PostgreSQL.

---

# Figure 2 — Integration and ingestion

## Caption

**Figure 2 — Integration with three live library systems.** Two Koha instances
in different MARC flavours and a self-hosted PMB, each harvested through its
own endpoint. Flavour detection is structural rather than label-based;
circulation is pseudonymised before it reaches storage.

## Description for the report

The platform was developed against three running systems rather than sample
files: Koha MARC21 with 50,436 works, Koha UNIMARC with 54,849, and PMB with
20,048. The bibliographic records are drawn from Open Library, sampled for
works carrying a description and at least two subject headings, written out as
MARC21 and as UNIMARC, and loaded into each system through its own import path.
Each system therefore holds real catalogue records in its native format.

Working against real endpoints exposed what documentation does not. The UNIMARC
instance serves its records under the metadata prefix `marc21`, in the
MARC21/slim namespace, so the format label is wrong. Tag numbers also collide
between the standards — 100, 300 and 020 exist in both with unrelated meanings
— which means a MARC21 field map applied to a UNIMARC feed produces plausible
nonsense rather than an error. Detection therefore probes record structure.

PMB required a further step: its export mixes namespaced and bare XML, `oai_dc`
returns HTTP 500 on that build, and the format has no adequate published
specification, so the translation shim was reverse-engineered field by field
against a running instance.

Circulation follows a separate path, and from Koha alone. Checkouts are read
from Koha's REST endpoint and passed through `circulation.py`, which replaces
each borrower number with a keyed HMAC-SHA256 digest truncated to 32
characters. The key is a per-deployment secret, so the pseudonym cannot be
reversed by anyone holding only the database.

## What to say

> Three systems, one pipeline. Fifty thousand records in MARC21, fifty-four
> thousand in UNIMARC, twenty thousand in PMB.
>
> The middle of this diagram is where working against live systems paid for
> itself. The UNIMARC instance serves its records labelled `marc21`, in the
> MARC21 namespace. The label is simply wrong — and because tag numbers collide
> between the two standards, trusting it would produce output that still looked
> like valid data. So detection reads the record's structure instead.
>
> The right-hand path is circulation, separate on purpose. Before anything is
> stored, the borrower number is replaced with a keyed digest. No raw
> identifier is ever written.

## Questions it invites

**"Where did the records come from?"**
Open Library, sampled for works with a description and at least two subject
headings, converted to both MARC flavours and imported into each system through
its own path. Real catalogue records in each system's native format, harvested
as a library's own would be.

**"Why a keyed HMAC rather than a hash?"**
A plain hash of a borrower number is reversible by anyone who can guess
borrower numbers, and they are sequential integers. The key makes it
irreversible, and it is held per deployment.

**"Could you support other systems?"**
The ingestion layer is flavour-aware by design. A new system needs a field map
and, if its export is unusual, a translation shim. PMB is the proof that adding
one is tractable.

---

# Figure 3 — Module architecture

## Caption

**Figure 3 — Module architecture.** The ingestion modules, the two model
services, the query API and the database tables they share. Each module is a
separate stage so a fault can be attributed to a stage rather than to the
pipeline.

## Description for the report

Ingestion is a pipeline of separate modules: endpoint verification, harvesting,
flavour detection, PMB translation, normalisation, language detection, field
mapping, alternate-script resolution, circulation pseudonymisation, and the
entry points that compose them. Each takes a defined input and produces a
defined output.

The separation between the recommend service and the API deserves note. Ranking
lives in `bookrs/recommend/rank.py` but executes inside the API at request
time; it imports NumPy alone, never the `implicit` library the ALS trainer
depends on, and the boundary is enforced by a test rather than by convention.
That is what keeps the public-facing image free of a C toolchain.

The API's search modules divide the same way. Exact search is trigram matching
over stored text and needs no model. Semantic search encodes the query through
`encoder_onnx.py`, an ONNX Runtime export whose masked mean pooling is matched
to the embedding service's encoder exactly.

The `ratings` table is present and unpopulated. Koha collects ratings natively
and its REST API exposes no route to read them, so the table is retained for a
future source rather than presented as a working signal.

## What to say

> Ingestion is separate modules, not one script, which is why a fault is
> attributable to a stage. That is also what the test suite rests on.
>
> Two things worth pointing at. `altscript.py` resolves MARC field 880, which
> carries the original-script form of a title where 245 holds a romanisation —
> the Khmer or Arabic original against a Latin transliteration. Early on the
> field map read 245 and ignored 880, which meant the records that justified
> choosing a multilingual model were the ones being fed transliterated text.
>
> And in the API, ranking lives in the recommend package but runs here, and
> imports NumPy only — never the ALS library. A test fails if anyone breaks
> that.
>
> One honest note: the ratings table is in the schema and empty. Koha collects
> ratings and publishes no way to read them.

## Questions it invites

**"That is a lot of modules for ingestion."**
Each is a stage that fails differently. Field mapping failing and language
detection failing are different problems with different fixes.

**"What is field 880?"**
MARC's alternate-script field. A record can hold a romanised title in 245 and
the original script — Khmer, Arabic, Hebrew — in 880. For a multilingual
catalogue it is the field that matters.

---

# Figure 4 — Search tiers and the measured result

## Caption

**Figure 4 — What a patron sees, and what it is worth.** Three tiers, each
additive, with the catalogue's own ordering never touched. The rescue tier is
measured against the catalogue's own search.

## Description for the report

The widget does three things, in increasing order of how much they interfere
with the catalogue's own behaviour. A panel of related books on a record page,
drawn from the library's own holdings with availability. Zero-result rescue on
a search page, answering the same query by meaning under a labelled heading and
appearing only when the catalogue found nothing. And a related band appended
below real results, at a stricter floor, containing only works the catalogue
did not already show.

Search quality is measured. The query set was built from the catalogue's own
subject headings, which makes it independent of the recommender by
construction, in the three ways keyword search fails: a question in ordinary
words, a topic phrase no title carries, and a transposed-letter typo. 120
queries, 40 of each, run through Koha's Zebra search and through
`/search/semantic` at a 0.55 floor.

Zebra found nothing for 77 of the 120. BookRS answered 69 of those — 90%
overall, 100% on natural language and concept phrases, 79% on typos.

Three qualifications belong with that number. It is a rescue rate rather than a
relevance score: `tools/evaluate.py` writes a judgement sheet of
query-suggestion pairs for a librarian to mark, because a generated label would
be the embedding grading its own output. Typos are the weak class because a
transposed letter damages the embedding as much as the keyword index —
semantic search is not a spell checker, and the trigram indexes exact search
already uses are where a fallback belongs. And a score floor cannot separate a
question from a non-question: `trsut` returns three short foreign-language
titles at 0.91, 0.80 and 0.77.

The threshold was calibrated rather than chosen. Across twenty queries against
the live catalogue, every sensible top result scored 0.579 or above and the two
wrong ones scored 0.534 and 0.500.

## What to say

Show the live rescue first, then the number.

> Koha's own search found nothing for that query. Below it, the same query
> answered by meaning, under a heading that says what it is. It appears only
> because the catalogue found nothing — it never competes with the librarian's
> ranking.
>
> This is measured. A hundred and twenty queries built from the catalogue's own
> subject headings, which makes the query set independent of the recommender.
> Zebra found nothing for seventy-seven of them. We answered sixty-nine — ninety
> per cent.
>
> Two things that does not say. Typos are the weak class at seventy-nine per
> cent, and that is expected: a transposed letter damages the embedding too.
> And it is a rescue rate, not a relevance rate — it says something was
> returned, not that it was good. The tool writes a judgement sheet for a
> librarian to mark, because a label we generated would be the embedding
> grading its own output.

That last sentence is the one to deliver clearly. It is the difference between
a measured claim and an impressive-sounding one.

## Questions it invites

**"How do I know the queries were not chosen to flatter it?"**
They were generated from the catalogue's own subject headings, which makes the
set independent of the recommender by construction, and split evenly across the
three failure classes before anything was run.

**"Would you score the judgement sheet?"**
It holds 69 query-suggestion pairs and needs a librarian rather than me. An
afternoon's work, and it converts the rescue rate into a relevance rate.

**"Does this replace the catalogue's search?"**
No, at any tier. Tier 2 appears only when the catalogue found nothing. Tier 3
appends below its results and contains only works it did not already show. The
catalogue's ordering is never modified.

---

# Presenting all four in one minute

> Four views. What a library installs — five containers beside their catalogue,
> reading it read-only, writing nothing back. How it reaches three different
> library systems, where the format labels turned out to be unreliable and
> detection had to read record structure instead. How it is built, as separate
> stages that fail separately. And what a patron gets: related books on a
> record, and an answer when the catalogue's own search finds nothing — which
> is the one part with a measured number behind it, ninety per cent of the
> queries Zebra could not answer.
>
> What is measured is search. What is not measured is recommendation quality,
> because factorisation has only ever run on generated circulation. That needs
> a library.
