import json
import math
import random
import re
import threading
from collections import Counter, deque
from pathlib import Path
from types import SimpleNamespace

from chatterbot import ChatBot, languages
from chatterbot.comparisons import LevenshteinDistance
from chatterbot.conversation import Statement
from chatterbot.trainers import ListTrainer

# The word tools and the context linker live in prepare_data.py, so sentences
# and questions are always cut into words the same way.
try:
    from .prepare_data import TOKEN_RE, build_links, extract_keywords, extract_years, stem
except ImportError:  # started directly: python charlie.py
    from prepare_data import TOKEN_RE, build_links, extract_keywords, extract_years, stem

BASE = Path(__file__).resolve().parent
SENTENCES_PATH = BASE / "sentences.json"

# --- Settings you can tune ---
MIN_CONFIDENCE = 0.20   # best answer below this -> "Sorry...". Set 0 to always give the best available answer.
KEYWORD_WEIGHT = 0.6    # confidence = 0.6 * keyword match + 0.4 * ChatterBot's compare score
MAX_CANDIDATES = 30     # how many keyword matches get compared with the question
HIGH_CONFIDENCE = 0.7   # other sentences scoring at least this are sent too...
CLOSE_TO_BEST = 0.9     # ...and so are sentences scoring at least 90% of the best one (set 1 to switch off)
MAX_ANSWERS = 3         # never send more than this many sentences (keep it at 3 or more)
CONNECT_MIN = 0.5       # linked sentences at least this close may join the answer
TOPIC_CONFIDENCE = 0.8  # confidence given to "tell me about <page topic>" answers
SHOW_CATEGORY = False   # True: start the answer with [Page > Category], e.g. [Ingolstadt > Geography]
SORRY = "Sorry, I don't know how to answer this question."

# A sentence starting with one of these depends on the sentence before it ("It was founded in 806.")
CONTINUATION_WORDS = {"it", "its", "they", "their", "this", "these", "those", "he", "she", "his",
                      "her", "there", "however", "also", "moreover", "additionally", "such"}

# Question types: if the question matches the pattern, sentences containing
# these words are preferred (they don't have to contain them).
QUESTION_HINTS = [
    (r"\bhow (big|large)\b|\bsize\b",
     ["area", "km", "km2", "mi", "sq", "size", "square", "largest", "length", "hectares", "kilometres"]),
    (r"\bhow many (people|residents|inhabitants)\b|\bpopulation\b|\bhow many live\b",
     ["inhabitants", "population", "residents", "people"]),
    (r"\bwhere\b", ["located", "lies", "lie", "situated", "river", "north", "south", "east", "west"]),
    (r"\bwhen\b|\bwhat year\b|\bwhich year\b", ["founded", "established", "first", "mentioned", "since"]),
]
# Small talk goes straight to ChatterBot, before the sentence search
SMALL_TALK_RE = re.compile(
    r"^(hi+|hey+|hello|hallo|servus|moin|yo|good (morning|afternoon|evening|night)|"
    r"bye|goodbye|good bye|see you|cya|ciao|tschüss|thanks|thank you|thx|"
    r"how are you|how r u|who are you|what is your name|what's your name)\b")
# "random fact about THI", "tell me a fun fact about ingolstadt", "something interesting about audi"
RANDOM_FACT_RE = re.compile(r"\b(?:random|fun|interesting|cool)\s+facts?\b|\bsomething\s+(?:random|interesting)\b")
WANTS_YEAR_RE = re.compile(r"\bwhen\b|\bwhat year\b|\bwhich year\b")
WANTS_NUMBER_RE = re.compile(r"\bhow (many|much|big|large|long|tall|high|old|far)\b")


def analyse_question(question):
    """What the question asks for: keywords, years, and what kind of answer fits."""
    text = question.lower()
    years = extract_years(question)
    keywords = extract_keywords(question)
    hints = set()
    for pattern, words in QUESTION_HINTS:
        if re.search(pattern, text):
            hints |= {stem(w) for w in words}
    return SimpleNamespace(
        keywords=keywords,
        years=years,
        hints=hints - keywords,
        wants_year=bool(WANTS_YEAR_RE.search(text)) and not years,
        wants_number=bool(WANTS_NUMBER_RE.search(text)),
    )


# ---------- The knowledge: sentences.json with categories and links ----------

_index_lock = threading.Lock()
_INDEX = None          # everything built from sentences.json (see build_index)
_index_mtime = None    # modification time of sentences.json when the index was built
SENTENCES = []         # handy for quick checks: len(charlie.SENTENCES)


def _as_record(entry):
    """sentences.json holds objects {id, text, source, section, subsection, paragraph, links};
    old files hold plain strings."""
    if isinstance(entry, str):
        return {"text": entry, "section": "", "subsection": "", "paragraph": None,
                "source": "", "links": None}
    if isinstance(entry, dict) and isinstance(entry.get("text"), str):
        return {"text": entry["text"], "section": entry.get("section") or "",
                "subsection": entry.get("subsection") or "",
                "paragraph": entry.get("paragraph"), "source": entry.get("source") or "",
                "links": entry.get("links")}
    return None


def _clean_links(raw, n, i):
    """Keep only valid [other id, closeness] pairs."""
    links = []
    for pair in raw or []:
        try:
            j, c = int(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= j < n and j != i:
            links.append((j, c))
    return links


def build_index(entries):
    records = [r for r in (_as_record(e) for e in entries) if r]
    n = len(records)
    texts = [r["text"] for r in records]
    tokens = [extract_keywords(t) for t in texts]
    years = [extract_years(t) for t in texts]
    has_number = [bool(re.search(r"\d", t)) for t in texts]
    # First few words of each sentence: usually the subject ("Bavaria is ...")
    heads = [{stem(w) for w in TOKEN_RE.findall(t.lower())[:4]} for t in texts]
    # The category and sub-category (headings) of each sentence, as keywords
    cats = [extract_keywords(r["section"] + " " + r["subsection"]) for r in records]

    # The context connector: sentence -> [(linked sentence, closeness)], closest first.
    # Made by prepare_data.py; old files without links get them built here.
    if records and all(r["links"] is not None for r in records):
        links = [_clean_links(r["links"], n, i) for i, r in enumerate(records)]
    else:
        links = [[(j, c) for j, c in row] for row in build_links(records)]

    # Rare words count for more than common ones
    df = Counter(w for toks in tokens for w in toks)
    idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}

    # Pages and their Introduction, for "tell me about <page>" questions
    intro, source_tokens = {}, {}
    for i, r in enumerate(records):
        if r["source"]:
            source_tokens.setdefault(r["source"], extract_keywords(r["source"]))
            if r["section"] == "Introduction":
                intro.setdefault(r["source"], []).append(i)
    # Every sentence on a page is about the page's title ("The city boundary ..." is about Ingolstadt).
    # A title word belongs to the page with the shortest title containing it, so "ingolstadt"
    # belongs to the Ingolstadt page and not also to "Technische Hochschule Ingolstadt".
    owner = {}
    for source, toks in sorted(source_tokens.items(), key=lambda item: len(item[1])):
        for w in toks:
            owner.setdefault(w, source)
    page_tokens = [{w for w in source_tokens.get(r["source"], set()) if owner.get(w) == r["source"]}
                   for r in records]

    return SimpleNamespace(records=records, texts=texts, tokens=tokens, years=years,
                           has_number=has_number, heads=heads, cats=cats, links=links, idf=idf,
                           page_tokens=page_tokens,
                           max_idf=math.log(n + 1) + 1,  # weight of a word that appears nowhere
                           intro=intro, source_tokens=source_tokens)


def _file_mtime():
    try:
        return SENTENCES_PATH.stat().st_mtime
    except FileNotFoundError:
        return None


def reload_index(force=False):
    """(Re)load sentences.json and return the number of sentences.

    This runs automatically before every question and only does work when the
    file changed on disk, so freshly scraped sentences are picked up without
    restarting anything (and by every server worker, not just one).
    """
    global _INDEX, _index_mtime, SENTENCES
    mtime = _file_mtime()
    if not force and _INDEX is not None and mtime == _index_mtime:
        return len(_INDEX.texts)

    with _index_lock:
        mtime = _file_mtime()
        if not force and _INDEX is not None and mtime == _index_mtime:
            return len(_INDEX.texts)

        if mtime is None:
            print(f"Warning: {SENTENCES_PATH} not found. Run prepare_data.py first.")
            new_index = build_index([])
        else:
            try:
                new_index = build_index(json.loads(SENTENCES_PATH.read_text(encoding="utf-8")))
            except (ValueError, OSError) as e:
                print(f"Could not read {SENTENCES_PATH}: {e}")
                new_index = _INDEX if _INDEX is not None else build_index([])

        _INDEX = new_index
        _index_mtime = mtime
        SENTENCES = new_index.texts
        return len(SENTENCES)


reload_index()


# ---------- Finding and scoring candidates ----------

def _weight(idx, word):
    return idx.idf.get(word, idx.max_idf)


def _context_closeness(idx, i, word):
    """How close the context of sentence i is to a word: the closeness of the
    nearest linked sentence that contains it (0.5 if it is in the heading)."""
    best = 0.5 if word in idx.cats[i] else 0.0
    for j, closeness in idx.links[i]:
        if closeness > best and word in idx.tokens[j]:
            best = closeness
    return best


def find_candidates(question, limit=MAX_CANDIDATES):
    """Return [(sentence number, keyword score)], best first.

    keyword score (0-1) = how much of the question is found in the sentence
    itself, plus in its linked sentences (scaled by their closeness), plus
    whether it is the right kind of answer (a year for "when", a number for
    "how many", ...). Rare words count more; words that appear nowhere count as missing.
    """
    q = analyse_question(question)
    if not q.keywords and not q.years:
        return []  # e.g. "hi", "bye" -> nothing left after filtering

    reload_index()
    idx = _INDEX
    if not idx.texts:
        return []

    weights = {w: _weight(idx, w) for w in q.keywords}
    year_weight = idx.max_idf if q.years else 0.0
    # "soft" parts: is it the right kind of answer? Hint words (area, km2, located, ...)
    # are worth an average keyword; just containing a year or number is worth half of that.
    average = sum(weights.values()) / len(weights) if weights else idx.max_idf
    soft_checks = []
    if q.hints:  # one hint word counts half, two or more count fully ("largest" + "km")
        soft_checks.append((average, lambda i: min(len(q.hints & idx.tokens[i]) / 2, 1.0)))
    if q.wants_year:
        soft_checks.append((0.5 * average, lambda i: 1.0 if idx.years[i] else 0.0))
    if q.wants_number:
        soft_checks.append((0.5 * average, lambda i: 1.0 if idx.has_number[i] else 0.0))
    total = sum(weights.values()) + year_weight + sum(w for w, _ in soft_checks)

    # Longer questions must match at least 2 things, so one generic word
    # like "city" can't pick a random sentence.
    parts = len(q.keywords) + (1 if q.years else 0)
    min_matches = 1 if parts <= 2 else 2

    found = []
    for i, toks in enumerate(idx.tokens):
        own = q.keywords & (toks | idx.page_tokens[i])
        year_share = len(q.years & idx.years[i]) / len(q.years) if q.years else 0.0
        soft = sum(w * check(i) for w, check in soft_checks)
        if not (own or year_share or soft):
            continue  # nothing here at all - skip the context work

        # Question words this sentence lacks but its linked sentences have
        context = {}
        for w in q.keywords - own:
            closeness = _context_closeness(idx, i, w)
            if closeness > 0:
                context[w] = closeness

        # A sentence must contain part of the question itself, or be the right
        # kind of answer inside a context that matches ("It covers 133 km2." next to a match)
        if not (own or year_share or (context and soft)):
            continue
        if len(own) + len(context) + (1 if year_share else 0) < min_matches:
            continue

        own_w = sum(weights[w] for w in own) + year_weight * year_share + soft
        context_w = sum(weights[w] * c for w, c in context.items())
        score = min((own_w + context_w) / total, 1.0)
        # best score first; then subject-position keyword; then shorter sentence; then earlier in the text
        found.append(((score, len(own & idx.heads[i]), -len(toks)), i, score))

    found.sort(key=lambda f: f[0], reverse=True)
    return [(i, score) for _, i, score in found[:limit]]


def search_sentences(question):
    """Best keyword match only (no comparison, no context)."""
    candidates = find_candidates(question, limit=1)
    return _INDEX.texts[candidates[0][0]] if candidates else None


# ---------- Confidence: ChatterBot's compare / compare_text ----------

_comparator = None
_comparator_warned = False


def get_comparator():
    global _comparator
    if _comparator is None:
        try:
            _comparator = LevenshteinDistance(languages.ENG)  # ChatterBot 1.2+
        except TypeError:
            _comparator = LevenshteinDistance()               # ChatterBot 1.0.x
    return _comparator


def similarity(text_a, text_b):
    """ChatterBot's own comparison: 0 = nothing in common, 1 = identical.
    ChatterBot 1.2+ has compare_text(); 1.0.x only has compare() with Statement objects."""
    comparator = get_comparator()
    if hasattr(comparator, "compare_text"):
        return comparator.compare_text(text_a, text_b)
    return comparator.compare(Statement(text=text_a), Statement(text=text_b))


def _score(question, idx, i, keyword_score):
    """Confidence of sentence i: mostly how much of the question it covers,
    plus how similar ChatterBot finds the question and the sentence."""
    return KEYWORD_WEIGHT * keyword_score + (1 - KEYWORD_WEIGHT) * similarity(question, idx.texts[i])


# ---------- Building the answer from linked sentences ----------

def _continues(text):
    """True for sentences like "It was founded in 806." that only make sense after the previous one."""
    words = TOKEN_RE.findall(text.lower())[:1]
    return bool(words) and words[0] in CONTINUATION_WORDS


def _same_paragraph(idx, i, j):
    a, b = idx.records[i], idx.records[j]
    return a["paragraph"] is not None and (a["source"], a["paragraph"]) == (b["source"], b["paragraph"])


def connected_sentences(idx, i, q):
    """The linked sentences that belong in the answer next to sentence i:
    close enough (CONNECT_MIN) and either about the question too, or part of
    the same thought ("It ..." right after it, or right before an "It ...")."""
    wanted = q.keywords | q.hints
    chosen = []
    for j, closeness in idx.links[i]:
        if closeness < CONNECT_MIN:
            continue
        related = bool(wanted & idx.tokens[j]) or bool(q.years & idx.years[j])
        continues = _same_paragraph(idx, i, j) and (
            (j == i + 1 and _continues(idx.texts[j])) or (j == i - 1 and _continues(idx.texts[i])))
        if related or continues:
            chosen.append(j)
    return chosen


def _topic_sentences(q, idx):
    """'Tell me about Ingolstadt': when the question only names the topic of a
    page, answer with the opening of that page's Introduction."""
    if q.years or q.hints or q.wants_number or not q.keywords:
        return None  # a specific question, not "tell me about X"
    matches = [(len(toks), src) for src, toks in idx.source_tokens.items()
               if q.keywords <= toks and idx.intro.get(src)]
    if not matches:
        return None
    _, source = min(matches)  # exact / shortest page title first
    return idx.intro[source][:MAX_ANSWERS]


def _category(record):
    return " > ".join(p for p in (record["source"], record["section"], record["subsection"]) if p)


def _pack(idx, picked, confidence, main):
    parts = [(_category(idx.records[i]), idx.texts[i]) for i in picked]
    return {"parts": parts, "ids": list(picked), "main": main, "confidence": confidence,
            "category": _category(idx.records[main])}


def _format(parts):
    """Join the sentences. With SHOW_CATEGORY, every run of sentences from the
    same page and category gets its own label: [Ingolstadt > Geography] ..."""
    out, last = [], None
    for category, sentence in parts:
        if SHOW_CATEGORY and category and category != last:
            out.append(f"[{category}]")
        out.append(sentence)
        last = category
    return " ".join(out)


def find_answer(question):
    """Return {"parts": [(category, sentence), ...], "ids": [...], "main": id,
    "confidence": 0-1, "category": "Page > Section"} or None."""
    global _comparator_warned
    q = analyse_question(question)
    if not q.keywords and not q.years:
        return None
    reload_index()
    idx = _INDEX
    if not idx.texts:
        return None

    topic = _topic_sentences(q, idx)
    if topic:
        return _pack(idx, topic, TOPIC_CONFIDENCE, topic[0])

    candidates = find_candidates(question)
    if not candidates:
        return None

    try:
        scored = [(_score(question, idx, i, kw), i, kw) for i, kw in candidates]
    except Exception as e:
        if not _comparator_warned:
            print(f"Comparator unavailable, using the keyword score only: {e}")
            _comparator_warned = True
        scored = [(kw, i, kw) for i, kw in candidates]

    scored.sort(key=lambda s: s[0], reverse=True)  # stable: equal scores keep their order
    best_conf, best, best_kw = scored[0]

    # 1) context first: the best sentence plus its connected sentences, in reading order
    group = [best] + connected_sentences(idx, best, q)[:MAX_ANSWERS - 1]
    picked = sorted(group)

    # 2) then other sentences that are just as good an answer - and cover the
    #    question almost as well (so a short sentence can't sneak in on looks alone)
    for conf, i, kw in scored[1:]:
        if len(picked) >= MAX_ANSWERS:
            break
        if i in picked or kw < best_kw * CLOSE_TO_BEST:
            continue
        if conf >= HIGH_CONFIDENCE or conf >= best_conf * CLOSE_TO_BEST:
            picked.append(i)

    return _pack(idx, picked[:MAX_ANSWERS], best_conf, best)


def explain(question):
    """For testing in the terminal: which sentence won, and what it is connected to."""
    found = find_answer(question)
    if not found:
        return "  (no sentence matched)"
    idx = _INDEX
    main = found["main"]
    lines = [f"  best sentence #{main} [{found['category']}], confidence {found['confidence']:.2f}",
             "  its links (closeness):"]
    for j, closeness in idx.links[main][:5]:
        mark = "*" if j in found["ids"] else " "
        lines.append(f"   {mark} #{j:<4} {closeness:.2f}  {idx.texts[j][:70]}")
    lines.append("   (* = used in the answer)")
    return "\n".join(lines)


# ---------- Random facts ----------

_recent_facts = deque(maxlen=20)   # don't repeat the last 20 facts


def random_fact_topic(question):
    """For "random fact about THI" return "thi"; for "tell me a random fact" return "".
    Returns None if the question doesn't ask for a random fact."""
    text = question.lower()
    m = RANDOM_FACT_RE.search(text)
    if not m:
        return None
    rest = re.sub(r"^\s*(?:about|on|regarding|from|of|for|in)\b", "", text[m.end():])
    rest = re.sub(r"^\s*the\b", "", rest)
    return re.sub(r"[^\w\s\-]", " ", rest).strip()


def _initials(title):
    """'Technische Hochschule Ingolstadt' -> 'thi', so "THI" finds that page."""
    words = re.findall(r"\w+", title)
    initials = "".join(w[0] for w in words if w[0].isupper()).lower()
    return initials if len(initials) >= 2 else None   # one letter is not a useful name


def _pages_for(topic, idx):
    """The page(s) a topic names: by title words ("ingolstadt", "technische hochschule
    ingolstadt") or by initials ("thi"). The shortest matching title wins, so
    "ingolstadt" means the Ingolstadt page, not FC Ingolstadt 04."""
    words = extract_keywords(topic)
    plain = re.sub(r"\s+", " ", topic.lower()).strip()
    matches = []
    for source, toks in idx.source_tokens.items():
        if (words and words <= toks) or plain in (source.lower(), _initials(source)):
            matches.append((len(toks), source))
    if not matches:
        return []
    shortest = min(n for n, _ in matches)
    return [source for n, source in matches if n == shortest]


def _good_fact(idx, i):
    """Sentences that make a decent fact on their own: not too short, no German side
    notes in brackets, and no list introductions ("The THI offers the following
    Master's programmes.") whose list was never scraped."""
    text = idx.texts[i]
    return len(text) >= 40 and not text.startswith("(") and "the following" not in text.lower()


PRONOUNS = {"he", "she", "him", "his", "her", "they", "them", "their"}


def _needs_context(text):
    """Starts with "It/They/This ..." or mentions he/she/they somewhere ("... at Oxford he wrote")."""
    return _continues(text) or bool(PRONOUNS & set(TOKEN_RE.findall(text.lower())))


def _fact_with_context(idx, i):
    """The chosen sentence plus what it needs to make sense, in reading order:
    the sentence before it (same paragraph), more sentences before that while they
    still lean on earlier ones, the next one if it continues the thought, and a
    neighbour if the fact is first in its paragraph and very short. Max 3."""
    group = [i]
    if i > 0 and _same_paragraph(idx, i, i - 1):
        group.insert(0, i - 1)                     # always bring the sentence before
    while (len(group) < 4 and group[0] > 0 and _needs_context(idx.texts[group[0]])
           and _same_paragraph(idx, group[0], group[0] - 1)):
        group.insert(0, group[0] - 1)              # up to 4 so a fact rarely starts with "It ..."
    if _needs_context(idx.texts[group[0]]):
        # still mid-thought (a long chain of "It ..." sentences): start with the
        # paragraph's first sentence, which names the subject, then the fact itself
        first = group[0]
        while first > 0 and _same_paragraph(idx, first, first - 1):
            first -= 1
        if first != group[0]:
            group = [first] + ([i - 1] if i - 1 > first else []) + [i]
    nxt = group[-1] + 1
    if (len(group) < 3 and nxt < len(idx.texts) and _same_paragraph(idx, group[-1], nxt)
            and _continues(idx.texts[nxt])):
        group.append(nxt)
    if len(group) == 1 and len(idx.texts[i]) < 90:   # a short fact gets a neighbour
        for j in (i + 1, i - 1):
            if 0 <= j < len(idx.texts) and _same_paragraph(idx, i, j):
                group = sorted([i, j])
                break
    return group


def random_fact(topic):
    """Pick a random sentence about the topic (with its context). Returns the answer text."""
    reload_index()
    idx = _INDEX
    if not idx.texts:
        return "I don't know any facts yet. Add a Wikipedia page first."

    everything = [i for i in range(len(idx.texts)) if _good_fact(idx, i)]
    if not topic:
        pool = everything
    else:
        pages = set(_pages_for(topic, idx))
        if pages:   # the topic is a page title: any sentence from that page
            pool = [i for i in everything if idx.records[i]["source"] in pages]
        else:       # otherwise: sentences that mention the topic
            words = extract_keywords(topic)
            pool = [i for i in everything if words and words <= (idx.tokens[i] | idx.page_tokens[i])]
    if not pool:
        return f"I don't know any facts about {topic} yet."

    fresh = [i for i in pool if i not in _recent_facts] or pool
    chosen = random.choice(fresh)
    group = _fact_with_context(idx, chosen)
    _recent_facts.extend(group)
    return " ".join(idx.texts[j] for j in group)


# ---------- ChatterBot fallback (greetings, goodbyes, etc.) ----------

# Absolute path next to this file (/app in the container = ~/cai-bot on the server),
# so the small-talk memory survives rebuilds. "sqlite:///chatbot.db" was relative to
# the container's working folder "/", which is thrown away on every rebuild.
SMALLTALK_DB = "sqlite:///" + str(BASE / "smalltalk.db")

_chatbot = None


def get_chatbot():
    """Create ChatterBot on first use, so a ChatterBot/spaCy problem can't
    crash the whole app at import time. If its database is empty (first start,
    or the file was deleted), it trains itself - no button press needed."""
    global _chatbot
    if _chatbot is None:
        bot = ChatBot(
            "Charlie",
            logic_adapters=["chatterbot.logic.BestMatch"],
            database_uri=SMALLTALK_DB,
            read_only=True,
        )
        try:
            if bot.storage.count() == 0:
                print("small-talk memory is empty - training automatically")
                print(train())
        except Exception as e:
            print(f"could not check the small-talk memory: {e}")
        _chatbot = bot
    return _chatbot


def _chatterbot_answer(question):
    """ChatterBot's reply if it is confident enough, else None."""
    try:
        response = get_chatbot().get_response(question)
        if response.confidence >= 0.5:
            return response.text, response.confidence
    except Exception as e:
        print(f"ChatterBot fallback unavailable: {e}")
    return None


def is_small_talk(question):
    text = re.sub(r"[^\w\s']", " ", question.lower()).strip()
    return bool(SMALL_TALK_RE.match(text)) and len(text.split()) <= 5


def get_answer_with_confidence(question):
    """Return (answer_text, confidence)."""
    # 1) greetings, goodbyes, thanks: ChatterBot first
    if is_small_talk(question):
        reply = _chatterbot_answer(question)
        if reply:
            return reply

    # 2) "random fact about X": a random sentence from page X, with context
    topic = random_fact_topic(question)
    if topic is not None:
        return random_fact(topic), 1.0

    # 3) facts from sentences.json
    found = find_answer(question)
    if found and found["confidence"] >= MIN_CONFIDENCE:
        return _format(found["parts"]), found["confidence"]

    # 4) not a fact we know: maybe ChatterBot knows it
    reply = _chatterbot_answer(question)
    if reply:
        return reply
    return SORRY, (found["confidence"] if found else 0.0)


def get_answer(question):
    return get_answer_with_confidence(question)[0]


def train():
    print("training started...")
    try:
        trainer_bot = ChatBot(
            "Charlie",
            logic_adapters=["chatterbot.logic.BestMatch"],
            database_uri=SMALLTALK_DB,
            read_only=False,
        )
        trainer = ListTrainer(trainer_bot)

        trainer.train(["Hi!", "Hey there, I am Charlie!"])
        trainer.train(["Hello", "Hey there, I am Charlie!"])
        trainer.train(["How are you?", "I'm doing great, thanks for asking!"])
        trainer.train(["Thanks", "You're welcome!"])
        trainer.train(["Thank you", "You're welcome!"])
        trainer.train(["Who are you?", "I am Charlie, ask me about TH Ingolstadt and the city!"])
        trainer.train(["Goodbye", "Bye bye!"])
        trainer.train(["Bye", "Bye bye!"])
        print("training finished.")
        return "training completed"
    except Exception as e:
        return "error: " + str(e)


if __name__ == "__main__":
    # Quick terminal test: python charlie.py   (shows the winning sentence and its links)
    print("Charlie is ready. Type 'quit' to exit.")
    while True:
        q = input("You: ").strip()
        if q.lower() in {"quit", "exit"}:
            break
        answer, confidence = get_answer_with_confidence(q)
        print(f"Charlie: {answer}")
        print(explain(q))
