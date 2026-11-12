# Knowledge System Hackathon: a time-aware answering system

My submission for the interface.ai Software Engineer II hackathon assignment.

The system reads several crawls ("snapshots") of a credit union's website, taken months apart, and answers member questions. Each answer gives:

- the **current** value, or the value on a requested date;
- the **right tier**, for example the term, model year or balance;
- a **source** page;
- an honest **"I don't know"** when the site doesn't say.

It also lists the places where the site contradicts itself, which value it believes, and why.

- **Solution:** [`submission/solution.py`](submission/solution.py). Its `build()` returns an object with `ingest`, `answer` and `conflicts`.
- **Design notes:** [`submission/NOTES.md`](submission/NOTES.md)

> **Not included:** this repository contains only my code and documentation. The participant kit (scorer, practice questions, assignment brief) and the supplied website snapshots are excluded, as required. No API keys are used or stored.

## Results on the practice set

| Metric | Starter | This solution |
|---|---|---|
| Accuracy | 0.093 | **0.92** |
| Paraphrase and repeat consistency | 1.0 | **1.0** |
| p95 answer latency | 0.16 s | **0.02 s** |
| Ingest (all five snapshots) | ~0 s | ~14 s (limit is 30 min) |

## Architecture

It is plain Python using only the standard library. It needs no API keys, no network calls and no third-party packages.

```
ingest(snapshot)        every page version kept, tagged with its snapshot date
   |-- text passages         -> BM25 inverted index (which version was live at each date)
   '-- rate-table rows       -> label, tier/term, rate / APR / APY, "effective as of" date

answer(question, as_of)
   1. pick the snapshot live on as_of (or a date in the question); default = newest
   2. handlers: routing number, phone, branch count, addresses, lobby/drive-up hours,
      membership eligibility, contact updates, canonical links
   3. rate questions -> the official rate sheet of that snapshot, rows filtered by term,
      model year, balance or loan amount, jumbo/special, Roth/Traditional, purchase/refinance
   4. decline (with no numbers) when the product, term or branch isn't published
   5. otherwise BM25 search, weighting official pages above news and blogs above
      calculators, newer above older, and lead paragraphs above later ones

conflicts()
   rate-sheet changes across snapshots + stale tables on other pages (calculators,
   landing pages, guides) + disagreeing text facts (branch count, award count)
```

### Rules for deciding what is current

1. The newest snapshot beats older snapshots.
2. The official rate sheet beats any other page showing the same tier.
3. For facts that only appear in news releases, the most recently dated release wins.
4. Qualifiers are different facts, not conflicts. Examples are term, model year, balance tier, rate vs APR, and personal vs business.

## Setup

You need Python 3.9 or newer; Python 3.11 has been checked for compatibility.

```bash
git clone https://github.com/harshini-5040/knowledge-system-hackathon-submission.git
pip install -r knowledge-system-hackathon-submission/submission/requirements.txt   # nothing to install
```

## Running the evaluation

The solution follows the kit's `harness/contract.py` interface and is run by the organizers' scorer. From the top folder of the participant kit, which holds `harness/`, `eval/` and `snapshots_zip/`:

```bash
python harness/run_eval.py \
  --submission <path-to>/knowledge-system-hackathon-submission/submission \
  --eval eval/public_eval.jsonl
```

On first run, the scorer unpacks the kit's snapshot zips. Then it calls `ingest()` for each snapshot, asks the questions, and prints the accuracy, consistency and speed pillars. Add `--conflicts-gold <file>` to also score `conflicts()`.

## Repository layout

```
submission/
  solution.py        the knowledge system (build() -> ingest / answer / conflicts)
  NOTES.md           approach, rules for "current", conflict detection, limitations
  requirements.txt   empty: standard library only
README.md
.gitignore
```

## Known limitations

- Free-text answers are extracted passages from the site, not generated prose.
- A question made mostly of generic words with only one key term can retrieve a related but wrong page.
- Relative dates such as "last year" are not resolved to a calendar year.
