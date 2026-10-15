import re
import sys
import time
import json
import builtins
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import pandas as pd
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import config


# ============================================================
# CONFIG
# ============================================================

if not config.GEMINI_API_KEYS:
    print("ERROR: GEMINI_API_KEY is not set.")
    print("Create a .env file (see .env.example) with your own key, or")
    print("export GEMINI_API_KEY=... before running this script.")
    sys.exit(1)

TARGET_LANGUAGE = config.TARGET_LANGUAGE
MODEL_NAME = config.GEMINI_MODEL_NAME

MAX_CHARS = 30000
MAX_RETRIES = 3
DELAY_BETWEEN_CHUNKS = 1

BASE_DIR = Path(__file__).resolve().parent

CSV_FILE = config.ARTICLES_CSV
RAW_DIR = config.RAW_DIR
TRANSLATED_DIR = config.TRANSLATED_DIR

TRANSLATED_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# OUTPUT (several articles are translated at the same time)
# ============================================================
#
# Every line printed while translating an article is prefixed with its ID,
# e.g. "[15204] Saved: 0002.txt", so parallel output can be told apart.

_log = threading.local()
_print_lock = threading.Lock()


def print(*args, **kwargs):

    prefix = getattr(_log, "prefix", "")

    if prefix:

        if args and isinstance(args[0], str):
            text = args[0]
            body = text.lstrip("\n")
            args = (text[:len(text) - len(body)] + prefix + " " + body,) + args[1:]
        else:
            args = (prefix,) + args

    kwargs.setdefault("flush", True)

    with _print_lock:
        builtins.print(*args, **kwargs)


# ============================================================
# API KEY ROTATION
# ============================================================
#
# Several keys can be listed in .env (GEMINI_API_KEYS). When the current
# key hits its quota, the next key is used automatically:
#   - daily quota used up / key invalid -> key is dropped for this run
#   - per-minute limit                 -> switch keys; if every key is
#                                         rate limited, wait it out
# When every key's daily quota is used up, AllKeysExhausted is raised and
# the script stops cleanly (finished chunks are kept; re-run to resume).

class AllKeysExhausted(Exception):
    pass


# A key that keeps hitting the "per-minute" limit this many times in a row
# is treated as used up (guards against a misreported daily limit).
MAX_RATE_LIMIT_HITS = 5


def _error_text(error):

    return f"{error.message} {error.details}"


def _retry_delay(error, default=60):

    match = re.search(
        r"retryDelay['\"]?\s*:\s*['\"](\d+(?:\.\d+)?)s",
        _error_text(error)
    )

    if match:
        return float(match.group(1)) + 1

    return default


def _classify_error(error):

    # Returns "daily", "minute", "invalid" or None (some other error).

    text = _error_text(error)
    lower = text.lower()

    if error.code in (401, 403) or "api_key_invalid" in lower \
            or "api key not valid" in lower or "api key expired" in lower:
        return "invalid"

    if error.code == 429 or error.status == "RESOURCE_EXHAUSTED":

        # "limit: 0" = this model isn't available to the key at all
        if "perday" in lower or "per day" in lower \
                or re.search(r"limit: 0\b", lower):
            return "daily"

        return "minute"

    return None


class KeyPool:

    # Shared by all translation workers (thread-safe). Each request goes to
    # the usable key with the fewest requests in flight, so N workers spread
    # over N keys, and a key that hits its limit is skipped automatically.

    def __init__(self, keys):

        self.keys = keys
        self.clients = [genai.Client(api_key=k) for k in keys]
        self.lock = threading.Lock()
        self.dead = set()
        self.cooldown_until = {}
        self.rate_limit_hits = {}
        self.in_flight = {i: 0 for i in range(len(keys))}

    def label(self, i):

        return f"key {i + 1}/{len(self.keys)} (...{self.keys[i][-4:]})"

    def _pick(self):

        # Caller holds the lock. Returns (key index, seconds to wait).

        alive = [
            i for i in range(len(self.keys))
            if i not in self.dead
        ]

        if not alive:
            raise AllKeysExhausted()

        now = time.time()

        ready = [
            i for i in alive
            if self.cooldown_until.get(i, 0) <= now
        ]

        if ready:
            return min(ready, key=lambda i: (self.in_flight[i], i)), 0

        # Every usable key is rate limited -> wait for the soonest one
        soonest = min(alive, key=lambda i: self.cooldown_until[i])

        return soonest, self.cooldown_until[soonest] - now

    def _record_limit(self, i, kind, error):

        with self.lock:

            if kind == "minute":

                hits = self.rate_limit_hits.get(i, 0) + 1

                self.rate_limit_hits[i] = hits

                if hits >= MAX_RATE_LIMIT_HITS:
                    kind = "daily"

            if i in self.dead:
                return

            if kind == "minute":

                delay = _retry_delay(error)

                self.cooldown_until[i] = time.time() + delay

                message = (
                    f"{self.label(i)} hit its per-minute limit "
                    f"(free again in ~{delay:.0f}s)."
                )

            elif kind == "daily":

                self.dead.add(i)

                message = (
                    f"{self.label(i)} has used up its quota for today. "
                    f"{len(self.keys) - len(self.dead)} key(s) left."
                )

            else:

                self.dead.add(i)

                message = (
                    f"{self.label(i)} is invalid or not allowed "
                    f"({error.code} {error.status}). Skipping it."
                )

        print(message)

    def generate(self, prompt, generation_config):

        while True:

            with self.lock:

                i, wait_time = self._pick()

                if wait_time <= 0:
                    self.in_flight[i] += 1

            if wait_time > 0:

                print(
                    f"All keys are rate limited. "
                    f"Waiting {wait_time:.0f} seconds..."
                )

                time.sleep(wait_time)

                continue

            try:

                response = self.clients[i].models.generate_content(
                    model=MODEL_NAME,
                    contents=prompt,
                    config=generation_config
                )

            except genai_errors.APIError as e:

                kind = _classify_error(e)

                if kind is None:
                    raise

                self._record_limit(i, kind, e)

                continue

            finally:

                with self.lock:
                    self.in_flight[i] -= 1

            with self.lock:
                self.rate_limit_hits[i] = 0

            return response


key_pool = KeyPool(config.GEMINI_API_KEYS)


# ============================================================
# TRANSLATION PROMPTS
# ============================================================

# Shared by both prompts: how to write, and which markup must survive
# untouched so that links and images work on SciWiki (see sanitize.py).
TRANSLATION_RULES = f"""
How to write:

1. Write the way a skilled {TARGET_LANGUAGE} science writer would explain
   this topic to {TARGET_LANGUAGE} readers -- fluent, natural and easy to
   read. Do NOT translate word by word or keep English sentence structure.
   Rephrase, reorder, split or join sentences wherever that reads better
   in {TARGET_LANGUAGE}.
2. Keep ALL of the information: every fact, name, number, date, equation,
   example and reference. Do NOT summarize, shorten, skip or add anything.
3. Use the standard {TARGET_LANGUAGE} scientific term where one exists. For
   a technical term with no common {TARGET_LANGUAGE} equivalent, write it in
   {TARGET_LANGUAGE} script and give the English term in brackets the first
   time it appears, e.g. "<term in {TARGET_LANGUAGE}> (photosynthesis)".
4. Keep widely used acronyms (DNA, NASA, IUCN, ...) and scientific
   (Latin) names of species as they are, in italics as in the source.
5. Keep the headings and paragraph structure, and translate the headings.

Wiki markup -- follow exactly, or links and images will break:

6. Internal links: keep the link TARGET in English exactly as written and
   translate only the visible text after the "|".
     [[Photosynthesis]]       -> [[Photosynthesis|<photosynthesis in {TARGET_LANGUAGE}>]]
     [[Cell (biology)|cells]] -> [[Cell (biology)|<cells in {TARGET_LANGUAGE}>]]
   Never translate or change the part before the "|".
7. Images and galleries: keep "File:" and the file name EXACTLY as written
   (including the extension). Translate only the caption text.
     [[File:Zebra 01.jpg|thumb|A plains zebra]]
       -> [[File:Zebra 01.jpg|thumb|<caption in {TARGET_LANGUAGE}>]]
8. Templates {{{{...}}}}: keep template names and parameter names in English;
   translate only human-readable values (captions, descriptions, quotes).
9. Never change URLs, file names, <ref name="..."> names, DOIs, ISBNs, or
   anything inside <math>...</math>. Translate the text of references only
   where it is prose (e.g. a quote), not titles of English publications.
10. Keep every <ref>...</ref>, table, heading marker (==) and list marker.
"""

SYSTEM_PROMPT = f"""
You are an expert {TARGET_LANGUAGE} science writer translating an English
Wikipedia article into {TARGET_LANGUAGE}.
{TRANSLATION_RULES}
Output ONLY the {TARGET_LANGUAGE} article text -- no introduction, notes or
conclusion. Do not mention that the text is a chunk, and do not repeat
content from previous chunks.
"""


# ============================================================
# FIRST CHUNK PROMPT
# ============================================================

FIRST_CHUNK_PROMPT = f"""
You are an expert {TARGET_LANGUAGE} science writer translating an English
Wikipedia article into {TARGET_LANGUAGE}.

In addition to translating the article, identify the MAIN ARTICLE TITLE.

The title must be the actual title of the article, NOT:
- a section heading
- a subsection heading
- a caption
- a person's name appearing inside the article
- a random bold phrase
- an infobox field that is not the article title
- an HTML comment (text between <!-- and -->)

Return the result EXACTLY in this format:

TITLE:
<{TARGET_LANGUAGE} article title>

ARTICLE:
<{TARGET_LANGUAGE} translated article>

Rules for the title:

1. Translate the English article title naturally into {TARGET_LANGUAGE}.
2. Preserve proper names, scientific names, abbreviations and catalogue names where appropriate.
3. For astronomical catalogue/object names, preserve the scientific identifier.
4. Do not include explanations in the title.
5. Do not put quotation marks around the title.

Rules for the article:
{TRANSLATION_RULES}
Output only the requested TITLE and ARTICLE sections.
"""


# ============================================================
# SPLIT ARTICLE INTO PARAGRAPH-AWARE CHUNKS
# ============================================================

def split_into_chunks(text, max_chars=MAX_CHARS):

    paragraphs = text.split("\n\n")

    chunks = []
    current = ""

    for paragraph in paragraphs:

        # ----------------------------------------------------
        # Very large paragraph
        # ----------------------------------------------------

        if len(paragraph) > max_chars:

            if current:
                chunks.append(current)
                current = ""

            start = 0

            while start < len(paragraph):

                end = start + max_chars

                chunks.append(
                    paragraph[start:end]
                )

                start = end

            continue

        # ----------------------------------------------------
        # Add paragraph to current chunk
        # ----------------------------------------------------

        if len(current) + len(paragraph) + 2 <= max_chars:

            if current:
                current += "\n\n" + paragraph

            else:
                current = paragraph

        else:

            if current:
                chunks.append(current)

            current = paragraph

    if current:
        chunks.append(current)

    return chunks


# ============================================================
# CLEAN TITLE
# ============================================================

def clean_title(title):

    if not title:
        return ""

    title = title.strip()

    # Remove accidental markdown/wikilink formatting
    title = title.replace("'''", "")
    title = title.replace("''", "")

    # [[English|Bengali]]
    if "[[" in title and "]]" in title:

        import re

        match = re.search(
            r"\[\[[^|\]]+\|([^\]]+)\]\]",
            title
        )

        if match:
            title = match.group(1)

    # [[Bengali]]
    title = title.replace("[[", "")
    title = title.replace("]]", "")

    # Remove accidental quotation marks
    title = title.strip("\"'“”‘’ ")

    return title.strip()


# ============================================================
# PARSE FIRST CHUNK RESPONSE
# ============================================================

def parse_first_chunk_response(response_text):

    if not response_text:
        return None, None

    text = response_text.strip()

    # --------------------------------------------------------
    # Find TITLE:
    # --------------------------------------------------------

    title_marker = "TITLE:"

    article_marker = "ARTICLE:"

    title_pos = text.find(title_marker)
    article_pos = text.find(article_marker)

    if title_pos == -1 or article_pos == -1:
        print("Could not find TITLE/ARTICLE sections.")
        return None, None

    title = text[
        title_pos + len(title_marker):
        article_pos
    ].strip()

    article = text[
        article_pos + len(article_marker):
    ].strip()

    title = clean_title(title)

    if not title:
        print("Empty title received.")
        return None, None

    if not article:
        print("Empty translated article received.")
        return None, None

    return title, article


# ============================================================
# CHECK A TRANSLATION ISN'T CUT SHORT
# ============================================================
#
# A full translation comes back at about the same length as the source
# (median ~1.0) and keeps every <ref> tag. Gemini occasionally returns
# only part of a chunk, silently dropping text and references.

MIN_LENGTH_RATIO = 0.7
MIN_REF_RATIO = 0.8


def is_complete(source, translated):

    if len(source) > 2000:

        ratio = len(translated) / len(source)

        if ratio < MIN_LENGTH_RATIO:
            return False, f"only {ratio:.0%} of the source length"

    source_refs = source.count("<ref")

    if source_refs >= 5:

        kept = translated.count("<ref") / source_refs

        if kept < MIN_REF_RATIO:
            return False, f"only {kept:.0%} of references kept"

    return True, ""


# ============================================================
# TRANSLATE FIRST CHUNK
# ============================================================

def translate_first_chunk(
    text,
    chunk_number,
    total_chunks,
    english_title
):

    prompt = FIRST_CHUNK_PROMPT + f"""

The original English article title is:

{english_title}

This is chunk {chunk_number} of {total_chunks}.

Do not mention the chunk number in your output.

ARTICLE TEXT:
{text}
"""

    best = None

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = key_pool.generate(
                prompt,
                types.GenerateContentConfig(
                    temperature=0.2
                )
            )

            result = response.text

            if result and result.strip():

                title, article = parse_first_chunk_response(
                    result
                )

                if title and article:

                    ok, reason = is_complete(text, article)

                    if ok:
                        return title, article

                    print(
                        f"Attempt {attempt}/{MAX_RETRIES}: "
                        f"translation looks cut short ({reason})."
                    )

                    if best is None or len(article) > len(best[1]):
                        best = (title, article)

                    continue

                print(
                    "Could not parse Gemini response."
                )

            else:

                print(
                    "Empty response received."
                )

        except AllKeysExhausted:

            raise

        except Exception as e:

            print(
                f"Attempt {attempt}/{MAX_RETRIES} failed:"
            )

            print(e)

            if attempt < MAX_RETRIES:

                wait_time = attempt * 5

                print(
                    f"Retrying in {wait_time} seconds..."
                )

                time.sleep(wait_time)

    if best is not None:

        print(
            "WARNING: every attempt came back incomplete; "
            "keeping the most complete one."
        )

        return best

    return None, None


# ============================================================
# TRANSLATE NORMAL CHUNK
# ============================================================

def translate_chunk(
    text,
    chunk_number,
    total_chunks
):

    prompt = SYSTEM_PROMPT + f"""

Translate the following article text.

This is chunk {chunk_number} of {total_chunks}.

Do not mention the chunk number in your output.

ARTICLE TEXT:
{text}
"""

    best = None

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = key_pool.generate(
                prompt,
                types.GenerateContentConfig(
                    temperature=0.2
                )
            )

            result = response.text

            if result and result.strip():

                result = result.strip()

                ok, reason = is_complete(text, result)

                if ok:
                    return result

                print(
                    f"Attempt {attempt}/{MAX_RETRIES}: "
                    f"translation looks cut short ({reason})."
                )

                if best is None or len(result) > len(best):
                    best = result

                continue

            print(
                "Empty response received."
            )

        except AllKeysExhausted:

            raise

        except Exception as e:

            print(
                f"Attempt {attempt}/{MAX_RETRIES} failed:"
            )

            print(e)

            if attempt < MAX_RETRIES:

                wait_time = attempt * 5

                print(
                    f"Retrying in {wait_time} seconds..."
                )

                time.sleep(wait_time)

    if best is not None:

        print(
            "WARNING: every attempt came back incomplete; "
            "keeping the most complete one."
        )

        return best

    return None


# ============================================================
# SAVE TITLE
# ============================================================

def save_bengali_title(
    article_id,
    bengali_title
):

    title_file = (
        TRANSLATED_DIR /
        f"{article_id}_title.txt"
    )

    title_file.write_text(
        bengali_title,
        encoding="utf-8"
    )

    print(
        f"Title saved: {bengali_title}"
    )


# ============================================================
# TRANSLATE ONE ARTICLE
# ============================================================

def translate_article(
    article_id,
    english_title
):

    raw_file = RAW_DIR / f"{article_id}.txt"

    article_dir = (
        TRANSLATED_DIR /
        f"{article_id}_chunks"
    )

    article_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    final_file = (
        TRANSLATED_DIR /
        f"{article_id}.txt"
    )

    title_file = (
        TRANSLATED_DIR /
        f"{article_id}_title.txt"
    )

    # --------------------------------------------------------
    # Already completely translated
    # --------------------------------------------------------

    if final_file.exists() and title_file.exists():

        print(
            f"\n[{article_id}] "
            f"Already translated. Skipping..."
        )

        return True

    # --------------------------------------------------------
    # Check raw article
    # --------------------------------------------------------

    if not raw_file.exists():

        print(
            f"\n[{article_id}] "
            f"RAW FILE NOT FOUND"
        )

        return False

    text = raw_file.read_text(
        encoding="utf-8"
    )

    if not text.strip():

        print(
            f"\n[{article_id}] "
            f"Empty article"
        )

        return False

    print("\n" + "=" * 70)

    print(
        f"Article ID : {article_id}"
    )

    print(
        f"English    : {english_title}"
    )

    print(
        f"Length     : {len(text):,} characters"
    )

    # --------------------------------------------------------
    # Split article
    # --------------------------------------------------------

    chunks = split_into_chunks(text)

    total_chunks = len(chunks)

    print(
        f"Chunks     : {total_chunks}"
    )

    translated_chunks = []

    bengali_title = None

    # --------------------------------------------------------
    # Translate chunks
    # --------------------------------------------------------

    for i, chunk in enumerate(
        chunks,
        start=1
    ):

        chunk_file = (
            article_dir /
            f"{i:04d}.txt"
        )

        # ====================================================
        # FIRST CHUNK
        # ====================================================

        if i == 1:

            # ------------------------------------------------
            # Existing first chunk
            # ------------------------------------------------

            if chunk_file.exists():

                print(
                    f"[{i}/{total_chunks}] "
                    f"First chunk already exists."
                )

                translated = (
                    chunk_file.read_text(
                        encoding="utf-8"
                    )
                )

                # --------------------------------------------
                # Try to recover title from separate file
                # --------------------------------------------

                if title_file.exists():

                    bengali_title = (
                        title_file.read_text(
                            encoding="utf-8"
                        ).strip()
                    )

                else:

                    print(
                        "WARNING: First chunk exists "
                        "but title file is missing."
                    )

                    print(
                        "Re-translating first chunk "
                        "to generate title."
                    )

                    chunk_file.unlink()

                if (
                    bengali_title
                    and translated
                ):

                    translated_chunks.append(
                        translated
                    )

                    continue

            # ------------------------------------------------
            # Translate first chunk
            # ------------------------------------------------

            print(
                f"[{i}/{total_chunks}] "
                f"Translating + extracting title..."
            )

            print(
                f"Chunk size: "
                f"{len(chunk):,} characters"
            )

            bengali_title, translated = (
                translate_first_chunk(
                    chunk,
                    i,
                    total_chunks,
                    english_title
                )
            )

            if (
                bengali_title is None
                or translated is None
            ):

                print(
                    f"FAILED: Article "
                    f"{article_id}, "
                    f"chunk {i}"
                )

                return False

            # ------------------------------------------------
            # Save first chunk
            # ------------------------------------------------

            chunk_file.write_text(
                translated,
                encoding="utf-8"
            )

            # Keep an existing title: it is the page's name on
            # SciWiki, so changing it would create a second page.
            existing_title = (
                title_file.read_text(encoding="utf-8").strip()
                if title_file.exists()
                else ""
            )

            if existing_title:

                bengali_title = existing_title

                print(
                    f"Keeping existing title: {bengali_title}"
                )

            else:

                save_bengali_title(
                    article_id,
                    bengali_title
                )

            print(
                f"Saved: {chunk_file.name}"
            )

            translated_chunks.append(
                translated
            )

            time.sleep(
                DELAY_BETWEEN_CHUNKS
            )

            continue

        # ====================================================
        # NORMAL CHUNKS
        # ====================================================

        if chunk_file.exists():

            print(
                f"[{i}/{total_chunks}] "
                f"Already translated. Skipping..."
            )

            translated = (
                chunk_file.read_text(
                    encoding="utf-8"
                )
            )

            translated_chunks.append(
                translated
            )

            continue

        print(
            f"[{i}/{total_chunks}] "
            f"Translating..."
        )

        print(
            f"Chunk size: "
            f"{len(chunk):,} characters"
        )

        translated = translate_chunk(
            chunk,
            i,
            total_chunks
        )

        if translated is None:

            print(
                f"FAILED: Article "
                f"{article_id}, "
                f"chunk {i}"
            )

            return False

        # ----------------------------------------------------
        # Save immediately
        # ----------------------------------------------------

        chunk_file.write_text(
            translated,
            encoding="utf-8"
        )

        print(
            f"Saved: {chunk_file.name}"
        )

        translated_chunks.append(
            translated
        )

        time.sleep(
            DELAY_BETWEEN_CHUNKS
        )

    # --------------------------------------------------------
    # Make sure title exists
    # --------------------------------------------------------

    if not bengali_title:

        if title_file.exists():

            bengali_title = (
                title_file.read_text(
                    encoding="utf-8"
                ).strip()
            )

        else:

            print(
                "ERROR: Title was not generated."
            )

            return False

    # --------------------------------------------------------
    # Combine chunks
    # --------------------------------------------------------

    final_text = "\n\n".join(
        translated_chunks
    )

    final_file.write_text(
        final_text,
        encoding="utf-8"
    )

    print(
        f"\nCOMPLETED: {article_id}"
    )

    print(
        f"Title: {bengali_title}"
    )

    print(
        f"Article saved: {final_file}"
    )

    return True


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print(f"{TARGET_LANGUAGE.upper()} WIKIPEDIA TRANSLATION")
    print("=" * 70)
    print(f"Model: {MODEL_NAME}")
    print(f"API keys: {len(config.GEMINI_API_KEYS)}")

    # --------------------------------------------------------
    # Read article list, skip finished articles
    # --------------------------------------------------------

    df = pd.read_csv(
        CSV_FILE
    )

    todo = []

    for _, row in df.iterrows():

        article_id = int(row["article_id"])

        done = (
            (TRANSLATED_DIR / f"{article_id}.txt").exists()
            and (TRANSLATED_DIR / f"{article_id}_title.txt").exists()
        )

        if not done:
            todo.append((article_id, str(row["english_title"])))

    workers = max(1, min(
        config.TRANSLATE_WORKERS or len(config.GEMINI_API_KEYS),
        len(todo) or 1
    ))

    print(f"Total articles     : {len(df)}")
    print(f"Already translated : {len(df) - len(todo)}")
    print(f"To translate       : {len(todo)}")
    print(f"At the same time   : {workers}")

    # --------------------------------------------------------
    # Translate several articles at once
    # --------------------------------------------------------

    stop = threading.Event()

    counts = Counter()

    def work(article_id, english_title):

        if stop.is_set():
            return "not started"

        _log.prefix = f"[{article_id}]"

        try:

            if translate_article(article_id, english_title):
                return "success"

            return "failed"

        except AllKeysExhausted:

            stop.set()

            return "not finished"

        except Exception as e:

            print(f"ERROR: {e}")

            return "failed"

        finally:

            _log.prefix = ""

    executor = ThreadPoolExecutor(max_workers=workers)

    pending = {
        executor.submit(work, article_id, english_title)
        for article_id, english_title in todo
    }

    try:

        while pending:

            # Short timeout keeps Ctrl+C responsive on Windows
            finished, pending = wait(
                pending,
                timeout=1,
                return_when=FIRST_COMPLETED
            )

            for future in finished:

                result = future.result()

                counts[result] += 1

                if result in ("success", "failed"):

                    print(
                        f"--- Progress: "
                        f"{counts['success'] + counts['failed']}/{len(todo)} "
                        f"done ({counts['failed']} failed)"
                    )

        executor.shutdown()

    except KeyboardInterrupt:

        stop.set()

        print(
            "\n\nStopped by user. Finishing the requests already "
            "in progress (press Ctrl+C again to quit at once)..."
        )

        executor.shutdown(wait=True, cancel_futures=True)

        print(
            "You can run the script again to resume."
        )

    if stop.is_set() and counts["not finished"]:

        print(
            "\n\nAll API keys have used up "
            "today's quota."
        )

        print(
            "Add more keys to GEMINI_API_KEYS in .env, "
            "or run again after the daily reset "
            "(midnight Pacific time)."
        )

        print(
            "Finished chunks are saved; the next run resumes."
        )

    # --------------------------------------------------------
    # Final summary
    # --------------------------------------------------------

    print("\n")

    print("=" * 70)
    print("TRANSLATION FINISHED")
    print("=" * 70)

    print(
        f"Successful : {counts['success']}"
    )

    print(
        f"Failed     : {counts['failed']}"
    )

    print(
        f"Not done   : "
        f"{len(todo) - counts['success'] - counts['failed']}"
    )

    print(
        f"Output     : {TRANSLATED_DIR}"
    )

    print("=" * 70)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()