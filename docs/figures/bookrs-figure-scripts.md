# BookRS-Platform — figure descriptions and presentation script

Three figures. For each: a caption, a written description for the report, a
spoken script, and the questions that figure invites.

Total spoken time if you present all three: about six minutes.

Order matters. Figure 1 answers "what is it", Figure 2 answers "does it work on
real systems", Figure 3 answers "how is it built". Present them in that order
even if the report prints them differently.

---

# Figure 1 — Deployment Architecture

## Caption

**Figure 1 — BookRS-Platform deployment architecture.** Five services under
Docker Compose, self-hosted by the library. The library's own system is read
through its published interfaces only; no component writes to it, and no
component holds a direct connection to its database.

## Description for the report

BookRS-Platform runs as five containers alongside the library's existing system
rather than inside it. Cataloguing data enters through OAI-PMH and circulation
data through Koha's REST API, both read-only. Everything the platform derives —
normalised works and items, sentence embeddings, collaborative factors — is held
in its own PostgreSQL 17 database, which is the single point of contact between
services.

The separation of stages is deliberate. Ingestion runs on demand or on a
schedule; embedding runs after a harvest; the recommend service runs as a
periodic batch; only the API runs continuously. The embedding service selects
what is stale rather than re-encoding everything: each vector row carries the
source hash, the model name and the embedder version, so "needs re-embedding"
is a single SQL predicate and a second run over an unchanged catalogue does
nothing. Because the API reads vectors and
factors that other services have already written, it never loads a model itself,
which is why it is the smallest of the four service images at 361 MB against the
embedding service's 1.96 GB. A library therefore pays the memory cost of the
model only while retraining, not while serving.

## Spoken script (about 100 seconds)

> This is the deployment view — what a library actually installs.
>
> The important thing is at the top. Their system stays untouched. We read the
> catalogue over OAI-PMH and circulation over Koha's REST API, both read-only.
> We never write to their database and we never connect to it directly. That
> wasn't a convenience decision — a library will not let an outside system near
> its patron records, and if the integration required that, nobody would adopt
> it.
>
> Everything we derive lives in our own PostgreSQL database, in the middle here.
> That's the only thing the four services share.
>
> And they run on different schedules. Ingestion runs when you harvest.
> Embedding runs after a harvest. The recommend service runs as a periodic batch.
> Only the API runs all the time — and because it reads vectors and factors the
> other services already wrote, it never loads a model. That's why it's 361
> megabytes and the embedding service is nearly two gigabytes. The library only
> pays for the model while it's retraining, not while it's answering patrons.
>
> At the bottom, the patron sees suggestions on their own catalogue page. No new
> interface to learn, nothing for them to visit.

## Questions this figure invites

**"Why not just query our database directly? It would be faster."**
It would, and it would also make us responsible for their schema and their patron
data. OAI-PMH and the REST API are published interfaces with stability
guarantees; a database schema is not. A direct connection would also break on
their next upgrade.

**"What happens if your platform goes down?"**
Their catalogue is unaffected. The widget fails silently and the page renders as
it always did. Nothing in their system depends on ours.

**"Two gigabytes for the embedding service — can our server handle that?"**
It only runs during a harvest or a refit. The always-running part is the API at
361 MB plus PostgreSQL. A modest server handles it.

**"Why a multilingual model rather than an English one?"**
It was measured, not assumed. On a same-subject similarity gap over real
catalogue data, the multilingual model scored +0.604 on English where the
English-only model scored +0.597 — effectively the same — and +0.301 on French
where the English-only model managed +0.169. Both produce 384 dimensions, so
the schema is unchanged. The costs are a larger download and about a third of
the throughput.

---

# Figure 2 — Integration and Ingestion

## Caption

**Figure 2 — Integration with three live library systems.** Two Koha instances in
different MARC flavours and a local PMB instance, each harvested through its own
endpoint. Flavour detection is structural rather than label-based; circulation
data is pseudonymised before it reaches storage.

## Description for the report

The platform was developed against three live systems rather than sample files: a
Koha MARC21 instance with 436 records and 961 items, a Koha UNIMARC instance with
4,849 records, and a PMB instance running locally in Docker.

Working against real endpoints exposed a problem that documentation does not. The
UNIMARC instance serves its records under the metadata prefix `marc21` and in the
MARC21/slim namespace, so the format label is wrong. Detection therefore cannot
trust what a record says it is; `flavour.py` probes for the presence of tag 245
against tag 200 and decides structurally. PMB required a further step, since its
export mixes namespaced and bare XML, and `pmb.py` translates it into the common
representation before field mapping.

Circulation follows a separate path, and from Koha alone. PMB implements loan,
reservation and reader services but ships them disabled in
`external_services/catalog.xml`, so no PMB installation exposes them; the
asymmetry is that system's configuration rather than an unfinished integration.
Checkouts are read from Koha's REST endpoint
and passed through `circulation.py`, which replaces each borrower number with a
keyed HMAC-SHA256 digest truncated to 32 characters. The key is a per-deployment
secret, so the pseudonym cannot be reversed by anyone holding only the database,
and no raw borrower identifier is ever written to storage.

## Spoken script (about 120 seconds)

> This is the integration view, and the reason it matters is that everything here
> was built against live systems, not sample files.
>
> Three of them. A Koha instance in MARC21 with 436 records. A second Koha
> instance in UNIMARC with 4,849. And a PMB instance I built and run locally —
> PMB because it's French-origin, and a lot of Cambodian libraries run
> French-lineage systems.
>
> Running against real endpoints is what caught the problem in the middle of this
> diagram. The UNIMARC instance serves its records labelled `marc21`, in the
> MARC21 namespace. The label is simply wrong. If I'd trusted it, every UNIMARC
> record would have been parsed with the wrong field map and the output would
> still have looked like valid data — just silently wrong.
>
> So detection is structural. `flavour.py` looks for whether the record carries
> tag 245 or tag 200 and decides from the record itself. That's the difference
> between checking that something ran and checking that it ran correctly.
>
> PMB needed one more step — its export mixes namespaced and bare XML, so there's
> a translation shim before field mapping.
>
> The right-hand path is circulation, and it's separate on purpose. Checkouts
> come from Koha's REST API, and before anything is stored, `circulation.py`
> replaces the borrower number with a keyed HMAC. The key is per-deployment. No
> raw borrower identifier ever reaches our database — not encrypted, not hashed
> and reversible. It's never written.

## Questions this figure invites

**"Is the PMB instance running now?"**
It can be — it runs locally under Docker. PMB harvesting was proven end to end,
including incremental change detection: a re-harvest reported 40 new, 8
unchanged, recognising the earlier records by content hash. Standing it up is
`cd ~/projects/pmb-docker && docker compose up -d`, and `scripts/evidence.sh`
probes all three instances live rather than citing what was verified once.

**"Why a keyed HMAC and not just a hash?"**
A plain hash of a borrower number is reversible by anyone who can guess borrower
numbers — and they're sequential integers, so that's trivial. The key is what
makes it irreversible. It's held per deployment, so it never leaves the library.

**"Could you support other systems?"**
The ingestion layer is flavour-aware by design, so a new system needs a field map
and, if its export is unusual, a translation shim. PMB is the proof that adding
one is tractable.

**"Why is circulation Koha only? Doesn't PMB lend books?"**
It does, but it doesn't publish the data. PMB's JSON-RPC and SOAP connectors take
their method inventory from `external_services/catalog.xml`, and three groups —
`pmbesResas`, `pmbesLoans` and `pmbesReaders`, ids 24 to 26 — are present on disk
with complete manifests and commented out in that file. The catalogue never loads
them, so no installation can export them. Borrower management is registered and
available, but identities without loans carry no recommendation signal, so the
platform doesn't request those either. I checked this against a running instance
rather than assuming PMB behaves like Koha.

**"4,849 records is small. Will this work at our scale?"**
Harvesting is incremental with resumption tokens, and the thesis system behind
this was validated on a dataset of 883,468 books. Scale is not the open question
— real circulation data is.

---

# Figure 3 — Module Architecture

## Caption

**Figure 3 — Module architecture.** The eleven ingestion modules, the two model
services, the query API and the database tables they share. Each module is a
separate stage so that each can be tested in isolation.

## Description for the report

The module view shows why the system is testable. Ingestion is eleven modules
arranged as a pipeline: endpoint verification, harvesting, flavour detection, PMB
translation, normalisation, language detection, field mapping, alternate-script
resolution, circulation pseudonymisation, and the two entry points that compose
them. Each stage takes a defined input and produces a defined output, so a fault
can be attributed to a stage rather than to the pipeline as a whole.

The separation between the recommend service and the API deserves note. The
ranking logic lives in `bookrs/recommend/rank.py` but executes inside the API at
request time; it imports NumPy alone, never the `implicit` library that the ALS
trainer depends on. The boundary is enforced by an automated test rather than by
convention, which is what keeps the public-facing image free of a C toolchain.

The `ratings` table appears in the schema but is unpopulated. Ratings are absent
from library circulation data, and the table is retained for a future source
rather than presented as a working signal.

## Spoken script (about 110 seconds)

> This is the module view. It's dense, so I'll point at three things.
>
> First, the left column. Ingestion is eleven separate modules, not one script.
> Verify the endpoint, harvest, detect the flavour, translate PMB, normalise,
> detect language, map fields, resolve alternate scripts, pseudonymise
> circulation, and two entry points that compose them. Every stage has a defined
> input and output, which means each one is independently testable. That's why
> the repository has 379 tests with none skipped — the architecture makes them
> possible to write.
>
> Second, `language.py` and `altscript.py`. Field 880 carries alternate-script
> versions of a title — the Khmer original where the main field holds a
> transliteration. Early on, the field map read 245 and ignored 880, which meant
> the records that justified choosing a multilingual model were exactly the ones
> being fed transliterated text instead of native script. Resolving 880 fixed
> that.
>
> Third, `rank.py`, at the bottom. It lives in the recommend package but runs
> inside the API, and it imports NumPy only — never the ALS library. That's
> what keeps the public-facing service small and free of a C toolchain, and
> there's a test that fails if anyone breaks the rule.
>
> One honest note: the `ratings` table is in the schema but empty. Library
> circulation doesn't produce ratings. It's there for a future source, not
> presented as something that works.

## Questions this figure invites

**"Eleven modules for ingestion seems like a lot."**
Each one is a stage that can fail differently. Field mapping failing and language
detection failing are different problems with different fixes; separating them is
what makes the failure attributable.

**"Why does rank.py live in the recommend package if it runs in the API?"**
Because it belongs with the recommendation logic conceptually, but the API needs
to call it at request time. Keeping it NumPy-only is what lets both be true
without the API pulling in the training dependencies.

**"What's the 880 field?"**
MARC's alternate-script field. A record can hold a title in Latin transliteration
in field 245 and the original script — Khmer, Chinese, Arabic — in 880. For a
multilingual catalogue it's the field that actually matters.

**"How do you know the recommendations are good?"**
I don't yet, and that's the honest answer. The pipeline is verified; the
recommendation quality can't be judged until it runs against a real library's
circulation history. That's the next step, and it's why I'd like to pilot with
your library.

---

# Presenting all three together

If you have one minute rather than six:

> Three views of the same system. The first is what a library installs — five
> containers beside their existing catalogue, reading it read-only, never writing
> to it. The second is the integration, built against three live systems, where
> the format labels turned out to be unreliable and detection had to be
> structural. The third is the internals — eleven ingestion stages, each
> separately testable, which is what 379 tests rest on.
>
> What's proven is the pipeline. What isn't proven is recommendation quality,
> because that needs a real library's circulation history. That's what I'd like
> to talk about.

Ending on the open question is deliberate. It's accurate, and it puts the pilot
request in his hands rather than making him extract it.
