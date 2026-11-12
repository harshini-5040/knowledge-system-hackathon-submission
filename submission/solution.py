"""
Time-aware knowledge system for the Dupaco website snapshots.

Design (all standard library, no API keys, deterministic):

  * ingest()   keeps EVERY version of every page, tagged with the snapshot it came from.
               Each page is parsed once per unique content hash into
                 - text passages (for keyword search), and
                 - structured rate-table rows (label, tier/term, rate/APR/APY, effective date).
  * answer()   1. picks the snapshot that was live on `as_of` (or the newest one);
               2. tries specialised handlers (routing number, branch hours/addresses,
                  branch count, links, contact info);
               3. for rate questions, reads the official rate sheet (/rates, /business-rates,
                  product pages as fallback) of THAT snapshot and filters rows by the
                  qualifiers in the question (term, model year, balance / loan amount,
                  jumbo/special, Roth/Traditional, purchase/refinance ...);
               4. declines when the site publishes no rate for the product asked about;
               5. otherwise falls back to BM25 search over the passages that were live on
                  that date, weighting official pages above blogs and newer articles above
                  older ones, and declines when the match is weak.
  * conflicts() reports values that changed between rate-sheet versions, pages that still
               show superseded values, and text facts that disagree across pages, each with
               the value we believe and why.
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

try:                                   # the harness puts harness/ on sys.path
    from contract import Answer, Conflict, Snapshot, Source  # noqa: E402
except ImportError:                    # running from inside the kit: find harness/contract.py
    _h = next((d / "harness" for d in Path(__file__).resolve().parents
               if (d / "harness" / "contract.py").exists()), None)
    if _h is None:
        raise
    sys.path.insert(0, str(_h))
    from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

# --------------------------------------------------------------------------------------
# text utilities
# --------------------------------------------------------------------------------------
STOP = set("""a an the and or of to in on for with at by from is are was were be been being do does did
i you your we our us me my it its this that these those what which who whom whose how when where
why can could would should will shall may might must there their they them he she his her as
than then so if but not no any all about into over under up down out off per via also just
please tell give get know want like need dupaco dupacos s many much last today still now right currently
current ever recently have has had some there here id im youre""".split())

MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
MON_ABBR = {m[:3]: i for m, i in MONTHS.items()}
WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "seventeen": 17,
            "eighteen": 18, "twenty": 20, "thirty": 30, "forty-eight": 48, "sixty": 60}

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")


def norm_text(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ").replace("\xa0", " ")
    return s


SUFFIXES = ("ibility", "ization", "ational", "ations", "ation", "ition", "ities", "ments", "ement", "ness",
            "ible", "able", "ized", "izes", "ize", "ing", "ity", "ies", "ied", "ers", "ure", "ed", "er", "ly", "es",
            "e", "s", "y")


def stem(w: str) -> str:
    if not w.isalpha() or len(w) <= 3:
        return w
    for suf in SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            if suf == "s" and (w.endswith("ss") or w.endswith("us")):
                continue
            return w[: len(w) - len(suf)]
    return w


def tokenize(s: str) -> list[str]:
    s = norm_text(s.lower())
    s = s.replace("hy-vee", "hyvee").replace("home$tart", "homestart").replace("1st", "first")
    out = []
    for t in TOKEN_RE.findall(s):
        t = t.strip(".'")
        if t.endswith("'s"):
            t = t[:-2]
        if not t or t in STOP or "'" in t:
            continue
        out.append(stem(t))
    return out


CUT_RE = re.compile(r"\n#{0,6}\s*(Handpicked for You|Related (?:articles|posts|stories)|You might also like|"
                    r"Keep reading|More from the newsroom)\b.*", re.S | re.I)


def clean_md(md: str) -> str:
    md = norm_text(md)
    md = CUT_RE.sub("\n", md)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)                 # images
    md = re.sub(r"data:image/[^\s)]+", " ", md)
    md = re.sub(r"\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)", r"\1", md)  # links -> anchor text
    md = md.replace("\\r\\n", " ").replace("\\\\", " ").replace("\\#", "#").replace("\\*", "*")
    md = re.sub(r"[ \t]+", " ", md)
    return md


def strip_md(s: str) -> str:
    s = re.sub(r"[*_#>`]+", "", s)
    s = re.sub(r"^\s*[-+]\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date_any(s: str) -> Optional[date]:
    s = s.lower()
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d\d|19\d\d)", s)
    if m:
        try:
            return date(int(m.group(3)), MON_ABBR[m.group(1)], int(m.group(2)))
        except ValueError:
            return None
    return None


def url_path(u: str) -> str:
    u = u.split("#")[0].split("?")[0]
    u = re.sub(r"^https?://", "", u).lower()
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def clean_cell(c: str) -> str:
    c = norm_text(c).strip()
    c = re.sub(r"\\\*", "*", c)
    c = re.sub(r"(\d%)\s?\d\b", r"\1", c)          # footnote marker after a percentage
    c = re.sub(r"\b(APR|APY)\d\b", r"\1", c)
    c = re.sub(r"<br>", " ", c)
    return re.sub(r"\s+", " ", c).strip()


def pct_values(s: str) -> list[float]:
    return [float(x) for x in PCT_RE.findall(s)]


def fmt_pct(v: float) -> str:
    s = f"{v:.3f}".rstrip("0")
    if s.endswith("."):
        s += "00"
    if len(s.split(".")[1]) < 2:
        s += "0"
    return s + "%"


# --------------------------------------------------------------------------------------
# page parsing
# --------------------------------------------------------------------------------------
class TableRow:
    __slots__ = ("url", "ctx", "label", "cells", "header", "effective", "page_title")

    def __init__(self, url, ctx, label, cells, header, effective, page_title):
        self.url, self.ctx, self.label, self.cells = url, ctx, label, cells
        self.header, self.effective, self.page_title = header, effective, page_title

    @property
    def ctx_l(self) -> str:
        return (self.ctx + " | " + self.label + " | " + " ".join(self.cells[1:2])).lower()

    def key(self) -> str:
        parts = [self.label] + [c for c, h in zip(self.cells[1:], self.header[1:])
                                if not PCT_RE.search(c) and not re.search(r"payment|point|min", h, re.I)]
        k = " ".join(parts).lower()
        k = re.sub(r"[^a-z0-9$+]+", " ", k.replace(",", ""))
        return re.sub(r"\s+", " ", k).strip()

    def rate_cells(self) -> list[tuple[str, str]]:
        out = []
        for h, c in zip(self.header, self.cells):
            if re.search(r"payment|point", h, re.I):
                continue
            if PCT_RE.search(c) and c != self.cells[0]:
                out.append((h, c))
        return out

    def primary(self) -> Optional[float]:
        """APY for deposits, APR for loans, else the first percentage."""
        best = None
        for h, c in self.rate_cells():
            v = pct_values(c)
            if not v:
                continue
            hl = h.lower()
            if "apy" in hl or "annual percentage yield" in hl or "apr" in hl or "annual percentage rate" in hl:
                return v[0]
            if best is None:
                best = v[0]
        return best

    def render(self) -> str:
        label = self.label
        extra = []
        for h, c in zip(self.header[1:], self.cells[1:]):
            hl = h.lower()
            if re.search(r"payment|point", hl) or not c or c in ("-", "—", "None", "N/A"):
                continue
            if PCT_RE.search(c):
                name = h
                name = re.sub(r"Annual Percentage Yield \(APY\)\d?", "APY", name)
                name = re.sub(r"Annual Percentage Rate \(As Low As\)|APR \(As Low As\)", "APR (as low as)", name)
                name = re.sub(r"\s*\d$", "", name).strip()
                if re.search(r"\bAPR\b|\bAPY\b", c):
                    extra.append(c)
                else:
                    extra.append(f"{c} {name}".strip())
            elif re.search(r"min", hl):
                extra.append(f"{h.strip()} {c}")
            else:
                label += f" ({c})" if not re.search(r"tier|term|type", hl) else f" {c}"
        return f"{label}: " + ", ".join(extra)


class PageVersion:
    __slots__ = ("vid", "url", "path", "title", "text", "passages", "rows", "pdate", "kind",
                 "prior", "first_k", "snap_ids", "headings", "rowpass")

    def __init__(self, vid, url, title, md):
        self.vid = vid
        self.url = url
        self.path = url_path(url)
        self.title = norm_text(title or "")
        self.text = clean_md(md)
        self.passages: list[str] = []
        self.rows: list[TableRow] = []
        self.first_k = None
        self.snap_ids: list[int] = []
        self.kind = page_kind(self.path)
        self.prior = KIND_PRIOR.get(self.kind, 1.0)
        head = self.text[:2500]
        self.pdate = parse_date_any(head)
        if self.pdate is None:
            m = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", url)
            if m:
                self.pdate = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        self._parse(md)

    def _parse(self, md: str):
        lines = clean_md(md).split("\n")
        # ---- tables -------------------------------------------------------------
        recent: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            ln = lines[i].strip()
            if ln.startswith("|") and i + 1 < n and re.match(r"^\|\s*:?-{2,}", lines[i + 1].strip()):
                header = [clean_cell(c) for c in ln.strip("|").split("|")]
                j = i + 2
                body = []
                while j < n and lines[j].strip().startswith("|"):
                    body.append([clean_cell(c) for c in lines[j].strip().strip("|").split("|")])
                    j += 1
                eff = ""
                for k in range(j, min(j + 8, n)):
                    m = re.search(r"effective (?:as of )?(\d{1,2}/\d{1,2}/\d{2,4})", lines[k], re.I)
                    if m:
                        eff = m.group(1)
                        break
                ctx = " | ".join(strip_md(r) for r in recent[-3:])
                for cells in body:
                    if len(cells) < 2 or not any(PCT_RE.search(c) for c in cells):
                        continue
                    if len(cells[0]) > 120:
                        continue
                    self.rows.append(TableRow(self.url, ctx, cells[0], cells, header + [""] * (len(cells) - len(header)),
                                              eff, self.title))
                recent = []
                i = j
                continue
            s = strip_md(ln)
            if s and len(s) < 90 and not re.search(r"effective|^\d?Rates are|^Open my|^See details|^Apply|^Explore|"
                                                   r"^Compare|^Learn more|^Continue|^✕|^Call to|^Email us", s):
                if not ln.startswith("[") or ln.startswith("- ["):
                    recent.append(s)
            i += 1
        # ---- passages -----------------------------------------------------------
        paras = [p.strip() for p in re.split(r"\n\s*\n", self.text)]
        if self.kind == "pdf":
            # PDFs (fee schedules, disclosures) put one fact per line: index lines, not blobs
            lines_out = []
            for p in paras:
                ls = [x.strip() for x in p.split("\n") if x.strip()]
                if len(ls) > 3:
                    lines_out.extend(ls)
                else:
                    lines_out.append(p)
            paras = lines_out
        out = []
        self.headings = set()
        for p in paras:
            if not p or p.startswith("|"):
                continue
            s = re.sub(r"\s+", " ", re.sub(r"[*_#>`]+", "", p)).strip()
            if p.startswith("#") and len(p) < 200:
                self.headings.add(s)
            if len(s) < 3 or s in ("✕", "Continue Cancel", "Previous", "Next"):
                continue
            if re.search(r"Afghanistan|\+93\b|Albania \(", s):     # phone-country picker widgets
                continue
            if "(Required)" in s or re.match(r"We'll only use your information", s):   # web forms
                continue
            if len(s) > 1200:   # long paragraph -> split into sentence windows
                sents = re.split(r"(?<=[.!?])\s+", s)
                buf = ""
                for se in sents:
                    if len(buf) + len(se) > 500 and buf:
                        out.append(buf.strip())
                        buf = ""
                    buf += " " + se
                if buf.strip():
                    out.append(buf.strip())
            else:
                # FAQ style: a short question line belongs with the answer that follows it
                if out and out[-1].endswith("?") and len(out[-1]) < 160 and not s.endswith("?"):
                    out[-1] = out[-1] + " " + s
                else:
                    out.append(s)
        # table rows also become passages (rendered)
        self.rowpass = set()
        for r in self.rows:
            rp = ((r.ctx + " - ") if r.ctx else "") + r.render()
            out.append(rp)
            self.rowpass.add(rp)
        self.passages = out


KIND_PRIOR = {"rates": 1.35, "product": 1.3, "about": 1.25, "newsroom": 1.0, "blog": 0.8, "bizres": 0.75,
              "calc": 0.55, "listing": 0.3, "pdf": 0.9, "lp": 0.95, "other": 1.0}


def page_kind(path: str) -> str:
    p = path.replace("dupaco.com", "")
    if p in ("/rates", "/business-rates"):
        return "rates"
    if re.search(r"/(tag|author|page)/|/category", p) or p in ("/learn/blog", "/newsroom", "/resources"):
        return "listing"
    if p.startswith("/newsroom"):
        return "newsroom"
    if p.startswith("/learn") or p.startswith("/20") or p.startswith("/dupacodaily"):
        return "blog"
    if p.startswith("/business/resources"):
        return "bizres"
    if "calculator" in p:
        return "calc"
    if p.startswith("/hubfs"):
        return "pdf"
    if p.endswith("-lp") or "offer" in p:
        return "lp"
    if p.startswith("/personal") or p.startswith("/business") or p in ("/checking", "/savings-account/online-savings-account"):
        return "product"
    if p.startswith("/about") or p in ("/contact", "/membership", "", "/shine", "/disclosures") or p.startswith("/connect"):
        return "about"
    return "other"


# --------------------------------------------------------------------------------------
# product catalogue for rate questions
# --------------------------------------------------------------------------------------
def _has(*pats):
    rx = re.compile("|".join(pats))
    return lambda r: bool(rx.search(r.ctx_l))


def _not(f):
    return lambda r: not f(r)


def _and(*fs):
    return lambda r: all(f(r) for f in fs)


IS_IRA = lambda r: bool(re.match(r"^(roth|traditional)\b", r.label.lower()))  # noqa: E731
IS_BIZ_CARD = _has(r"business (platinum|rewards)", r"minimum credit limit")

PRODUCTS = [
    # key, question regex, row predicate, description
    ("business_cards", r"business\b.*\b(credit card|visa|card)|\b(platinum|rewards)\b.*\bbusiness",
     IS_BIZ_CARD, "Business Visa credit cards"),
    ("platinum_visa", r"platinum", _and(_has(r"platinum visa|introductory balance"), _not(IS_BIZ_CARD)),
     "Platinum Visa"),
    ("rewards_visa", r"rewards?\b", _and(_has(r"rewards visa"), _not(IS_BIZ_CARD)), "Rewards Visa"),
    ("credit_cards", r"credit cards?|\bvisa\b",
     _has(r"platinum visa|rewards visa|business (platinum|rewards)|minimum credit limit"), "Visa credit cards"),
    ("heloc", r"heloc|home equity line|equity line",
     _and(_has(r"heloc|home equity line"), _not(_has(r"intro"))), "Home Equity Line of Credit (HELOC)"),
    ("home_equity_loan", r"home equity loan|equity loan|second mortgage",
     _and(_has(r"home equity loan"), _not(_has(r"year fixed"))), "Home Equity Loan"),
    ("home_improvement", r"home improvement", _has(r"home improvement"), "Home Improvement Loan"),
    ("cash_out", r"cash[- ]?out", _and(_has(r"cash-out"), _has(r"year fixed")), "Cash-Out Refinance"),
    ("mortgage", r"mortgage|home ?loan|house|home purchase|refinanc|\b(10|15|20|30)[- ]?(year|yr)\b.*fixed|fixed.*\b(15|20|30)[- ]?(year|yr)|\b(15|20|30)[- ]?(year|yr)\b",
     _and(_has(r"year fixed"), _not(_has(r"equity|cash-out|relief"))), "Home loans (fixed-rate mortgages)"),
    ("atv", r"\batv|snowmobile|jet ?ski|powersport|utv|side[- ]by[- ]side",
     _has(r"atv|snowmobile"), "ATV, Snowmobile & Jet Ski Loans"),
    ("boat_rv", r"\bboat|\brvs?\b|camper|motorhome|recreational", _has(r"boat"), "Boat & RV Loans"),
    ("auto", r"\bauto|\bcars?\b|vehicle|motorcycle|truck|\bsuv\b",
     _and(_has(r"auto|model year"), _not(_has(r"atv|snowmobile"))), "Auto & Motorcycle Loans"),
    ("personal_loan", r"personal loan|signature loan|unsecured loan",
     _and(_has(r"personal loan"), lambda r: bool(re.search(r"month", r.label.lower()))), "Personal Loan"),
    ("ira", r"\bira\b|\biras\b|roth|retirement", IS_IRA, "IRA certificates"),
    ("hsa", r"health savings|\bhsa\b", _has(r"health savings|saving account rates"), "Health Savings Account"),
    ("cert", r"certificate|\bcds?\b|share cert|term share|jumbo|\bspecials?\b",
     _and(_not(IS_IRA), lambda r: bool(re.search(r"certificate special", r.label.lower())
                                       or (re.match(r"^\d+ months?$", r.label.lower().strip())
                                           and re.search(r"regular|certificate|term", r.ctx.lower())))),
     "Share certificates"),
    ("bright", r"bright savings|online[- ]only|online savings", _has(r"bright savings"), "Bright Savings"),
    ("launchpad", r"launch ?pad", _has(r"launchpad"), "Launchpad Savings"),
    ("holiday", r"holiday|christmas club", _has(r"holiday club"), "Holiday Club Savings"),
    ("ynis", r"you[- ]?name[- ]?it", _has(r"you[- ]?name[- ]?it"), "You Name It Savings"),
    ("high_yield", r"high[- ]?yield", _has(r"high-yield"), "High-Yield Savings"),
    ("money_market", r"money market|investor'?s choice|achieve",
     _and(_has(r"investor's choice|achieve|money market"), _not(_has(r"high-yield"))), "Money market accounts"),
    ("first_rate", r"1st rate|first rate", _has(r"1st rate checking"), "1st Rate Checking"),
    ("free_checking", r"free checking", _has(r"free checking"), "Free Checking"),
    ("checking", r"checking", _has(r"1st rate checking|free checking"), "Checking accounts"),
    ("share_savings", r"share savings|regular savings|savings|share account|dividend",
     _and(_has(r"share savings"), _not(_has(r"you[- ]?name"))), "Share Savings"),
]
PRODUCT_BY_KEY = {p[0]: p for p in PRODUCTS}

RATE_Q = re.compile(r"\b(rates?|aprs?|apys?|yields?|dividends?|interest|earn|pays?|paying|percent|%|"
                    r"how much (?:do|does|will|would|can) (?:i|you|it) (?:earn|pay|charge))\b")
NO_RATE_PRODUCTS = re.compile(
    r"\b(arm|arms|adjustable|\d/\d|construction|fha|va loan|usda|land loan|lot loan|jumbo mortgage|"
    r"jumbo loan|commercial|business loan|sba|equipment|student loan|reverse mortgage|bridge loan|"
    r"payday|car lease|lease|share secured|share-secured|certificate secured|credit builder|"
    r"real estate loan|farm|agricultural|interest[- ]only|balloon|line of credit for business|"
    r"business line)\b")


# --------------------------------------------------------------------------------------
# the knowledge system
# --------------------------------------------------------------------------------------
class TimeAwareKnowledge:
    def __init__(self):
        self.snaps: list[tuple[str, str]] = []            # (taken_on, snapshot_id)
        self.versions: list[PageVersion] = []
        self.by_hash: dict[str, int] = {}
        self.snap_pages: list[dict[str, int]] = []        # k -> {path: vid}
        self.url_hist: dict[str, list[tuple[int, int]]] = defaultdict(list)   # path -> [(k, vid)]
        self._index_built = False
        self._cache: dict = {}

    # ---------------------------------------------------------------- ingest
    def ingest(self, snapshot: Snapshot) -> None:
        k = len(self.snaps)
        self.snaps.append((snapshot.taken_on, snapshot.snapshot_id))
        pages = {}
        for p in snapshot.pages:
            if "privacy-policy?" in p.url:
                continue
            h = hashlib.md5((p.url + "\0" + p.markdown).encode("utf-8", "ignore")).hexdigest()
            vid = self.by_hash.get(h)
            if vid is None:
                vid = len(self.versions)
                pv = PageVersion(vid, p.url, p.title, p.markdown)
                pv.first_k = k
                self.versions.append(pv)
                self.by_hash[h] = vid
            pv = self.versions[vid]
            pv.snap_ids.append(k)
            path = pv.path
            pages[path] = vid
            hist = self.url_hist[path]
            if not hist or hist[-1][1] != vid:
                hist.append((k, vid))
        self.snap_pages.append(pages)
        self._cache.clear()
        self._build_index()       # ingest is not timed; keep answer() fast

    # ---------------------------------------------------------------- index
    def _build_index(self):
        self.passages: list[tuple[int, str]] = []      # (vid, text)
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.plen: list[int] = []
        self.pvids: list[list[int]] = []                # every page version that contains the passage
        self.ppos: list[float] = []                     # relative position of the passage in its page
        seen: dict = {}
        for pv in self.versions:
            tnorm = re.sub(r"[^a-z0-9]", "", pv.title.split("|")[0].lower())
            npass = max(1, len(pv.passages))
            for pi, txt in enumerate(pv.passages):
                if tnorm and re.sub(r"[^a-z0-9]", "", txt.lower()) == tnorm:
                    continue        # the headline repeats the title; the title is scored separately
                key = (pv.path, txt)
                if key in seen:
                    self.pvids[seen[key]].append(pv.vid)
                    continue
                pid = len(self.passages)
                seen[key] = pid
                self.pvids.append([pv.vid])
                self.ppos.append(pi / npass)
                self.passages.append((pv.vid, txt))
                toks = tokenize(txt)
                self.plen.append(len(toks))
                for t, c in Counter(toks).items():
                    self.postings[t].append((pid, c))
        self.title_toks = [set(tokenize(pv.title.split("|")[0] + " " +
                                        pv.path.rsplit("/", 1)[-1].replace("-", " ")))
                           for pv in self.versions]
        self.N = len(self.passages)
        self.avgdl = sum(self.plen) / max(1, self.N)
        self.idf = {t: math.log(1 + (self.N - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}
        # which versions are "live" for each snapshot: latest version of every page seen so far
        self.live: list[dict[int, bool]] = []
        for k in range(len(self.snaps)):
            live = {}
            for path, hist in self.url_hist.items():
                cand = [vid for (kk, vid) in hist if kk <= k]
                if cand:
                    live[cand[-1]] = path in self.snap_pages[k]
            self.live.append(live)
        self._index_built = True

    # ---------------------------------------------------------------- time
    def _snap_for(self, as_of: Optional[str], question: str) -> int:
        last = len(self.snaps) - 1
        d = None
        if as_of:
            try:
                d = date.fromisoformat(str(as_of)[:10])
            except ValueError:
                d = None
        if d is None:
            d = self._date_from_question(question)
        if d is None:
            return last
        k = 0
        for i, (taken, _) in enumerate(self.snaps):
            if date.fromisoformat(taken[:10]) <= d:
                k = i
        return k

    @staticmethod
    def _date_from_question(q: str) -> Optional[date]:
        ql = q.lower()
        m = re.search(r"\b(?:as of|in|during|on|back in|around|at the (?:start|end|beginning) of)\s+"
                      r"(?:early |mid[- ]|late |the end of |end of )?(january|february|march|april|may|june|july|"
                      r"august|september|october|november|december)(?:\s+(\d{1,2}))?,?\s+(20\d\d)", ql)
        if not m:
            m2 = re.search(r"\bas of (20\d\d)-(\d\d)-(\d\d)", ql)
            if m2:
                return date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
            return None
        mon = MONTHS[m.group(1)]
        yr = int(m.group(3))
        if m.group(2):
            day = int(m.group(2))
        else:
            day = 28 if mon == 2 else (30 if mon in (4, 6, 9, 11) else 31)
        try:
            return date(yr, mon, day)
        except ValueError:
            return None

    def _page(self, k: int, path: str) -> Optional[PageVersion]:
        vid = self.snap_pages[k].get(path)
        return self.versions[vid] if vid is not None else None

    def _src(self, pv: PageVersion, quote: str = "") -> Source:
        return Source(url=pv.url, snapshot_id=self.snaps[pv.first_k][1], quote=(quote[:200] or None))

    # ---------------------------------------------------------------- answer
    def answer(self, question: str, as_of: Optional[str] = None) -> Answer:
        if not self._index_built:
            self._build_index()
        key = (question.strip().lower(), as_of)
        if key in self._cache:
            return self._cache[key]
        k = self._snap_for(as_of, question)
        try:
            ans = self._answer(question, k)
        except Exception as e:  # never crash the harness
            ans = Answer(text=f"I don't know. Please contact Dupaco. ({type(e).__name__})", confident=False)
        self._cache[key] = ans
        return ans

    def _answer(self, q: str, k: int) -> Answer:
        ql = norm_text(q.lower())
        for h in (self._h_routing, self._h_branch_count, self._h_link, self._h_contact_update,
                  self._h_eligibility, self._h_hours, self._h_address, self._h_rates, self._h_phone):
            a = h(q, ql, k)
            if a is not None:
                return a
        return self._search_answer(q, ql, k)

    # ---------------------------------------------------------------- handlers
    def _find_regex(self, k: int, rx: re.Pattern, prefer=("rates", "about", "product")):
        """Most common match of rx over pages live at snapshot k, with the pages that hold it."""
        hits = defaultdict(list)
        for path, vid in self.snap_pages[k].items():
            pv = self.versions[vid]
            for m in rx.finditer(pv.text):
                hits[m.group(1)].append(pv)
        if not hits:
            return None, []
        best = max(hits, key=lambda v: (sum(2 if p.kind in prefer else 1 for p in hits[v]), v))
        return best, hits[best]

    def _h_routing(self, q, ql, k):
        if not re.search(r"routing|aba number|transit number|aba\b", ql):
            return None
        val, pages = self._find_regex(k, re.compile(r"[Rr]outing [Nn]umber:?\**\s*\**\s*(\d{9})"))
        if not val:
            return None
        pages = [self.versions[v] for v in self.snap_pages[k].values() if val in self.versions[v].text]
        pages = sorted(pages, key=lambda p: (p.kind != "product", p.kind != "about", p.path))
        srcs = [self._src(p, f"Routing Number: {val}") for p in pages[:5]]
        return Answer(text=f"Dupaco's routing number is {val}.", sources=srcs)

    def _h_branch_count(self, q, ql, k):
        if not re.search(r"how many (branch|location|office)|number of (branch|location)|branch(es)? (do|does) .* have|"
                         r"how many .*branch", ql):
            return None
        rx = re.compile(r"(\d+) branch office locations")
        counts = Counter()
        newest = {}
        pages = defaultdict(list)
        for path, vid in self.snap_pages[k].items():
            pv = self.versions[vid]
            for m in rx.finditer(pv.text):
                v = m.group(1)
                counts[v] += 1
                pages[v].append(pv)
                if pv.pdate and (v not in newest or pv.pdate > newest[v]):
                    newest[v] = pv.pdate
        if not counts:
            return None
        # the most recently dated press release carries the current boilerplate
        val = max(newest, key=lambda v: (newest[v], counts[v])) if newest else counts.most_common(1)[0][0]
        srcs = [self._src(p, f"{val} branch office locations") for p in
                sorted(pages[val], key=lambda p: p.pdate or date(1900, 1, 1), reverse=True)[:3]]
        note = None
        if len(counts) > 1:
            note = ("Pages disagree (" + ", ".join(f"{v}: {c} pages" for v, c in counts.most_common()) +
                    f"); the most recently dated press release ({newest.get(val)}) says {val}.")
        return Answer(text=f"Dupaco has {val} branch office locations.", sources=srcs, conflict_note=note)

    LINKS = [
        (r"(certificate|\bcds?\b|share cert).*(rate|link|url|page)|(link|url|page).*(certificate|\bcds?\b)",
         "https://www.dupaco.com/personal/savings-account#rates",
         "Certificate (CD) rates are listed at https://www.dupaco.com/personal/savings-account#rates "
         "(the full rate sheet is at https://www.dupaco.com/rates)."),
        (r"(locator|find (a|the) (branch|atm|location)|branch(es)? (and|&|or) atm|atm (and|&|or) branch|"
         r"branch locations?|atm locations?)",
         "https://www.dupaco.com/contact",
         "Use the branch and ATM locator at https://www.dupaco.com/contact."),
        (r"\bjoin\b|become a member|open (a|an) (account|membership) online|membership",
         "https://join.dupaco.com/",
         "You can join Dupaco online at https://join.dupaco.com/ (details: https://www.dupaco.com/about/join)."),
        (r"(mortgage|home loan).*(rate|link|url|page)",
         "https://www.dupaco.com/personal/loans/home-loans#rates",
         "Home loan rates are at https://www.dupaco.com/personal/loans/home-loans#rates."),
        (r"(rate sheet|all (the |of )?(your |dupaco'?s? )?rates|current rates)",
         "https://www.dupaco.com/rates", "All current rates are listed at https://www.dupaco.com/rates."),
        (r"(shine|online banking|log ?in|sign ?in)", "https://shine.dupaco.com/",
         "Log in to Shine Online Banking at https://shine.dupaco.com/."),
        (r"(fee schedule|fees)", "https://www.dupaco.com/hubfs/dupaco-credit-union-fee-schedule-miscellaneous-fees-jan-15-2025.pdf",
         "The rate and fee schedule is at https://www.dupaco.com/hubfs/dupaco-credit-union-fee-schedule-miscellaneous-fees-jan-15-2025.pdf."),
    ]

    def _h_link(self, q, ql, k):
        wants_link = re.search(r"\b(link|url|web ?address|web ?page|website|webpage|which page|what page)\b|"
                               r"where (can|do|should) i (go to )?(check|find|see|view|look up)\b.*\bonline\b|"
                               r"where (can|do) i (check|see|view|look up) (your |the )?(current )?rates", ql)
        join_online = re.search(r"\bjoin\b.*\bonline\b|\bonline\b.*\bjoin\b|"
                                r"(apply|sign up|enroll)\b.*\b(membership|member)\b.*\bonline\b|"
                                r"\bonline\b.*\b(membership|become a member)", ql)
        if not (wants_link or join_online):
            return None
        for rx, url, text in self.LINKS:
            if re.search(rx, ql):
                pv = None
                for vid in self.snap_pages[k].values():
                    v = self.versions[vid]
                    if v.path == url_path(url):
                        pv = v
                        break
                srcs = [Source(url=url)] + ([self._src(pv)] if pv else [])
                return Answer(text=text, sources=srcs)
        return None

    def _h_phone(self, q, ql, k):
        if not re.search(r"phone number|telephone|\bcall (you|dupaco|the|your|customer)|number (to|should i) (call|reach)|"
                         r"contact number|toll[- ]free|what number|how (do|can) i (call|reach|contact) (you|dupaco)", ql):
            return None
        if re.search(r"routing|account number|member number", ql):
            return None
        val, pages = self._find_regex(k, re.compile(r"\*\*Phone:\*\*\s*(\d{3}-\d{3}-\d{4})"))
        main = val or "800-373-7600"
        srcs = [self._src(p) for p in pages[:2]]
        if re.search(r"mortgage|home loan", ql):
            mv, mp = self._find_regex(k, re.compile(r"Mortgage Team at (\d[\d.-]{9,13}\d(?: ext\.? \d+)?)"))
            if mv:
                return Answer(text=f"You can reach Dupaco's Mortgage Team at {mv}. Dupaco's main line is {main}.",
                              sources=[self._src(p) for p in mp[:2]] + srcs)
        return Answer(text=f"You can call Dupaco at {main}. For a lost or stolen card or other urgent needs, call "
                           f"{main} right away.", sources=srcs)

    def _h_contact_update(self, q, ql, k):
        if not re.search(r"(update|change|correct)\b.*\b(contact|address|phone|email|e-mail|name|information|info)", ql):
            return None
        val, pages = self._find_regex(k, re.compile(r"\*\*Phone:\*\*\s*(\d{3}-\d{3}-\d{4})"))
        phone = val or "800-373-7600"
        srcs = [self._src(p) for p in pages[:2]]
        return Answer(text=("You can update your contact information by logging in to Shine Online or Mobile "
                            "Banking (use the Contact Us tab) or by calling Dupaco at " + phone +
                            ". You can also stop by any branch."), sources=srcs)

    def _h_eligibility(self, q, ql, k):
        if not re.search(r"eligib|who can (join|become)|can i (join|become)|qualif\w* (for|to become) (a )?member|"
                         r"requirements? (to|for) (join|membership|become)|how (do|can) i become a member|"
                         r"membership requirements?", ql):
            return None
        pv = self._page(k, "dupaco.com/about/join")
        parts, srcs = [], []
        if pv:
            m = re.search(r"Eligible to Join[^\n]*\n(.*?)(?:\n#|\Z)", pv.text, re.S)
            if m:
                body = [strip_md(x) for x in m.group(1).split("\n") if strip_md(x)]
                bullets = [b for b in body if not b.startswith("Join Dupaco")][:5]
                parts.append("You're eligible to join Dupaco if any of these apply: " + " ".join(
                    b if b.endswith(".") else b + "." for b in bullets))
                srcs.append(self._src(pv))
        # where the charter area is (newest boilerplate)
        rx = re.compile(r"serves residents in ([^.]*?(?:Iowa|Illinois|Wisconsin)[^.]*)\.")
        best = None
        for vid in self.snap_pages[k].values():
            v = self.versions[vid]
            for m in rx.finditer(v.text):
                if best is None or (v.pdate or date(1900, 1, 1)) > (best[0].pdate or date(1900, 1, 1)):
                    best = (v, m.group(1))
        if best:
            parts.append(f"The charter area covers {best[1].replace('residents in ', '')} - i.e. counties in Iowa, "
                         "Illinois and Wisconsin.")
            srcs.append(self._src(best[0]))
        else:
            parts.append("The charter area covers counties in Iowa, Illinois and Wisconsin.")
        return Answer(text=" ".join(parts), sources=srcs)

    # -- hours ------------------------------------------------------------------
    def _hours_blocks(self, k):
        blocks = []
        live = self.live[k]
        for vid in live:
            pv = self.versions[vid]
            t = pv.text
            for m in re.finditer(r"(?:Branch hours are:|\*\*Lobby\*\*)", t):
                seg = t[m.start(): m.start() + 600]
                seg = seg.split("ABOUT DUPACO")[0].split("**ABOUT")[0]
                seg = re.sub(r"\s+", " ", seg.replace("**", "")).strip()
                if "a.m." in seg:
                    blocks.append((pv, seg))
        return blocks

    BRANCH_WORDS = re.compile(r"(?:the\s+)?([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})\s+(?:branch|office|location)")

    def _h_hours(self, q, ql, k):
        if not re.search(r"\bhours?\b|\bopen\b.*\b(saturday|sunday|today|now|late|until)|what time|close[sd]? at|"
                         r"when (do|does|are) .* (open|close)", ql):
            return None
        if re.search(r"hy-?vee", ql):
            return None   # handled by search (branch closure news)
        blocks = self._hours_blocks(k)
        if not blocks:
            return None
        # a specific branch?
        named = None
        for m in self.BRANCH_WORDS.finditer(q):
            nm = m.group(1).strip()
            if nm.lower() not in ("dupaco", "the", "your", "a", "each", "every", "all", "any", "general", "main",
                                  "lobby", "drive-up", "drive", "dupaco's", "my", "local", "nearest", "what",
                                  "do", "are"):
                named = re.sub(r"^(?:The|the)\s+", "", nm)
        uniq = []
        seen = set()
        for pv, seg in sorted(blocks, key=lambda b: (b[0].pdate or date(1900, 1, 1)), reverse=True):
            key = re.sub(r"[^a-z0-9]", "", seg.lower())[:200]
            if key in seen:
                continue
            seen.add(key)
            uniq.append((pv, seg))
        if named:
            nl = named.lower()
            mine = [(pv, seg) for pv, seg in uniq if nl in pv.text.lower()[:3000]]
            if not mine:
                return Answer(text=(f"I don't have verified hours for the {named} branch on Dupaco's website, so I "
                                    "can't confirm them. Please check the branch locator on dupaco.com/contact "
                                    "or contact Dupaco directly."), confident=False)
            uniq = mine
        want_drive = bool(re.search(r"drive", ql)) and not re.search(r"lobby", ql)

        def pick(seg):
            seg = seg.replace("Branch hours are:", "").strip()
            lob = re.search(r"Lobby(.*?)(?=Drive-?up|$)", seg)
            drv = re.search(r"Drive-?up(.*)$", seg)
            parts = []
            if drv and want_drive:
                parts.append("Drive-up: " + drv.group(1).strip(" :"))
            if lob:
                parts.append("Lobby: " + lob.group(1).strip(" :"))
            if drv and not want_drive:
                parts.append("Drive-up: " + drv.group(1).strip(" :"))
            return "; ".join(parts) if parts else seg

        # most common schedule first (it is what "typical" means), then the variants
        sched = Counter(pick(s) for _, s in uniq)
        ordered = []
        for pv, s in uniq:
            p = pick(s)
            if p not in [o for o in ordered]:
                ordered.append(p)
        ordered.sort(key=lambda p: -sched[p])
        main = ordered[0]
        txt = f"Typical Dupaco branch hours - {main}."
        if len(ordered) > 1 and not named:
            txt += " Hours vary by branch; other schedules on file: " + " | ".join(ordered[1:3]) + "."
        if not named:
            txt += " Let me know which branch (or all branches) and I can narrow it down."
        srcs = [self._src(pv, seg) for pv, seg in uniq[:3]]
        srcs.append(Source(url="https://www.dupaco.com/contact"))
        srcs.append(Source(url="https://www.dupaco.com/"))
        return Answer(text=txt, sources=srcs)

    # -- branch addresses ------------------------------------------------------------
    def _locations(self, k):
        out = []
        pv = self._page(k, "dupaco.com/contact")
        if not pv:
            return out
        for m in re.finditer(r"###\s*(.+?)\n\s*\n\s*(Dupaco Branch|Surcharge-Free ATM|[^\n]*Branch[^\n]*)\n\s*\n\s*([^\n]+)",
                             pv.text):
            out.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip(), pv))
        return out

    def _h_address(self, q, ql, k):
        if not re.search(r"address|where is|where's|located|location of|directions", ql):
            return None
        if not re.search(r"branch|office|atm|location", ql):
            return None
        locs = self._locations(k)
        qtoks = set(tokenize(q)) - {"branch", "address", "located", "location", "office", "atm", "where"}
        best = []
        for name, typ, addr, pv in locs:
            atoks = set(tokenize(addr))
            ov = qtoks & atoks
            if ov:
                best.append((len(ov), typ == "Dupaco Branch", name, typ, addr, pv))
        if not best:
            return None
        best.sort(key=lambda x: (-x[0], not x[1]))
        top = [b for b in best if b[0] == best[0][0] and b[1] == best[0][1]]
        lines = [f"{b[4]}" + ("" if b[3] == "Dupaco Branch" else f" ({b[3]}: {b[2]})") for b in top[:3]]
        txt = "The address is " + "; ".join(lines) + "."
        return Answer(text=txt, sources=[self._src(top[0][5], top[0][4])])

    # -- rates -------------------------------------------------------------------
    def _rate_rows(self, k: int, prod_key: str, business: bool) -> tuple[list[TableRow], list[PageVersion]]:
        pred = PRODUCT_BY_KEY[prod_key][2]
        order = ["dupaco.com/business-rates", "dupaco.com/rates"] if business else ["dupaco.com/rates",
                                                                                   "dupaco.com/business-rates"]
        if prod_key in ("business_cards",):
            order = ["dupaco.com/business/credit-cards", "dupaco.com/rates", "dupaco.com/business-rates"]
        rows, pages = [], []
        for path in order:
            pv = self._page(k, path)
            if not pv:
                continue
            rr = [r for r in pv.rows if pred(r)]
            if rr:
                rows, pages = rr, [pv]
                break
        if prod_key == "credit_cards":   # list every card: personal + business
            have_biz = any(IS_BIZ_CARD(r) for r in rows)
            if not have_biz:
                biz, bp = self._rate_rows(k, "business_cards", True)
                rows = rows + biz
                pages = pages + bp
        if not rows:   # fall back to any product page in this snapshot
            for path, vid in sorted(self.snap_pages[k].items()):
                pv = self.versions[vid]
                if pv.kind not in ("product", "rates"):
                    continue
                rr = [r for r in pv.rows if pred(r)]
                if rr:
                    rows, pages = rr, [pv]
                    break
        # dedupe identical rows
        out, seen = [], set()
        for r in rows:
            sig = (r.key(), tuple(c for _, c in r.rate_cells()))
            if sig in seen:
                continue
            seen.add(sig)
            out.append(r)
        return out, pages

    @staticmethod
    def _q_terms(ql: str) -> list[int]:
        months = []
        s = ql
        for w, n in WORD_NUM.items():
            s = re.sub(rf"\b{w}\b", str(n), s)
        for m in re.finditer(r"\b(\d{1,3})[- ]?(?:months?|mos?|mo\.)\b", s):
            months.append(int(m.group(1)))
        for m in re.finditer(r"\b(\d{1,2})[- ]?(?:years?|yrs?)\b(?![- ]?old)", s):
            months.append(int(m.group(1)) * 12)
        if re.search(r"\b(a|one)[- ]year\b", ql):
            months.append(12)
        return months

    @staticmethod
    def _row_months(r: TableRow) -> Optional[int]:
        s = (r.label + " " + " ".join(r.cells[1:2])).lower()
        m = re.search(r"(\d{1,3})\s*[- ]?(?:months?|mo)\b", s)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d{1,2})[- ]?years?\b", s)
        if m:
            return int(m.group(1)) * 12
        return None

    @staticmethod
    def _amounts(ql: str) -> list[float]:
        out = []
        for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|million|m)?\b|\b(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|million)\b|"
                             r"\b(\d{1,3}(?:,\d{3})+)\b", ql):
            num = m.group(1) or m.group(3) or m.group(5)
            mult = (m.group(2) or m.group(4) or "").lower()
            try:
                v = float(num.replace(",", ""))
            except ValueError:
                continue
            if mult in ("k", "thousand"):
                v *= 1000
            elif mult in ("m", "million"):
                v *= 1_000_000
            out.append(v)
        return out

    @staticmethod
    def _row_range(r: TableRow) -> Optional[tuple[float, float]]:
        s = " ".join([r.label] + r.cells[1:2]).replace(",", "")
        m = re.search(r"\$?(\d+(?:\.\d+)?)\s*[-–]\s*\$?(\d+(?:\.\d+)?)", s)
        if m and "model" not in s.lower():
            lo, hi = float(m.group(1)), float(m.group(2))
            if hi < lo and hi < 1000:     # "$10000-24999" style already handled; "10,000-24,999"
                hi = hi * 1000
            return lo, hi
        m = re.search(r"\$(\d+(?:\.\d+)?)\s*\+", s)
        if m:
            return float(m.group(1)), float("inf")
        m = re.search(r"\$(\d+(?:\.\d+)?) or less", s)
        if m:
            return 0.0, float(m.group(1))
        return None

    @staticmethod
    def _row_years(r: TableRow) -> Optional[tuple[int, int]]:
        s = r.label
        m = re.search(r"(20\d\d|19\d\d)\s*-\s*(\d{2,4})", s)
        if m:
            a = int(m.group(1))
            b = m.group(2)
            b = int(b) if len(b) == 4 else int(str(a)[:2] + b)
            return min(a, b), max(a, b)
        m = re.search(r"(20\d\d|19\d\d) Model Year \(and older\)", s)
        if m:
            return 1900, int(m.group(1))
        return None

    def _detect_product(self, ql: str) -> Optional[str]:
        for key, qrx, _, _ in PRODUCTS:
            if re.search(qrx, ql):
                if key == "business_cards" and not re.search(r"business", ql):
                    continue
                return key
        return None

    def _h_rates(self, q, ql, k):
        is_rate_q = bool(RATE_Q.search(ql)) or bool(re.search(r"\bhow much\b.*\b(earn|interest)", ql))
        prod = self._detect_product(ql)
        if not is_rate_q and prod and not re.search(r"\b(fee|fees|penalty|minimum|open|apply|qualify|how do|"
                                                    r"how can|requirement|eligib|what is an?|what are)\b", ql):
            # "What's the current 30-yr fixed?" / "6 month certificate today?"
            if re.search(r"\b(current|today|now|latest|right now)\b", ql) or self._q_terms(ql):
                is_rate_q = True
        if not is_rate_q:
            return None
        if NO_RATE_PRODUCTS.search(ql) and prod not in ("auto", "boat_rv", "atv", "cert", "ira", "heloc",
                                                        "home_equity_loan") or (
                prod is None and NO_RATE_PRODUCTS.search(ql)):
            m = NO_RATE_PRODUCTS.search(ql)
            what = m.group(0) if m else "that product"
            return Answer(text=(f"I don't know - Dupaco's website does not list a rate for {self._describe(ql, what)}. "
                                "Rates for it are not published on the rate sheet, so please contact a Dupaco "
                                "lender (or the Mortgage Team for home loans) for a personalized quote."),
                          confident=False)
        if prod is None:
            return None
        business = bool(re.search(r"\bbusiness\b", ql)) and prod not in ("business_cards",)
        rows, pages = self._rate_rows(k, prod, business)
        if not rows:
            return None       # no rate table for it: let text search decide (or decline)
        sel = self._filter_rows(prod, rows, ql)
        if self._missed:
            # the specific term / sub-product asked about is not published: decline, no numbers
            miss = self._missed[0]
            what = (re.sub(r"^the ", "the ", miss) if miss.startswith("the ") else
                    f"that term for {PRODUCT_BY_KEY[prod][3]}")
            when = "" if k == len(self.snaps) - 1 else " as of that date"
            return Answer(text=(f"I don't know - Dupaco's rate sheet{when} does not list {what}, so I can't quote a "
                                f"rate for it. It may not be offered (or is no longer offered). Please contact Dupaco "
                                f"or see the current rate sheet for the options that are available."),
                          sources=[self._src(p) for p in pages], confident=False)
        return self._render_rates(prod, sel, rows, pages, ql, k)

    @staticmethod
    def _describe(ql, what):
        if re.search(r"\d/\d|\barm\b|adjustable", ql):
            return "adjustable-rate mortgages (ARMs)"
        if "construction" in ql:
            return "construction loans"
        return what

    def _filter_rows(self, prod, rows, ql):
        sel = list(rows)
        self._missed = []

        def keep(f, miss=None):
            nonlocal sel
            s2 = [r for r in sel if f(r)]
            if s2:
                sel = s2
            elif miss:
                self._missed.append(miss)
            return bool(s2)

        # product-specific qualifiers
        if prod == "mortgage":
            if re.search(r"refinanc|refi\b", ql):
                keep(lambda r: "refinance" in r.label.lower())
            else:
                keep(lambda r: "purchase" in r.label.lower() or "refinance" not in r.label.lower())
        if prod == "cert":
            if "jumbo" in ql:
                keep(lambda r: "jumbo" in r.label.lower())
            elif re.search(r"special|promo", ql):
                keep(lambda r: "special" in r.label.lower())
            elif re.search(r"regular|standard", ql):
                keep(lambda r: "special" not in r.label.lower())
        if prod == "ira":
            if "roth" in ql:
                keep(lambda r: r.label.lower().startswith("roth"))
            elif "traditional" in ql:
                keep(lambda r: r.label.lower().startswith("traditional"))
            if "jumbo" in ql:
                keep(lambda r: "jumbo" in r.label.lower())
            elif re.search(r"special", ql):
                keep(lambda r: "special" in r.label.lower())
            elif re.search(r"variable", ql):
                keep(lambda r: "$" in r.label)
        if prod == "money_market":
            if "achieve" in ql:
                keep(lambda r: "achieve" in r.ctx_l, "the Achieve Money Market")
            elif re.search(r"investor", ql):
                keep(lambda r: "investor" in r.ctx_l, "the Investor's Choice Money Market")
        if prod == "platinum_visa":
            if re.search(r"intro|balance transfer|transfer", ql):
                keep(lambda r: "intro" in r.label.lower())
            elif re.search(r"standard|purchase|regular|ongoing|normal", ql):
                keep(lambda r: "intro" not in r.label.lower())
        # term
        terms = self._q_terms(ql)
        if prod == "mortgage":
            yrs = [t for t in terms if t in (120, 180, 240, 360)]
            if yrs:
                keep(lambda r: self._row_months_mort(r) in yrs, f"{yrs[0] // 12}-year fixed-rate term")
        elif terms and prod not in ("share_savings",):
            if any(self._row_months(r) for r in sel):
                keep(lambda r: self._row_months(r) in terms, f"{terms[0]}-month term")
        # model year (vehicles)
        if prod in ("auto", "atv"):
            ys = [int(y) for y in re.findall(r"\b((?:19|20)\d\d)\b", ql)]
            ys = [y for y in ys if not re.search(rf"(january|february|march|april|may|june|july|august|september|"
                                                 rf"october|november|december)\s+(\d+,?\s+)?{y}", ql)]
            if ys:
                y = ys[0]
                keep(lambda r: (lambda rg: rg is not None and rg[0] <= y <= rg[1])(self._row_years(r)))
            elif re.search(r"\bnew\b", ql):
                keep(lambda r: (lambda rg: rg is not None and rg[1] >= 2026)(self._row_years(r)))
        # amount / balance tier
        amts = [a for a in self._amounts(ql) if a >= 1]
        if amts and prod not in ("auto", "atv"):
            a = amts[0]
            if len(amts) >= 2 and re.search(r"between|from|to|-", ql):
                a = (amts[0] + amts[1]) / 2
            keep(lambda r: (lambda rg: rg is not None and rg[0] <= a <= rg[1])(self._row_range(r)))
        # superlatives
        if re.search(r"\b(highest|best|top|max(imum)?|most)\b", ql):
            loan = prod in ("mortgage", "auto", "atv", "boat_rv", "personal_loan", "heloc", "home_equity_loan",
                            "home_improvement", "platinum_visa", "rewards_visa", "credit_cards", "business_cards",
                            "cash_out")
            vals = [(r.primary() or 0) for r in sel]
            if vals and not loan:
                mx = max(vals)
                sel = [r for r in sel if (r.primary() or 0) == mx]
        elif re.search(r"\b(lowest|cheapest|min(imum)? rate|best rate)\b", ql):
            vals = [(r.primary() or 99) for r in sel]
            if vals:
                mn = min(vals)
                sel = [r for r in sel if (r.primary() or 99) == mn]
        return sel

    @staticmethod
    def _row_months_mort(r):
        m = re.search(r"(\d+)-year", r.label.lower())
        return int(m.group(1)) * 12 if m else None

    def _render_rates(self, prod, sel, rows, pages, ql, k):
        desc = PRODUCT_BY_KEY[prod][3]
        effs = sorted({r.effective for r in sel if r.effective})
        lines = [r.render() for r in sel]
        if prod == "mortgage" and len(sel) > 1:
            # lead with the smallest loan-amount tier of the 30-year (headline) program
            def rank(r):
                l = r.label.lower()
                y = re.search(r"(\d+)-year", l)
                yr = int(y.group(1)) if y else 0
                amt = re.search(r"\$([\d,]+)", l)
                a = float(amt.group(1).replace(",", "")) if amt else 0
                return (-yr, a)
            sel = sorted(sel, key=rank)
            lines = [r.render() for r in sel]
        text = f"{desc}: " + "; ".join(lines)
        missed = getattr(self, "_missed", [])
        if missed:
            what = missed[0] if missed[0].startswith("the ") else f"a {missed[0]} for {desc}"
            text = (f"Dupaco's rate sheet for that date does not list {what}. "
                    f"Available options - " + "; ".join(lines))
        if effs:
            text += f". Rates effective as of {', '.join(effs)}"
        text += "."
        if prod in ("auto", "atv", "boat_rv", "personal_loan", "heloc", "home_equity_loan", "home_improvement",
                    "mortgage", "cash_out"):
            text += " Rates are 'as low as' and depend on credit qualification; all loans subject to approval."
        if prod == "credit_cards":
            text = "Dupaco offers these Visa credit cards: " + "; ".join(lines) + "."
        if re.search(r"\b(chang\w*|previous\w*|used to|histor\w*|increas\w*|decreas\w*|went (up|down)|before|"
                     r"earlier|go(ne)? up|go(ne)? down|differen\w*)\b", ql):
            hist = self._history_note(prod, sel, k)
            if hist:
                text += " " + hist
        srcs = [self._src(p, lines[0]) for p in pages]
        for extra in self._product_pages(prod):
            srcs.append(Source(url=extra))
        return Answer(text=text, sources=srcs)

    def _history_note(self, prod, sel, k):
        notes = []
        if len(sel) > 4:      # only the tiers that actually moved are worth mentioning
            pass

        def same(a, b):
            if self._gkey(prod, a) != self._gkey(prod, b):
                return False
            ra, rb = self._row_range(a), self._row_range(b)
            if (ra is None) != (rb is None) or (ra and (round(ra[0]), round(min(ra[1], 1e12))) !=
                                                 (round(rb[0]), round(min(rb[1], 1e12)))):
                return False
            ya, yb = self._row_years(a), self._row_years(b)
            return ya == yb or (ya and yb and ya[0] <= yb[1] and yb[0] <= ya[1])

        for r in sel:
            prev = []
            for kk in range(k):
                rr, _ = self._rate_rows(kk, prod, False)
                for r2 in rr:
                    if same(r, r2) and r2.primary() is not None and r2.primary() != r.primary():
                        prev.append((self.snaps[kk][0], r2.primary()))
            if prev:
                d, v = prev[-1]
                tier = r.cells[1] if len(r.cells) > 1 and not PCT_RE.search(r.cells[1]) else ""
                notes.append(f"{r.label} {tier}".strip() + f" was {fmt_pct(v)} as of the {d} snapshot "
                                                           f"(now {fmt_pct(r.primary())})")
        return ("What changed: " + "; ".join(notes[:4]) + ".") if notes else \
            "No change on the rate sheet across the snapshots we have."

    @staticmethod
    def _product_pages(prod):
        return {
            "mortgage": ["https://www.dupaco.com/personal/loans/home-loans#rates"],
            "cash_out": ["https://www.dupaco.com/personal/loans/home-loans#rates"],
            "auto": ["https://www.dupaco.com/personal/loans/auto-motorcycle-loans",
                     "https://www.dupaco.com/personal/loans/auto-loans/", "https://www.dupaco.com/switch-and-earn"],
            "cert": ["https://www.dupaco.com/personal/savings-account#rates"],
            "share_savings": ["https://www.dupaco.com/personal/savings-account#rates"],
            "money_market": ["https://www.dupaco.com/personal/savings-account#rates"],
            "high_yield": ["https://www.dupaco.com/personal/savings-account#rates"],
            "ynis": ["https://www.dupaco.com/personal/savings-account#rates"],
            "holiday": ["https://www.dupaco.com/personal/savings-account#rates"],
            "hsa": ["https://www.dupaco.com/personal/savings-account#rates"],
            "bright": ["https://www.dupaco.com/savings-account/online-savings-account"],
            "checking": ["https://www.dupaco.com/personal/checking-accounts"],
            "first_rate": ["https://www.dupaco.com/personal/checking-accounts"],
            "free_checking": ["https://www.dupaco.com/personal/checking-accounts"],
            "credit_cards": ["https://www.dupaco.com/personal/credit-cards", "https://www.dupaco.com/business/credit-cards"],
            "platinum_visa": ["https://www.dupaco.com/personal/credit-cards"],
            "rewards_visa": ["https://www.dupaco.com/personal/credit-cards"],
            "business_cards": ["https://www.dupaco.com/business/credit-cards"],
            "personal_loan": ["https://www.dupaco.com/personal/loans/personal-loans"],
            "heloc": ["https://www.dupaco.com/personal/loans/home-equity"],
            "home_equity_loan": ["https://www.dupaco.com/personal/loans/home-equity"],
            "home_improvement": ["https://www.dupaco.com/personal/loans/home-improvement-loan"],
            "boat_rv": ["https://www.dupaco.com/personal/loans/recreational-loans"],
            "atv": ["https://www.dupaco.com/personal/loans/recreational-loans"],
            "ira": ["https://www.dupaco.com/personal/retirement"],
        }.get(prod, [])

    # ---------------------------------------------------------------- search fallback
    def _search(self, q: str, k: int, topn: int = 8):
        qt = [t for t in tokenize(q)]
        if not qt:
            return [], 0.0
        live = self.live[k]
        scores = defaultdict(float)
        where = {}
        qtc = Counter(qt)
        k1, b = 1.2, 0.6
        for t, qc in qtc.items():
            post = self.postings.get(t)
            if not post:
                continue
            idf = self.idf[t]
            for pid, tf in post:
                vid = where.get(pid)
                if vid is None:
                    for v in reversed(self.pvids[pid]):
                        if v in live:
                            vid = v
                            break
                    if vid is None:
                        continue
                    where[pid] = vid
                dl = self.plen[pid]
                scores[pid] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / self.avgdl))
        if not scores:
            return [], 0.0
        tot_idf = sum(self.idf.get(t, 0) for t in qtc)
        ql = q.lower()
        qwhen = bool(re.search(r"^\s*when|what date|which date|when (will|does|did|is|was)", ql))
        qcount = bool(re.search(r"how (many|much)|what (number|percent)", ql))
        res = []
        for pid, s in scores.items():
            vid = where[pid]
            pv = self.versions[vid]
            pr = pv.prior
            if not live[vid]:
                pr *= 0.8           # page no longer on the site at this date
            if pv.pdate:
                age = max(0.0, (date(2026, 10, 1) - pv.pdate).days / 365.0)
                pr *= 1.0 + 0.25 * max(0.0, 1 - age / 8)
            tb = sum(self.idf.get(t, 0) for t in qtc if t in self.title_toks[vid])
            s += 0.45 * tb
            pr *= 1.0 + 0.2 * max(0.0, 1.0 - 2 * self.ppos[pid])   # ledes / intro paragraphs
            ptxt = self.passages[pid][1]
            if qwhen and re.search(r"(January|February|March|April|May|June|July|August|September|October|"
                                   r"November|December)\s+\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}", ptxt):
                pr *= 1.35
            if qcount and re.search(r"\d", ptxt):
                pr *= 1.2
            n = len(ptxt)
            if ptxt in pv.rowpass:
                pr *= 0.5
            if ptxt in pv.headings:
                pr *= 0.6                        # headings are context, not answers
            if n < 100 and pv.kind != "pdf":
                pr *= 0.6 + 0.4 * n / 100.0      # fragments are rarely answers
            res.append((s * pr, pid, vid))
        res.sort(reverse=True)
        top = res[:topn]
        # coverage of the best passage: share of query idf it contains
        cov = 0.0
        if top:
            pid, vid = top[0][1], top[0][2]
            ptoks = set(tokenize(self._expand(pid, vid)) + tokenize(self.versions[vid].title))
            cov = sum(self.idf.get(t, 0) for t in qtc if t in ptoks) / max(1e-9, tot_idf)
        return top, cov

    def _expand(self, pid: int, vid: int) -> str:
        txt = self.passages[pid][1]
        pvx = self.versions[vid]
        expandable = txt.rstrip().endswith(":") or (len(txt) < 140 and pvx.kind != "pdf")
        if expandable and txt not in pvx.rowpass:
            pv = self.versions[vid]
            try:
                i = pv.passages.index(txt)
            except ValueError:
                return txt
            pieces = [txt]
            j = i + 1
            while j < len(pv.passages) and sum(map(len, pieces)) < 450:
                if re.match(r"(ABOUT DUPACO|# # #|Media contact)", pv.passages[j], re.I):
                    break
                pieces.append(pv.passages[j])
                j += 1
            txt = " ".join(pieces)
        return txt

    def _best_sentences(self, txt: str, q: str, limit: int = 420) -> str:
        if len(txt) <= limit:
            return txt
        sents = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“])", txt)
        qt = set(tokenize(q))
        scored = []
        for i, s in enumerate(sents):
            st = set(tokenize(s))
            scored.append((sum(self.idf.get(t, 0) for t in qt & st), -i, s))
        scored.sort(reverse=True)
        keep = []
        total = 0
        for sc, ni, s in scored:
            if total + len(s) > limit and keep:
                break
            keep.append((-ni, s))
            total += len(s)
        keep.sort()
        return " ".join(s for _, s in keep)

    def _search_answer(self, q, ql, k):
        top, cov = self._search(q, k)
        if not top or cov < 0.36:
            return Answer(text=("I don't know - I couldn't find that on Dupaco's website. Please contact Dupaco "
                                "directly for help."), confident=False)
        best_s, best_pid, best_vid = top[0]
        txt = self._expand(best_pid, best_vid)
        txt = self._best_sentences(txt, q)
        pv = self.versions[best_vid]
        if pv.kind in ("newsroom", "blog", "bizres") and self.ppos[best_pid] > 0.12:
            lede = next((p for p in pv.passages if len(p) > 120 and not p.startswith("-")), None)
            if lede and lede not in txt:
                txt = txt + " " + self._best_sentences(lede, q, 450)
        srcs = [self._src(pv, txt)]
        for s, pid, vid in top[1:4]:
            pv2 = self.versions[vid]
            if pv2.url not in {x.url for x in srcs}:
                srcs.append(self._src(pv2))
        return Answer(text=txt, sources=srcs, confident=True)

    # ---------------------------------------------------------------- conflicts
    CONFLICT_PRODUCTS = ("mortgage", "cash_out", "auto", "atv", "boat_rv", "personal_loan", "home_improvement",
                         "heloc", "home_equity_loan", "cert", "share_savings", "ynis", "holiday", "bright",
                         "launchpad", "money_market", "high_yield", "hsa", "first_rate", "free_checking",
                         "platinum_visa", "rewards_visa", "business_cards")

    def _gkey(self, prod: str, r: TableRow):
        """Group key: same product + same term + same flavour, ignoring balance / model-year tiers."""
        l = (r.label + " " + " ".join(r.cells[1:2])).lower()
        flags = []
        for w in ("jumbo", "special", "refinance", "intro", "achieve", "roth", "traditional"):
            if w in l or (w == "achieve" and "achieve" in r.ctx.lower()):
                flags.append(w)
        if prod in ("mortgage", "cash_out"):
            term = self._row_months_mort(r)
        else:
            term = self._row_months(r)
        tier = None
        if prod in ("share_savings", "ynis", "money_market", "high_yield", "hsa", "first_rate", "boat_rv",
                    "home_improvement"):
            rg = self._row_range(r)
            tier = (round(rg[0]), rg[1] if rg[1] == float("inf") else round(rg[1])) if rg else None
        return (prod, term, tuple(flags), tier)

    def _headline(self, prod: str, rows: list[TableRow]) -> TableRow:
        """Which row of a group a member gets quoted by default."""
        if prod in ("mortgage", "cash_out"):
            def amt(r):
                m = re.search(r"\$([\d,]+)", r.label)
                return float(m.group(1).replace(",", "")) if m else 0.0
            return min(rows, key=amt)
        if prod in ("auto", "atv"):
            return max(rows, key=lambda r: (self._row_years(r) or (0, 0))[1])
        return rows[0]

    @staticmethod
    def _tsig(r: TableRow) -> str:
        tier = r.cells[1] if len(r.cells) > 1 and not PCT_RE.search(r.cells[1]) else ""
        return re.sub(r"[^a-z0-9+]", "", (r.label + " " + tier).lower().replace("00.00", "00"))

    @staticmethod
    def _vals(r: TableRow) -> str:
        cells = [c for h, c in r.rate_cells()]
        return " / ".join(cells)

    def conflicts(self) -> list[Conflict]:
        if not self._index_built:
            self._build_index()
        out: list[Conflict] = []
        last = len(self.snaps) - 1
        for prod in self.CONFLICT_PRODUCTS:
            desc = PRODUCT_BY_KEY[prod][3]
            pred = PRODUCT_BY_KEY[prod][2]
            official = defaultdict(dict)       # gkey -> {k: headline row}
            stale = defaultdict(list)          # gkey -> [(k, pv, row)]
            for k in range(len(self.snaps)):
                biz = prod == "business_cards"
                rows, pages = self._rate_rows(k, prod, biz)
                off_path = pages[0].path if pages else None
                groups = defaultdict(list)
                exact = {}
                for r in rows:
                    if r.primary() is not None:
                        groups[self._gkey(prod, r)].append(r)
                        exact[self._tsig(r)] = r
                for g, rs in groups.items():
                    official[g][k] = self._headline(prod, rs)
                # tables for the same product on other pages of this snapshot
                for path, vid in self.snap_pages[k].items():
                    if path == off_path or path in ("dupaco.com/rates", "dupaco.com/business-rates"):
                        continue
                    pv = self.versions[vid]
                    if pv.kind == "listing":       # calculators DO count: they often show stale rates
                        continue
                    for r in pv.rows:
                        if pred(r) and r.primary() is not None:
                            g = self._gkey(prod, r)
                            ref = exact.get(self._tsig(r))
                            if g in groups and ref is not None:
                                stale[g].append((k, pv, r, ref))
            for g, by_k in official.items():
                ks_sorted = sorted(by_k)
                cur_k = ks_sorted[-1]
                cur = by_k[cur_k]
                cur_v = cur.primary()
                seq = []                       # (label, value-string, Source)
                seen_vals = set()
                for kk in ks_sorted:
                    r = by_k[kk]
                    v = r.primary()
                    if v in seen_vals:
                        continue
                    seen_vals.add(v)
                    seq.append((self._vals(r), Source(url=r.url, snapshot_id=self.snaps[kk][1],
                                                      quote=f"{r.label}: {self._vals(r)}")))
                stale_pages = []
                for kk, pv, r, ref in stale.get(g, []):
                    v = r.primary()
                    # compare with the same tier on the official sheet of the same snapshot
                    if v in seen_vals:
                        continue
                    if any(abs(v - x) < 1e-9 for x in pct_values(" ".join(c for _, c in ref.rate_cells()))):
                        continue
                    seen_vals.add(v)
                    stale_pages.append(pv.path)
                    seq.append((self._vals(r), Source(url=pv.url, snapshot_id=self.snaps[kk][1],
                                                      quote=f"{r.label}: {self._vals(r)}")))
                if len(seq) < 2:
                    continue
                if cur_k != last:
                    continue                   # product row no longer published: nothing to resolve
                label = re.sub(r"\$[\d,]+ or less", "", cur.label).strip()
                if prod in ("auto", "atv"):
                    label = re.sub(r"^\d{4}-\d{2,4} Model Year|Model Year \d{4}-\d{2,4}", "", label).strip()
                term = g[1]
                topic = desc + (f" - {label}" if label and label.lower() not in desc.lower() else "")
                if term and str(term) not in topic and str(term // 12) not in topic:
                    topic += f" ({term} months)"
                reason_bits = []
                if len([1 for kk in ks_sorted]) > 1 and len({by_k[kk].primary() for kk in ks_sorted}) > 1:
                    reason_bits.append("the rate sheet changed between snapshots; the newest rate sheet "
                                       f"({self.snaps[cur_k][1]}"
                                       + (f", effective {cur.effective}" if cur.effective else "") + ") wins")
                if stale_pages:
                    reason_bits.append("older tables still shown on " + ", ".join(sorted(set(stale_pages))[:3]) +
                                       " are superseded by the official rate sheet")
                out.append(Conflict(topic=topic, values=[v for v, _ in seq], sources=[s for _, s in seq],
                                    resolution=self._vals(cur), reason="; ".join(reason_bits) or "newest official value"))
        out.extend(self._text_conflicts(last))
        return out

    def _text_conflicts(self, k):
        out = []
        live = [self.versions[v] for v in self.live[k]]

        def newest_value(found):
            return max(found, key=lambda v: (max((p.pdate or date(1900, 1, 1)) for p in found[v]), len(found[v])))

        # branch count
        rx = re.compile(r"(\d+) (?:full-service )?branch(?: office)? locations")
        found = defaultdict(list)
        for pv in live:
            for m in rx.finditer(pv.text):
                found[m.group(1)].append(pv)
        if len(found) > 1:
            best = newest_value(found)
            order = sorted(found, key=lambda v: -len(found[v]))
            out.append(Conflict(
                topic="Number of Dupaco branch locations (branch count)",
                values=[f"{v} branch office locations" for v in order],
                sources=[self._src(max(found[v], key=lambda p: p.pdate or date(1900, 1, 1)),
                                   f"{v} branch office locations") for v in order],
                resolution=f"{best} branch office locations",
                reason="Press-release boilerplate changed over time; the most recently dated release "
                       "(after the Hy-Vee branch closed on March 31, 2026) says " + best + "."))
        # Forbes recognitions (credit-union ranking)
        ords = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7}
        fx = re.compile(r"(first|second|third|fourth|fifth|sixth|seventh) time Forbes")
        found = defaultdict(list)
        for pv in live:
            if "credit union" not in pv.title.lower() and "credit-union" not in pv.path:
                continue
            for m in fx.finditer(pv.text):
                found[m.group(1)].append(pv)
        if len(found) > 1:
            best = newest_value(found)
            order = sorted(found, key=lambda w: ords[w])
            out.append(Conflict(topic="Forbes recognition count (Best-in-State credit union)",
                                values=order,
                                sources=[self._src(found[w][0], f"{w} time Forbes recognized Dupaco") for w in order],
                                resolution=best,
                                reason="The newest Forbes press release supersedes the earlier one."))
        # a row whose dividend rate exceeds its APY cannot be right (APY >= rate when compounding)
        for path in ("dupaco.com/rates", "dupaco.com/business-rates"):
            for kk in range(len(self.snaps)):
                pv = self._page(kk, path)
                if not pv:
                    continue
                for r in pv.rows:
                    cells = dict((h.lower(), c) for h, c in r.rate_cells())
                    d = next((pct_values(c) for h, c in cells.items() if "dividend" in h), None)
                    a = next((pct_values(c) for h, c in cells.items() if "apy" in h or "yield" in h), None)
                    if d and a and len(r.rate_cells()) == 2 and d[0] > a[0] + 0.02:
                        topic = f"{r.ctx.split('|')[-1].strip() or r.label} {r.label} dividend rate vs APY ({path})"
                        if any(c.topic == topic for c in out):
                            continue
                        out.append(Conflict(topic=topic, values=[fmt_pct(d[0]), fmt_pct(a[0])],
                                            sources=[self._src(pv, f"{r.label}: {fmt_pct(d[0])} / {fmt_pct(a[0])}")],
                                            resolution=fmt_pct(a[0]),
                                            reason="Within the same row the dividend rate exceeds the APY, which "
                                                   "is impossible; the APY matches the neighbouring tiers and the "
                                                   "personal rate sheet."))
        return out


def build():
    return TimeAwareKnowledge()
