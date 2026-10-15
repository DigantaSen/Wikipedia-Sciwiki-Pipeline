"""
config.py

Central configuration for the Wikipedia -> regional-language SciWiki
translation pipeline. Edit the values below before running the pipeline:

    01_collect.py -> 02_download.py -> 03_translate.py -> 04_upload.py

Secrets (Gemini API key, SciWiki password) are NOT set in this file.
The Gemini key is read from a ".env" file (see .env.example) or an
environment variable; the SciWiki password is always typed interactively
by 04_upload.py and never stored anywhere.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


# ============================================================
# 1. TOPICS
# ============================================================
# Wikipedia pages to start exploring from (subtopics of Wikipedia's
# "List of lists of lists"). 01_collect.py rotates through these: each
# run continues from the topic after the one used last time, and moves on
# to the next topic early if one runs out of new articles.
TOPICS = [
    "Lists of plants",
    "Lists of diseases",
    "Lists of drugs",
    "Lists of molecules",
    "Lists of compounds",
    "Lists of human genes",
    "Lists of extinct species",
    "Lists of prehistoric animals",
    "Lists of organisms by population",
    "Lists of invasive species",
    "Lists of plant diseases",
    "Lists of reptiles",
    "Lists of volcanoes",
    "Lists of earthquakes",
    "Lists of ecoregions",
    "Lists of renewable energy topics",
    "Lists of artificial intelligence topics",
    "Lists of programming languages",
    "Lists of network protocols",
    "Lists of fatty acids",
    "Lists of nuclear reactors",
    "Lists of rockets",
    "Lists of pharmaceutical industry topics",
    "Lists of animals",
]

# Topics used by other team members. These are never used as a starting
# point, and the collector never explores into them either.
EXCLUDED_TOPICS = [
    "Lists of astronomical objects",
    "Lists of scientists",
    "Lists of science and technology awards",
    "Lists of sequenced genomes",
    "Lists of mathematics topics",
    "Lists of physics topics",
    "Lists of chemistry topics",
    "Lists of biology topics",
    "Lists of computer science topics",
    "Lists of engineering topics",
    "Lists of geology topics",
    "Lists of medicine topics",
    "Lists of environmental topics",
    "Lists of statistics topics",
]


# ============================================================
# 2. TARGET LANGUAGE
# ============================================================
# Human-readable language name, used directly in the Gemini translation
# prompt, e.g. "Bengali", "Hindi", "Tamil", "Telugu", "Marathi", "Kannada".
TARGET_LANGUAGE = "Bengali"


# ============================================================
# 3. ARTICLE ID RANGE
# ============================================================
# Each team member is assigned a non-overlapping range so uploads never
# collide with each other. See README.md for the full team assignment
# table. Set these ONCE to your assigned range and never change them:
# new articles are numbered automatically from the last ID already used.
START_ID = 15001
END_ID = 20000  # inclusive

# Batch size: how many articles each run of run_pipeline.py works on.
# Articles still waiting from an earlier run (not yet uploaded) count
# toward this, so unfinished work is completed before new articles are
# collected. Must be <= (END_ID - START_ID + 1).
MAX_ARTICLES = 500


# ============================================================
# 4. SCIWIKI
# ============================================================
SCIWIKI_API_URL = "https://ltrc.sciwiki.in/api.php"
SCIWIKI_USERNAME = "Digantasen"

# Contact info (an email address or a project/profile URL) sent to
# Wikipedia with every request, as Wikimedia's User-Agent policy asks.
# Scripts that include one are rate limited far less aggressively.
# Leave empty to send none.
WIKIPEDIA_CONTACT = ""


# ============================================================
# 5. ATTRIBUTION TEMPLATE
# ============================================================
# Appended to the bottom of every uploaded page. Translate these three
# lines into TARGET_LANGUAGE once when you set this file up, then leave it
# alone -- {english_title} and {english_title_url} are filled in per
# article automatically.
ATTRIBUTION_TEMPLATE = """----
''উৎস:'' ইংরেজি উইকিপিডিয়ার “{english_title}” নিবন্ধের অনুবাদ।
''মূল নিবন্ধ:'' {english_title_url}
''এই অনুবাদটি স্বয়ংক্রিয়ভাবে প্রস্তুত করা হয়েছে।''"""


# ============================================================
# 6. GEMINI MODEL
# ============================================================
GEMINI_MODEL_NAME = "gemini-3.1-flash-lite"

# How many articles 03_translate.py translates at the same time.
# 0 = one per API key (4 keys -> 4 at once).
TRANSLATE_WORKERS = 0


# ============================================================
# PATHS (no need to change)
# ============================================================
DATA_DIR = BASE_DIR / "data"
ARTICLES_CSV = DATA_DIR / "articles.csv"
COLLECT_STATE_FILE = DATA_DIR / "collect_state.json"
RAW_DIR = DATA_DIR / "raw"
TRANSLATED_DIR = DATA_DIR / "translated"


# ============================================================
# SECRETS -- loaded from .env, never hardcoded here
# ============================================================

def wikipedia_user_agent(client_name):
    # Format from https://meta.wikimedia.org/wiki/User-Agent_policy :
    #   <client>/<version> (<contact>; <description>) <library>/<version>
    import requests
    contact = WIKIPEDIA_CONTACT.strip()
    details = "IIIT Hyderabad student project"
    if contact:
        details = f"{contact}; {details}"
    return f"{client_name}/1.0 ({details}) python-requests/{requests.__version__}"


def _load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


def _load_gemini_keys():
    # GEMINI_API_KEYS may hold several keys separated by commas;
    # 03_translate.py switches to the next one when a key's quota runs out.
    # GEMINI_API_KEY (single key) is still supported.
    raw = os.environ.get("GEMINI_API_KEYS", "")
    keys = [k.strip() for k in raw.replace("\n", ",").split(",")]
    keys.append((GEMINI_API_KEY or "").strip())
    unique = []
    for k in keys:
        if k and k not in unique:
            unique.append(k)
    return unique


GEMINI_API_KEYS = _load_gemini_keys()


# ============================================================
# VALIDATION
# ============================================================

def validate():
    errors = []
    if END_ID < START_ID:
        errors.append("END_ID must be >= START_ID.")
    range_size = END_ID - START_ID + 1
    if MAX_ARTICLES > range_size:
        errors.append(
            f"MAX_ARTICLES ({MAX_ARTICLES}) exceeds your assigned ID range "
            f"size ({range_size}). Lower MAX_ARTICLES or check your "
            f"START_ID/END_ID against the team table in README.md."
        )
    if not [t for t in TOPICS if t.strip()]:
        errors.append("TOPICS is empty.")
    excluded = {t.strip().lower() for t in EXCLUDED_TOPICS}
    for t in TOPICS:
        if t.strip().lower() in excluded:
            errors.append(f"TOPICS contains an excluded topic: {t!r}")
    if not TARGET_LANGUAGE.strip():
        errors.append("TARGET_LANGUAGE is empty.")
    return errors
