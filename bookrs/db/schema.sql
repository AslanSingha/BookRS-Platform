-- BookRS-Platform schema.
--
-- Column types and sizes follow measurements against real Koha
-- catalogues (436 MARC21 records, 4,849 UNIMARC); see
-- docs/marc-field-analysis.md. Where a measured maximum exists it is
-- noted, with headroom above it.

-- ---------------------------------------------------------------
-- sources: one row per integrated library system.
--
-- Every deployment serves exactly one library, but provenance is kept
-- so a record can always be traced back to the endpoint it came from,
-- and so a second source can be added without migration.
-- ---------------------------------------------------------------
CREATE TABLE sources (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(120)  NOT NULL,
    base_url        TEXT          NOT NULL,
    -- Operator-supplied. Never derived from the endpoint's advertised
    -- baseURL, which Koha reports incorrectly (docs section 2.3).
    metadata_prefix VARCHAR(32)   NOT NULL DEFAULT 'marc21',
    marc_flavour    VARCHAR(16),  -- detected, not configured
    last_harvest    TIMESTAMPTZ,
    -- Carried forward between runs so a sudden loss of holdings is
    -- treated as a configuration regression (docs section 12.1).
    last_had_items  BOOLEAN,
    UNIQUE (base_url, metadata_prefix)
);

-- ---------------------------------------------------------------
-- works: the bibliographic record.
--
-- What the recommendation engine reasons about. One embedding and one
-- factor row per work, regardless of how many physical copies exist.
-- ---------------------------------------------------------------
CREATE TABLE works (
    id               BIGSERIAL PRIMARY KEY,
    source_id        INTEGER      NOT NULL REFERENCES sources(id) ON DELETE CASCADE,

    -- The full OAI identifier ("KOHA-OAI-TEST:1"), not the bare
    -- biblionumber: GetRecord requires the full form, and the archiveID
    -- prefix is operator-configurable. Unique within a source in both
    -- reference corpora (436/436 and 4849/4849).
    source_record_id VARCHAR(255) NOT NULL,

    title            TEXT         NOT NULL DEFAULT '',   -- measured max 391
    publisher        VARCHAR(255)          DEFAULT '',   -- measured max 199
    publication_year SMALLINT,

    -- TEXT rather than VARCHAR: a 520 summary reached 3,284 characters
    -- and a 505 contents note 1,417. Both exceed SBERT's input window
    -- and will need truncation at embedding time, not storage time.
    summary          TEXT         NOT NULL DEFAULT '',
    contents         TEXT         NOT NULL DEFAULT '',

    -- Arrays rather than join tables. These are read whole, never
    -- queried individually, and a join table for a field with a
    -- measured maximum of 15 entries buys nothing.
    authors          TEXT[]       NOT NULL DEFAULT '{}', -- max 15 per work
    subjects         TEXT[]       NOT NULL DEFAULT '{}', -- max 51 per work
    isbns            VARCHAR(13)[] NOT NULL DEFAULT '{}',-- max 9 per work
    -- VARCHAR, not CHAR: PostgreSQL pads CHAR to its declared width and
    -- the driver returns the padding, so a value shorter than the width
    -- compares equal in SQL but unequal in Python. MARC language codes
    -- are three characters, but malformed two-character ISO 639-1 codes
    -- do occur in real catalogues.
    languages        VARCHAR(3)[] NOT NULL DEFAULT '{}', -- max 3 per work

    -- Which MARC field each value came from. A wrong title is far
    -- easier to diagnose when the record says it came from 200$a.
    provenance       JSONB        NOT NULL DEFAULT '{}',

    -- MARC 005 for MARC21, absent under UNIMARC. Used to decide whether
    -- a re-harvested record needs re-embedding: circulation bumps the
    -- OAI datestamp without changing bibliographic content, so
    -- re-embedding on datestamp alone would be enormously wasteful
    -- (docs section 9.2).
    marc_005         VARCHAR(20),
    -- VARCHAR for the same reason as languages above. A SHA-256 digest
    -- is always 64 characters so production data would never trigger the
    -- padding, but a column whose correctness depends on every value
    -- exactly filling its width is a trap: any shorter value would
    -- silently never match, and every record would look modified
    -- forever.
    content_hash     VARCHAR(64),  -- bibliography only; governs re-embedding
    -- Holdings only. Kept separate because the two change independently:
    -- a checkout moves the items and not the bibliography, and a
    -- catalogue correction does the reverse. One combined hash would
    -- either re-embed the catalogue on every loan or leave availability
    -- stale between full syncs.
    items_hash       VARCHAR(64),

    first_seen       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at       TIMESTAMPTZ,

    UNIQUE (source_id, source_record_id)
);

-- ISBN is the entity-resolution key but NOT unique: 52 ISBNs in the
-- UNIMARC corpus are shared by 111 works, and inspection confirms these
-- are genuine duplicate catalogue records (same title, author, year and
-- publisher under two biblionumbers) rather than distinct works. One
-- pair differs only by a missing space in the title and a differently
-- recorded publisher -- which a title+author key would miss and ISBN
-- catches. Hence a plain index, and merging handled in application code.
CREATE INDEX works_isbns_idx     ON works USING GIN (isbns);
CREATE INDEX works_subjects_idx  ON works USING GIN (subjects);
CREATE INDEX works_authors_idx   ON works USING GIN (authors);
CREATE INDEX works_source_idx    ON works (source_id) WHERE deleted_at IS NULL;

-- ---------------------------------------------------------------
-- items: the physical copy.
--
-- Real-time inventory, deliberately outside the ML pipeline. A work
-- may have none: 25 of 436 MARC21 records had zero holdings, which is
-- normal for on-order titles, electronic resources and catalogue-only
-- entries (docs section 8.1).
-- ---------------------------------------------------------------
CREATE TABLE items (
    id             BIGSERIAL PRIMARY KEY,
    work_id        BIGINT       NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    barcode        VARCHAR(64),
    owning_branch  VARCHAR(32),  -- 952$a / 995$c -- note the subfield differs
    holding_branch VARCHAR(32),  -- 952$b / 995$b -- an item can be held elsewhere
    location       VARCHAR(120),
    call_number    VARCHAR(120),
    item_type      VARCHAR(32),  -- governs loanability; not every type circulates
    -- Present only while on loan. Absence means available.
    due_date       DATE,
    -- 952$l, cumulative issues. MARC21 only: Koha's default UNIMARC
    -- framework does not map it (docs section 10.8).
    issue_count    INTEGER,
    last_seen      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX items_work_idx    ON items (work_id);
CREATE INDEX items_barcode_idx ON items (barcode);

-- ---------------------------------------------------------------
-- ratings: patron ratings read from the ILS.
--
-- Koha collects these natively (OpacStarRatings defaults to enabled)
-- and stores (borrowernumber, biblionumber, rating_value). They are
-- NOT in the MARC record and do not arrive via OAI-PMH, so this table
-- is populated through a separate REST channel (docs section 11).
--
-- How much data exists varies entirely by deployment and is measured at
-- setup rather than assumed.
-- ---------------------------------------------------------------
CREATE TABLE ratings (
    source_id    INTEGER     NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    patron_ref   VARCHAR(64) NOT NULL,   -- opaque; never a name or card number
    work_id      BIGINT      NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    rating       SMALLINT    NOT NULL CHECK (rating BETWEEN 1 AND 5),
    rated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, patron_ref, work_id)
);

CREATE INDEX ratings_work_idx ON ratings (work_id);

-- ---------------------------------------------------------------
-- embeddings: one vector per work.
--
-- REAL[] rather than a native vector type. pgvector is not present in
-- the stock postgres image, and adopting it would mean asking a
-- library's IT staff to run a non-standard image for a capability not
-- yet needed: one query vector against 5,284 stored vectors is well
-- under a millisecond in NumPy, and against 300,000 is roughly 30 ms.
-- (Measured: the full all-pairs matrix over 5,284 works takes 145 ms,
-- but that is not a query-time operation and does not scale -- all
-- pairs over 300,000 works would be 100 GB.) The vectors are the same
-- numbers either way, so moving to pgvector later is a migration
-- rather than a redesign.
--
-- Vectors are L2-normalised at generation, so a dot product is cosine
-- similarity and no scaling is needed at query time.
-- ---------------------------------------------------------------
CREATE TABLE embeddings (
    work_id          BIGINT      PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
    vector           REAL[]      NOT NULL,
    dimensions       SMALLINT    NOT NULL,

    -- Which model and which composition produced this vector. A change
    -- to either makes stored vectors incomparable to new ones, and
    -- mixing two vector spaces produces plausible nonsense rather than
    -- an error -- the same failure the ingestion mapper version guards
    -- against, one layer up.
    model            VARCHAR(120) NOT NULL,
    embedder_version SMALLINT     NOT NULL,

    -- The content hash the vector was generated from. Re-embedding is
    -- needed when this no longer matches the work's current hash.
    source_hash      VARCHAR(64)  NOT NULL,

    -- Roughly a third of both reference corpora have nothing but a
    -- title to encode (29.4% MARC21, 38.5% UNIMARC). Recorded so the
    -- ranking layer can weigh a title-only vector differently rather
    -- than treating it as equivalent evidence.
    is_title_only    BOOLEAN      NOT NULL DEFAULT FALSE,

    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Finds work needing (re-)embedding: a model change, a version bump, or
-- a source record whose bibliography moved.
CREATE INDEX embeddings_staleness_idx
    ON embeddings (model, embedder_version, source_hash);
CREATE INDEX embeddings_title_only_idx
    ON embeddings (is_title_only) WHERE is_title_only;
