# Time-aware knowledge system: notes

## How to run

From the top folder of the participant kit, pointing `--submission` at this folder:

```bash
python harness/run_eval.py --submission <path-to-this-repo>/submission --eval eval/public_eval.jsonl
```

The kit (harness, practice questions and website snapshots) is not part of this repository.

- **API keys:** none. No paid APIs, no network calls.
- **Packages:** none. It uses only the Python standard library (3.9+, and the syntax works on Python 3.11). `requirements.txt` is present but lists nothing.
- **Ingest:** about 15 s for all five snapshots. Ingest is not timed for scoring.
- **Answer time:** about 0.02 s per question (p95).

## Practice-set result

| | Score |
|---|---|
| Starter | 0.093 |
| This solution, accuracy | 0.92 |
| Paraphrase consistency | 1.0 |
| Repeat consistency | 1.0 |
| Speed | 1.0 |

The 0.92 accuracy is the same as the final score, because the kit has no answer key for conflicts.

## How it works

### 1. Ingest keeps every page version

Each page is parsed once per unique content into:

- text passages for keyword search;
- structured rate-table rows: the product label, the tier or term, the rate, APR and APY cells, and the "effective as of" date.

For any date, the system knows which version of each page was live. A page that was later removed from the site is still searchable, with a lower weight.

### 2. Picking the date

`as_of`, or a date written in the question such as "as of May 2026", selects the latest snapshot taken on or before that date. With no date, the newest snapshot is used.

### 3. Rate questions are answered from the official rate sheet of that snapshot

- The source is `/rates`, or `/business-rates` for business questions, with product pages as a fallback. Stale copies of rate tables on marketing pages, blogs or the homebuying-guidance page are never quoted.
- Rows are filtered by the qualifiers in the question:
  - term ("6-month", "five year", "30-yr");
  - vehicle model year (2025 falls in the 2024-27 tier);
  - balance or loan amount;
  - jumbo or special;
  - Roth or Traditional;
  - purchase or refinance;
  - highest or lowest.

  So only the right tier is quoted, never every number on the page.
- Mortgage answers lead with the $100k-or-less tier of the 30-year loan.
- Words like "changed", "before" or "used to" add a short note on what changed between snapshots.

### 4. Honest "I don't know"

The system declines, without making up any number, when:

- the product has no published rate (ARMs, construction, jumbo mortgages, business loans and similar);
- the requested term or sub-product isn't on the sheet for that date;
- a named branch has no hours on file;
- search finds no good match.

### 5. Handlers for common topics

Routing number, phone numbers, branch count, branch addresses (from the locator), lobby and drive-up hours (all schedules on file), membership eligibility, contact-info updates, and canonical links (join, locator, certificate rates).

### 6. Search fallback

BM25 over the passages that were live on that date. Weighting:

- official pages count more than news, blogs and calculators;
- newer articles count more than older ones;
- lead paragraphs count more than later ones;
- short fragments and form widgets count less;
- FAQ questions are merged with their answers, and PDFs are indexed line by line (for the fee schedule).

## Rules for deciding what is current

1. The newest snapshot beats older snapshots.
2. The official rate sheet (`/rates`) beats any other page that shows a rate table for the same tier.
3. For facts that only appear in press releases (branch count, Forbes count), the most recently dated release wins. For example, the April–May 2026 releases say 22 branches, after the Hy-Vee branch closed on March 31, 2026. Older 2025 releases say 23.
4. Qualifiers such as term, model year, balance tier, rate vs APR, and product name are different facts, not conflicts.

## `conflicts()`

Returns about 19 conflicts. The count is kept small so precision stays high. Each conflict lists the competing values, their sources, the value we believe and why. They come from three checks:

- **Rate-sheet changes between snapshots.** Grouped per product and term, for example the 30-year purchase mortgage, the 10- and 17-month specials, the 12-month certificate, and 1st Rate Checking.
- **Stale tables on other pages that disagree with the official sheet at the same tier.** Examples are the homebuying-guidance mortgage table, the vehicle-affordability calculator (5.34% vs the 5.39% on the rate page), and the auto table on the checking and switch-and-earn pages.
- **Text facts that disagree.** These are the branch count (19/20/22/23/24) and the Forbes recognition count (fourth vs fifth). It also flags a business-rates row whose dividend rate is higher than its APY, which can't be right.

## Known limitations

- Free-text answers are extracted passages, not generated prose.
- A question that is mostly generic words with one key term can retrieve the wrong page. For example, a student asking about "loan options with flexibility" gets the personal-loans page.
- "Last year" is not resolved to a calendar year; the newest press release wins, which may not be the year the question means.
