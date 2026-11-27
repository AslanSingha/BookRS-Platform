# Demo run sheet: BookRS in Koha

About 15 minutes. It shows what the supervisor asked for on 28 Oct:

- recommendations inside Koha's own OPAC, made from Koha's own data,
  which the platform only reads;
- a new reader whose activity visibly changes what they are shown;
- retraining on a cadence the library sets.

It also shows search by meaning and similar books. The terminal side is
`tools/demo.sh` (run it with no arguments for its commands). It works on
whatever catalogue Koha holds: the 2,000-book slice, or the full 883,468
after the import. Never run the demo during the import.

## Before the meeting (10 minutes)

1. In the project folder: `tools/demo.sh check`. It must end with
   `Ready.`
   - OPAC or plugin FAIL after a PC restart: bring Koha back and
     reinstall the plugin (the restart steps), then check again.
   - `vectors in the API: not loaded yet`: wait 30 seconds after the
     platform starts.
2. Choose today's new patron's username: `demo2` (`demo1` was used on
   26 Nov). If you used `demo2` before, take the next free number.
3. Choose five books of one clear taste, unlike the popular list, and
   keep their links open. For example, classic dystopias:
   ```bash
   tools/demo.sh find 1984
   tools/demo.sh find brave new world
   tools/demo.sh find animal farm
   tools/demo.sh find lord of the flies
   tools/demo.sh find fahrenheit
   ```
   Each prints the record's OPAC link. Take the most rated edition.
4. Open three windows:
   - the OPAC logged out (http://localhost:8080);
   - the staff interface logged in as `koha` (http://localhost:8081);
   - a terminal in the project folder.

## 1. The library, and what the platform reads (2 min)

**Do:** `tools/demo.sh check`.

**Say:** Koha is the library system. The platform reads its database
and never writes to it. It reads:

- the catalogue (`biblio`, `biblioitems`, `items`);
- the ratings patrons give in the OPAC (`ratings`);
- whether a patron also wrote a review (`reviews`).

The read job runs every 10 minutes, the embedding job hourly, and the
retraining daily. Each cadence is one line in `.env`.

**Expect:** the numbers of records, patrons and ratings, and each job
`ok` with its next run.

## 2. Search by meaning, and similar books (3 min)

**Do:** in the OPAC, open *Search by meaning*. Search `a boy wizard at
school`, then a description in your own words, such as `a story about
surviving in a world after war`. Open a result's record page.

**Say:** The search compares meanings, not words: a multilingual model
turned every book's title and description into a vector. On the record
page, *Readers who rated this also rated* comes from the ALS model
trained on the ratings. Each book appears once, whatever its number of
editions.

## 3. A new reader (6 min)

**Do:** in the staff interface, add a patron: Patrons, New patron.
- Name: Demo Two.
- Category: School.
- OPAC username `demo2`, password `Bookrs-Demo-2026`.
- Fill in the required contact fields with anything.

Then log in to the OPAC as `demo2`, and in the terminal:
```bash
tools/demo.sh list demo2
```

**Expect:** *Popular in this library*, and the note *You have not
rated any books yet, so these are popular in this library.* The
terminal shows the same list.

**Say:** a new reader has no history, so they get the most rated books
(stage 0 of the cold start).

**Do:** rate the first three of your five books in the OPAC (the stars
on each record page), then:
```bash
tools/demo.sh patron demo2      # the three ratings, in Koha's own table
tools/demo.sh read              # the platform reads them: "3 ratings given or changed"
tools/demo.sh list demo2
```
Reload the OPAC homepage.

**Expect:** *Recommended for you*, and the note *Based on your 3
ratings so far.* The terminal marks with `+` the books that were not there before, and
lists the ones that left.

**Say:** nothing was retrained. Each time the patron opens the page,
their place among the books is computed from their ratings. With fewer
than five ratings, they are placed using the books they rated only.

**Do:** rate the last two, then:
```bash
tools/demo.sh read
tools/demo.sh list demo2
```

**Expect:** the note *Based on your 5 ratings.*, and the list moves
further toward the taste you chose.

**Say:** from five ratings, the reader is placed against the whole
catalogue (full folding-in).

## 4. Retraining (3 min)

**Do:**
```bash
tools/demo.sh refit
tools/demo.sh list demo2
```

How long the refit takes:
- about 2 minutes on the 2,000-book catalogue;
- longer on the full one. Start it, then talk while it runs.

The command also waits 30 seconds after the refit, until the API has
the new model.

**Say:** retraining relearns every book's factors from all ratings,
`demo2`'s included. Its cadence is `BOOKRS_REFIT_EVERY` in `.env`, for
example `6h`, `1d` or `2w`. `tools/demo.sh check` shows when each job
runs next.

**Expect:** some books in and some out. Most of the change comes from
relearning every book, not from five ratings among millions. Retraining
is not seeded yet, so lists shift a little between refits.

## 5. Search ordered by taste (1 min)

**Do:** as `demo2`, search *Search by meaning* for `a boy wizard at
school`, or in the terminal:
```bash
tools/demo.sh search demo2 a boy wizard at school
```

**Expect:** the note *Ordered by meaning and by your 5 ratings*.

**Say:** the words choose the books, and the patron's ratings reorder
them: 0.7 meaning, 0.3 taste (the thesis's weights). Taste never adds a
book the words did not find.

## If something goes wrong

| What you see | What to do |
|---|---|
| *not available right now*, or `demo2` with ratings sees *Popular in this library* | The OPAC could not reach the platform: `docker compose restart api`, wait 30 s, `tools/demo.sh check` |
| `read` finds 0 ratings | The scheduled read took them first. Carry on: `tools/demo.sh list demo2` shows them used |
| The homepage did not change | Reload with Ctrl+Shift+R. The terminal list is what the page shows |
| `Koha refused the login demo2` | The password differs: `tools/demo.sh list demo2 <password>`, or set `DEMO_PASSWORD` |

## Afterwards

Nothing needs undoing. `demo2` and their ratings stay in Koha, like any
patron's; delete the patron in the staff interface if you prefer. The
lists `tools/demo.sh list` saved are in `data/demo/`.
