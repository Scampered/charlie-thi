import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:  # NLTK comes with ChatterBot; the Porter stemmer needs no extra downloads
    from nltk.stem import PorterStemmer
    _porter = PorterStemmer()
except Exception:
    _porter = None

BASE = Path(__file__).resolve().parent

# On your host, data.json lives in ../cai_scraper/cai_scraper/spiders/ relative to this file.
# You can override it: python prepare_data.py path/to/data.json
DEFAULT_INPUT = BASE / ".." / "cai_scraper" / "cai_scraper" / "spiders" / "data.json"
OUTPUT_PATH = BASE / "sentences.json"

MIN_LENGTH = 20  # shorter "sentences" are almost always fragments
LEAD_SECTION = "Introduction"  # category of the text before the first heading

# --- Context links ---
LINKS_PER_SENTENCE = 8   # keep the 8 closest sentences of every sentence
MIN_CLOSENESS = 0.10     # weaker links are not stored
POSITION_WEIGHT = 0.6    # closeness = 0.6 * position closeness + 0.4 * keyword closeness
# Position closeness: how near two sentences are in the original article
POSITION_CLOSENESS = {
    "next to each other": 1.0,     # directly above / below, same paragraph
    "two apart": 0.7,              # same paragraph, one sentence in between
    "same paragraph": 0.5,
    "same subsection": 0.3,        # same h3 heading, different paragraph
    "same section": 0.2,           # same h2 heading, different paragraph
}

# Sections that are lists, links or citations, not facts. Everything under them is skipped.
SKIP_SECTIONS = {
    "see also", "references", "notes", "external links", "further reading", "gallery",
    "notable people", "twin towns – sister cities", "twin towns - sister cities",
    "organizations and clubs", "bibliography", "sources", "citations", "literature",
}

# Abbreviations that end in a period but do NOT end a sentence.
ABBREVIATIONS = (
    "St.", "Dr.", "Prof.", "Mr.", "Mrs.", "Ms.", "No.", "Nr.", "vs.", "etc.",
    "e.g.", "i.e.", "approx.", "ca.", "Jan.", "Feb.", "Mar.", "Apr.", "Jun.",
    "Jul.", "Aug.", "Sep.", "Sept.", "Oct.", "Nov.", "Dec.", "Inc.", "Ltd.",
)

# Words that say nothing about the content (also used by charlie.py for questions)
STOP_WORDS = {
    # articles, pronouns, helpers
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "am", "of", "in",
    "on", "at", "to", "for", "and", "or", "but", "nor", "not", "no", "yes", "it", "its",
    "this", "that", "these", "those", "what", "which", "who", "whom", "whose", "where",
    "when", "why", "how", "i", "me", "my", "mine", "you", "your", "yours", "we", "us", "our",
    "he", "him", "his", "she", "her", "hers", "they", "them", "their", "theirs", "itself",
    "himself", "herself", "themselves", "do", "does", "did", "done", "doing", "can", "could",
    "would", "should", "will", "shall", "may", "might", "must", "with", "without", "as",
    "by", "from", "into", "onto", "out", "up", "down", "over", "under", "off", "about",
    "above", "below", "between", "through", "during", "before", "after", "again", "also",
    "there", "here", "some", "any", "many", "much", "more", "most", "very", "just", "only",
    "own", "same", "such", "other", "each", "every", "all", "both", "few", "has", "have",
    "had", "having", "if", "so", "than", "then", "too", "now", "ever", "even", "still",
    # chat words that are not about the content
    "tell", "please", "happened", "happen", "happens", "give", "show", "know", "get", "got",
    "let", "say", "said", "want", "like", "thing", "things", "something", "anything",
    "ok", "okay", "yeah", "hey", "hi", "hello", "thanks", "thank",
    "big", "large", "long", "year",
}
TOKEN_RE = re.compile(r"[a-zäöüß0-9]+")
# 4-digit years and ranges: 2007, 2007-2009, 2007–08, 2010/11
YEAR_RE = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?:\s*[–—\-/]\s*(\d{4}|\d{2}))?(?![\d,.]\d)")


# ---------- Word tools (shared with charlie.py) ----------

_stem_cache = {}


def stem(word):
    """'boundaries' and 'boundary' -> the same stem, so different word forms match."""
    result = _stem_cache.get(word)
    if result is None:
        result = word
        if len(word) <= 3:
            pass  # short words are left alone: the stemmer would turn "his" into "hi"
        elif _porter is not None:
            try:
                result = _porter.stem(word)
            except Exception:
                pass
        else:  # tiny fallback if NLTK is missing
            for suffix, repl in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
                if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                    result = word[:-len(suffix)] + repl
                    break
        _stem_cache[word] = result
    return result


def _is_year(word):
    return len(word) == 4 and word.isdigit() and 1000 <= int(word) <= 2099


def extract_keywords(text):
    """Lowercase, drop punctuation, stop words, years and tiny numbers, stem the rest."""
    words = set()
    for w in TOKEN_RE.findall(text.lower()):
        if w in STOP_WORDS or len(w) <= 1 or _is_year(w):
            continue
        if w.isdigit() and len(w) <= 2:  # "08" from "2007-08", "2" from "2. Bundesliga"
            continue
        words.add(stem(w))
    return words


def extract_years(text):
    """All years a text talks about. Ranges are expanded: '2007-09' -> {2007, 2008, 2009}."""
    years = set()
    for m in YEAR_RE.finditer(text):
        start = end = int(m.group(1))
        if m.group(2):
            tail = m.group(2)
            end = int(tail) if len(tail) == 4 else start // 100 * 100 + int(tail)
            if end < start or end - start > 50:
                end = start
        years.update(range(start, end + 1))
    return years


# ---------- Cleaning and splitting ----------

def clean_text(text):
    # Citation markers and bracket junk: [1], [a], [de], [Wikidata], [citation needed], [edit]
    text = re.sub(r"\[[^\]]*\]", "", text)
    # Leftover HTML tags and entities
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    # Quotes and stray formatting characters
    text = text.replace('"', "").replace("“", "").replace("”", "")
    text = text.replace("​", "").replace("ⓘ", "")
    # Pronunciation/language leftovers like "(German: ; Austro-Bavarian: )" and empty "()".
    # Parentheses with real content, e.g. "(as of 31 December 2023)", are kept.
    text = re.sub(r"\(\s*(?:[A-Za-z\- ]+:\s*[;,]?\s*)+\)", "", text)
    text = re.sub(r"\(\s*\)", "", text)
    # Spaces before punctuation left behind by removed markers, then collapse whitespace
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


SENTENCE_END_RE = re.compile(
    # after . ! ? - or after .) .] ." - when the next sentence starts with a capital, a digit or "("
    r"(?:(?<=[.!?])|(?<=[.!?][)\]'\"]))\s+(?=[(A-ZÄÖÜ0-9])")


def split_sentences(text):
    # Split after . ! ? when the next sentence starts with a capital letter, digit or bracket
    chunks = SENTENCE_END_RE.split(text)

    # Glue chunks back together when the split happened after an abbreviation
    # after an ordinal number like "2. Bundesliga", or after a single letter like "a. d. Donau"
    merged = []
    for chunk in chunks:
        if merged and (merged[-1].endswith(ABBREVIATIONS)
                       or re.search(r"(?:^|\s)\d{1,2}\.$", merged[-1])        # "2. Bundesliga"
                       or re.search(r"(?:^|\s)[A-Za-z]\.$", merged[-1])):     # "a. d. Donau", "J. S. Bach"
            merged[-1] = merged[-1] + " " + chunk
        else:
            merged.append(chunk)
    return merged


def is_good_sentence(sentence):
    if len(sentence) < MIN_LENGTH or len(sentence.split()) < 5:
        return False
    # Real sentences end with . ! ? (maybe followed by a closing bracket or quote).
    # This drops list intros like "Ingolstadt is twinned with:".
    if not re.search(r"[.!?][)\]'\"]*$", sentence):
        return False
    if not re.search(r"[A-Za-zÄÖÜäöüß]{3,}", sentence):
        return False
    # Mostly digits/symbols (tables, coordinates, etc.)
    letters = sum(c.isalpha() for c in sentence)
    return letters / len(sentence) >= 0.5


def extract_sentences(scraped_data):
    """Walk through data.json in order and return one record per sentence:
        {"text": ..., "section": "History", "subsection": "Middle Ages",
         "paragraph": 3, "source": "Ingolstadt"}
    section    = the h2 heading (category) the sentence is under
    subsection = the h3 heading under that (sub-category), "" if there is none
    paragraph  = number of the paragraph on its page (sentences with the same
                 number are neighbours in the original text)
    source     = the Wikipedia page it came from
    """
    # When several pages are scraped at once, Scrapy can mix their items in data.json.
    # So every page keeps its own reading position (heading, paragraph count, ...),
    # and the sentences are grouped per page in the order the pages first appear.
    pages = {}   # source -> {"section", "subsection", "paragraph", "skipping", "records"}
    seen = set()

    for item in scraped_data:
        if not isinstance(item, dict):
            continue

        source = clean_text(item.get("source") or "")
        page = pages.setdefault(source, {"section": LEAD_SECTION, "subsection": "",
                                         "paragraph": -1, "skipping": False, "records": []})
        text = clean_text(item.get("text", ""))
        tag = item.get("tag")

        if tag == "h2":
            if text:
                page["section"], page["subsection"] = text, ""
                page["skipping"] = text.lower() in SKIP_SECTIONS
            continue
        if tag == "h3":
            if text:
                page["subsection"] = text
            continue
        if tag != "p" or page["skipping"]:
            continue

        kept = [s.strip() for s in split_sentences(text) if is_good_sentence(s.strip())]
        if not kept:
            continue
        page["paragraph"] += 1
        for sentence in kept:
            key = sentence.lower()
            if key in seen:
                continue
            seen.add(key)
            page["records"].append({"text": sentence, "section": page["section"],
                                    "subsection": page["subsection"],
                                    "paragraph": page["paragraph"], "source": source})

    records = []
    for page in pages.values():
        records.extend(page["records"])
    return records


# ---------- Context links between sentences ----------

def build_links(records, per_sentence=LINKS_PER_SENTENCE, min_closeness=MIN_CLOSENESS):
    """Connect every sentence to its closest sentences.

    Returns one list per sentence: [[other sentence id, closeness], ...], closest first.
    closeness (0-1) = 0.6 * position closeness + 0.4 * keyword closeness
      position closeness: 1.0 directly above/below, 0.7 two apart, 0.5 same paragraph,
                          0.3 same subsection, 0.2 same section, 0 elsewhere
      keyword closeness:  how many (rare) words the two sentences share, 0-1
    So neighbours in the text always come first; sentences elsewhere (even on
    other pages) are linked when they talk about the same things.
    """
    n = len(records)
    tokens = [extract_keywords(r["text"]) for r in records]
    df = Counter(w for toks in tokens for w in toks)
    idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}
    totals = [sum(idf[w] for w in toks) for toks in tokens]

    # Very common words ("city", "ingolstadt") link everything to everything: ignore them for linking
    too_common = max(20, n // 10)
    postings = defaultdict(list)
    for i, toks in enumerate(tokens):
        for w in toks:
            if df[w] <= too_common:
                postings[w].append(i)

    # Where every sentence sits in its article
    paragraphs, sections, subsections = defaultdict(list), defaultdict(list), defaultdict(list)
    for i, r in enumerate(records):
        if r.get("paragraph") is None or not r.get("section"):
            continue  # old records without context: keyword links only
        paragraphs[(r["source"], r["paragraph"])].append(i)
        sections[(r["source"], r["section"])].append(i)
        if r.get("subsection"):
            subsections[(r["source"], r["section"], r["subsection"])].append(i)

    position = [defaultdict(float) for _ in range(n)]
    for members in sections.values():
        for i in members:
            for j in members:
                if i != j:
                    position[i][j] = POSITION_CLOSENESS["same section"]
    for members in subsections.values():
        for i in members:
            for j in members:
                if i != j:
                    position[i][j] = POSITION_CLOSENESS["same subsection"]
    for members in paragraphs.values():
        for a, i in enumerate(members):
            for b, j in enumerate(members):
                distance = abs(a - b)
                if distance == 1:
                    position[i][j] = POSITION_CLOSENESS["next to each other"]
                elif distance == 2:
                    position[i][j] = POSITION_CLOSENESS["two apart"]
                elif distance > 2:
                    position[i][j] = POSITION_CLOSENESS["same paragraph"]

    links = []
    for i in range(n):
        shared = defaultdict(float)
        for w in tokens[i]:
            for j in postings.get(w, ()):
                if j != i:
                    shared[j] += idf[w]

        scored = []
        for j in set(shared) | set(position[i]):
            union = totals[i] + totals[j] - shared[j]
            keyword = shared[j] / union if union > 0 else 0.0
            closeness = POSITION_WEIGHT * position[i][j] + (1 - POSITION_WEIGHT) * keyword
            if closeness >= min_closeness:
                scored.append((round(closeness, 2), j))
        scored.sort(key=lambda s: (-s[0], s[1]))
        links.append([[j, c] for c, j in scored[:per_sentence]])
    return links


# ---------- Reading and writing sentences.json ----------

def load_existing(path):
    """Return the records already stored in sentences.json (empty list if none).
    Old files that are a plain list of strings are accepted too."""
    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} does not contain a list of sentences")

    records = []
    for entry in data:
        if isinstance(entry, str):
            records.append({"text": entry, "section": "", "subsection": "",
                            "paragraph": None, "source": ""})
        elif isinstance(entry, dict) and isinstance(entry.get("text"), str):
            records.append({"text": entry["text"], "section": entry.get("section") or "",
                            "subsection": entry.get("subsection") or "",
                            "paragraph": entry.get("paragraph"), "source": entry.get("source") or ""})
    return records


def write_sentences(records, output_path=OUTPUT_PATH):
    """Number the sentences, link them, and save sentences.json (one sentence per line)."""
    output_path = Path(output_path)
    links = build_links(records)
    lines = []
    for i, r in enumerate(records):
        entry = {"id": i, "text": r["text"], "source": r["source"], "section": r["section"],
                 "subsection": r["subsection"], "paragraph": r["paragraph"], "links": links[i]}
        lines.append(json.dumps(entry, ensure_ascii=False))

    # Write to a temp file and swap it in, so a running Charlie never reads a half-written file
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as out:
        out.write("[\n" + ",\n".join(lines) + "\n]\n")
    os.replace(str(tmp_path), str(output_path))


def update_sentences(input_path=DEFAULT_INPUT, output_path=OUTPUT_PATH, append=True):
    """Read data.json, clean it, and add the sentences to sentences.json.

    append=True  keeps everything already in sentences.json. A page that is
                 scraped again replaces its old sentences, and old sentences
                 without context are upgraded when the same text shows up again.
    append=False starts sentences.json from scratch.
    All links are rebuilt every time, so new pages get connected to old ones.
    Returns (added, total).
    """
    input_path = Path(input_path).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Scraper data file not found at {input_path}")

    scraped_data = json.loads(input_path.read_text(encoding="utf-8"))
    new_records = extract_sentences(scraped_data)

    existing = load_existing(output_path) if append else []
    old_texts = {r["text"].lower() for r in existing}
    new_sources = {r["source"] for r in new_records if r["source"]}
    new_texts = {r["text"].lower() for r in new_records}

    combined = [r for r in existing
                if r["source"] not in new_sources
                and not (not r["source"] and r["text"].lower() in new_texts)]
    seen = {r["text"].lower() for r in combined}

    added = 0
    for record in new_records:
        key = record["text"].lower()
        if key in seen:
            continue
        seen.add(key)
        combined.append(record)
        if key not in old_texts:
            added += 1

    write_sentences(combined, output_path)
    return added, len(combined)


def process_sentences(input_path=DEFAULT_INPUT, output_path=OUTPUT_PATH, append=True):
    """Same as update_sentences, but returns a message string (used by the command line)."""
    try:
        added, total = update_sentences(input_path, output_path, append)
    except FileNotFoundError as e:
        return f"Error: {e}"
    return f"Success: added {added} new sentences ({total} total) in {output_path}"


if __name__ == "__main__":
    # python prepare_data.py [path/to/data.json] [--fresh]
    #   --fresh  rebuild sentences.json from scratch instead of appending
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fresh = "--fresh" in sys.argv[1:]
    source = args[0] if args else DEFAULT_INPUT
    print(process_sentences(source, append=not fresh))
