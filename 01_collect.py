import csv
import json
import os
import sys
import time
import requests
from collections import deque

import config


# ============================================================
# CONFIG
# ============================================================

API_URL = "https://en.wikipedia.org/w/api.php"

TOPICS = [t.strip() for t in config.TOPICS if t.strip()]

START_ID = config.START_ID
END_ID = config.END_ID
TARGET_ARTICLES = config.MAX_ARTICLES

OUTPUT_DIR = str(config.DATA_DIR)
OUTPUT_FILE = str(config.ARTICLES_CSV)
STATE_FILE = str(config.COLLECT_STATE_FILE)

WIKI_BASE = "https://en.wikipedia.org"

CSV_HEADER = [
    "article_id",
    "english_title",
    "wikipedia_url",
    "translation_status",
    "upload_status"
]

HEADERS = {
    "User-Agent": config.wikipedia_user_agent(
        "WikipediaToSciWikiCollector"
    )
}

# Minimum gap between any two Wikipedia requests (seconds)
REQUEST_INTERVAL = 1.0

# Titles checked per request. 20 is Wikipedia's maximum when
# fetching intro extracts.
CHECK_BATCH_SIZE = 20

# Pages that are clearly list/index pages.
# We explore them but don't count them as final articles.
LIST_PREFIXES = (
    "list of ",
    "lists of ",
    "outline of "
)


# ============================================================
# SESSION
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# TITLE NORMALIZATION
# ============================================================

def canon(title):

    # Wikipedia treats "_" and " " the same, and the first letter
    # of a title is case-insensitive.
    title = title.replace("_", " ").strip()

    if not title:
        return title

    return title[0].upper() + title[1:]


EXCLUDED_KEYS = {canon(t) for t in config.EXCLUDED_TOPICS}

# Never collect these as articles (all are list/start pages anyway).
IGNORED_KEYS = (
    {"Main Page"}
    | {canon(t) for t in TOPICS}
    | EXCLUDED_KEYS
)


# ============================================================
# API REQUEST WITH RETRY
# ============================================================

_last_request_time = 0.0


def _throttle():

    global _last_request_time

    wait = _last_request_time + REQUEST_INTERVAL - time.time()

    if wait > 0:
        time.sleep(wait)

    _last_request_time = time.time()


def _retry_after_seconds(response, attempt):

    # Use the wait Wikipedia asks for, if it sent one
    header = response.headers.get("Retry-After", "")

    try:
        return max(1, int(header))
    except ValueError:
        return min(10 * 2 ** attempt, 120)


def api_request(params):

    for attempt in range(5):

        try:

            _throttle()

            response = session.get(
                API_URL,
                params=params,
                timeout=30
            )

            # Wikipedia rate limit
            if response.status_code == 429:

                wait_time = _retry_after_seconds(response, attempt)

                print(
                    f"    Rate limited by Wikipedia. "
                    f"Waiting {wait_time} seconds..."
                )

                time.sleep(wait_time)

                continue

            response.raise_for_status()

            return response.json()

        except (requests.RequestException, ValueError) as e:

            print(
                f"    Request failed: {e}"
            )

            if attempt < 4:

                wait_time = 3 * (attempt + 1)

                print(
                    f"    Retrying in "
                    f"{wait_time} seconds..."
                )

                time.sleep(wait_time)

            else:

                print(
                    "    Giving up on this request."
                )

    return None


# ============================================================
# GET LINKS FROM A WIKIPEDIA PAGE
# ============================================================

def get_links(page_title):

    # Returns a list of links, or None if the request failed
    # (so a network error is never mistaken for "no more links").

    params = {
        "action": "parse",
        "page": page_title,
        "prop": "links",
        "redirects": "1",
        "format": "json",
        "formatversion": "2"
    }

    data = api_request(params)

    if data is None:
        return None

    if "error" in data:

        print(
            "    API error:",
            data["error"]
        )

        return []

    return data.get(
        "parse",
        {}
    ).get(
        "links",
        []
    )


# ============================================================
# GET ARTICLE TEXT
# ============================================================

def get_pages_info(titles):

    # Looks up several titles in one request (following continuations).
    # Returns (pages_by_title, normalized, redirects, interwiki),
    # or None if the request failed.

    params = {
        "action": "query",
        "prop": "extracts|pageprops",
        "ppprop": "disambiguation",
        "exintro": "1",
        "explaintext": "1",
        "exlimit": "max",
        "redirects": "1",
        "titles": "|".join(titles),
        "format": "json",
        "formatversion": "2"
    }

    pages = {}
    normalized = {}
    redirects = {}
    interwiki = set()

    continuation = {}

    while True:

        data = api_request({**params, **continuation})

        if data is None:
            return None

        query = data.get("query", {})

        for item in query.get("normalized", []):
            normalized[item["from"]] = item["to"]

        for item in query.get("redirects", []):
            redirects[item["from"]] = item["to"]

        for item in query.get("interwiki", []):
            interwiki.add(item["title"])

        for page in query.get("pages", []):

            entry = pages.setdefault(page["title"], {})

            for key, value in page.items():

                if key == "pageprops":
                    entry.setdefault("pageprops", {}).update(value)

                else:
                    entry[key] = value

        if "continue" not in data:
            break

        continuation = data["continue"]

    return pages, normalized, redirects, interwiki


# ============================================================
# CHECK WHICH PAGES ARE REAL ARTICLES
# ============================================================

def check_articles(titles):

    # Returns {title: (status, canonical_title)} where status is:
    #   "good"  -> real article; canonical_title has redirects resolved
    #   "bad"   -> definitely not usable (cached, never rechecked)
    #   "error" -> request failed; may be retried in a later run

    info = get_pages_info(titles)

    if info is None:
        return {t: ("error", None) for t in titles}

    pages, normalized, redirects, interwiki = info

    results = {}

    for title in titles:

        # Follow normalization and redirects to the final page
        name = normalized.get(title, title)

        # Link to another wiki (e.g. Wiktionary)
        if title in interwiki or name in interwiki:

            results[title] = ("bad", None)

            continue

        hops = set()

        while name in redirects and name not in hops:

            hops.add(name)

            name = redirects[name]

        page = pages.get(name)

        if page is None:

            results[title] = ("error", None)

            continue

        results[title] = judge_page(page, name)

    return results


def judge_page(page, page_title):

    # Page does not exist
    if page.get("missing") or page.get("invalid"):
        return "bad", None

    # Disambiguation pages aren't articles
    if "disambiguation" in page.get("pageprops", {}):
        return "bad", None

    canonical = canon(page.get("title", page_title))

    if canonical in IGNORED_KEYS:
        return "bad", None

    # A redirect to a list page
    if canonical.lower().startswith(LIST_PREFIXES):
        return "bad", None

    text = page.get(
        "extract",
        ""
    ).strip()

    # Require meaningful textual content
    if len(text) < 300:
        return "bad", None

    return "good", canonical


# ============================================================
# CREATE WIKIPEDIA URL
# ============================================================

def make_wikipedia_url(title):

    return (
        WIKI_BASE
        + "/wiki/"
        + title.replace(" ", "_")
    )


# ============================================================
# CSV (APPEND-ONLY LEDGER OF EVERY ARTICLE EVER COLLECTED)
# ============================================================

def read_existing_rows():

    if not os.path.exists(OUTPUT_FILE):
        return []

    with open(
        OUTPUT_FILE,
        "r",
        newline="",
        encoding="utf-8"
    ) as file:

        return list(
            csv.DictReader(file)
        )


def append_article(article_id, title):

    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True
    )

    is_new = (
        not os.path.exists(OUTPUT_FILE)
        or os.path.getsize(OUTPUT_FILE) == 0
    )

    with open(
        OUTPUT_FILE,
        "a",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.writer(file)

        if is_new:
            writer.writerow(CSV_HEADER)

        writer.writerow([
            article_id,
            title,
            make_wikipedia_url(title),
            "pending",
            "pending"
        ])


# ============================================================
# STATE (PERSISTS ACROSS RUNS)
# ============================================================
#
#   next_topic_index : which entry of config.TOPICS the next run starts at
#   last_id          : highest article ID ever assigned
#   collected        : every English title ever collected (dedup history,
#                      kept even if articles.csv is edited or deleted)
#   skipped          : titles already checked and rejected
#   exhausted_topics : topics that have no new articles left

def load_state():

    state = {
        "next_topic_index": 0,
        "last_id": None,
        "collected": [],
        "skipped": [],
        "exhausted_topics": []
    }

    if os.path.exists(STATE_FILE):

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            state.update(
                json.load(file)
            )

    return state


def save_state(state):

    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True
    )

    tmp_file = STATE_FILE + ".tmp"

    with open(
        tmp_file,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=1
        )

    os.replace(tmp_file, STATE_FILE)


# ============================================================
# COLLECT ARTICLES FROM ONE TOPIC
# ============================================================

def collect_from_topic(topic, wanted, ctx):

    # ctx holds the shared run state:
    #   seen, skipped (sets of canonical titles), next_id, state, new

    queue = deque()

    queue.append(topic)

    visited_pages = set()

    found = 0

    had_errors = False

    while queue and found < wanted:

        current_page = queue.popleft()

        if current_page in visited_pages:
            continue

        visited_pages.add(current_page)

        print()
        print("-" * 60)
        print(
            f"Exploring: {current_page}"
        )

        links = get_links(current_page)

        if links is None:

            had_errors = True

            continue

        print(
            f"Found {len(links)} links"
        )

        # ----------------------------------------------------
        # Sort links: list/index pages are explored later,
        # everything else is a candidate article.
        # ----------------------------------------------------

        candidates = []

        candidate_keys = set()

        for link in links:

            title = link.get("title")

            if not title:
                continue

            # Only main namespace pages
            if link.get("ns", 0) != 0:
                continue

            # Red links
            if link.get("exists") is False:
                continue

            key = canon(title)

            if key in visited_pages or key in candidate_keys:
                continue

            if key in ctx["seen"] or key in ctx["skipped"]:
                continue

            if key in IGNORED_KEYS:
                continue

            # Do not count obvious list/index pages, but explore
            # them because they may contain individual articles.
            if key.lower().startswith(LIST_PREFIXES):

                queue.append(key)

                continue

            candidates.append(key)

            candidate_keys.add(key)

        # ----------------------------------------------------
        # Check candidates in batches (one request per batch)
        # ----------------------------------------------------

        for start in range(0, len(candidates), CHECK_BATCH_SIZE):

            if found >= wanted:
                break

            batch = candidates[start:start + CHECK_BATCH_SIZE]

            print(
                f"Checking {len(batch)} pages "
                f"({start + len(batch)}/{len(candidates)})..."
            )

            results = check_articles(batch)

            for key in batch:

                status, canonical = results[key]

                if status == "error":

                    had_errors = True

                    print(
                        f"  ✗ {key}: request failed — "
                        "will retry in a later run"
                    )

                    continue

                if status == "bad":

                    ctx["skipped"].add(key)

                    continue

                # Link was a redirect to an article we already have
                if canonical in ctx["seen"]:

                    ctx["skipped"].add(key)

                    print(
                        f"  ✗ {key}: already collected as {canonical}"
                    )

                    continue

                # --------------------------------------------
                # New article: save immediately so an
                # interrupted run never loses work or
                # reuses an ID.
                # --------------------------------------------

                article_id = ctx["next_id"]

                append_article(article_id, canonical)

                ctx["seen"].add(canonical)
                ctx["next_id"] += 1
                ctx["new"].append((article_id, canonical, topic))

                state = ctx["state"]
                state["last_id"] = article_id
                state["collected"].append(canonical)
                save_state(state)

                found += 1

                print(
                    f"  ✓ ARTICLE {found}/{wanted} "
                    f"(ID {article_id}): {canonical}"
                )

                if found >= wanted:
                    break

    # The topic is only "used up" if we walked everything reachable
    # from it without any request failing along the way.
    exhausted = (
        found < wanted
        and not queue
        and not had_errors
    )

    return found, exhausted


# ============================================================
# MAIN
# ============================================================

def main():

    errors = config.validate()

    if errors:

        print("CONFIG ERROR:")

        for e in errors:
            print(f"  - {e}")

        sys.exit(1)

    print("=" * 60)
    print("WIKIPEDIA SCIENTIFIC ARTICLE COLLECTOR")
    print("=" * 60)

    state = load_state()

    rows = read_existing_rows()

    # --------------------------------------------------------
    # Every title ever collected — never collect it again
    # --------------------------------------------------------

    seen = {canon(t) for t in state["collected"]}

    seen |= {
        canon(row["english_title"])
        for row in rows
        if row.get("english_title")
    }

    skipped = {canon(t) for t in state["skipped"]}

    # Make sure titles from older CSVs are in the history too
    state["collected"] = sorted(seen)

    # --------------------------------------------------------
    # Continue from the next unused ID
    # --------------------------------------------------------

    used_ids = [START_ID - 1]

    for row in rows:

        try:
            used_ids.append(int(row["article_id"]))
        except (KeyError, TypeError, ValueError):
            pass

    if state["last_id"] is not None:
        used_ids.append(int(state["last_id"]))

    next_id = max(used_ids) + 1

    remaining_ids = END_ID - next_id + 1

    # --------------------------------------------------------
    # Finish earlier work first: articles collected before but
    # not uploaded yet count toward this run's batch.
    # --------------------------------------------------------

    backlog = sum(
        1 for row in rows
        if row.get("upload_status") != "uploaded"
    )

    wanted = min(TARGET_ARTICLES - backlog, remaining_ids)

    print(f"ID range        : {START_ID}-{END_ID}")
    print(f"Already have    : {len(seen)} articles")
    print(f"Not uploaded yet: {backlog}")
    print(f"Next article ID : {next_id}")
    print(f"New articles    : {max(wanted, 0)}")

    if remaining_ids <= 0:

        print()
        print(
            f"Your ID range is full (last ID used: {next_id - 1}). "
            "Nothing new to collect."
        )

        return

    if wanted <= 0:

        print()
        print(
            f"{backlog} earlier articles are still waiting to be "
            "translated/uploaded; finishing those before collecting "
            "new ones."
        )

        return

    # --------------------------------------------------------
    # Rotate through topics, starting after last run's topic
    # --------------------------------------------------------

    exhausted_topics = set(state["exhausted_topics"])

    start_index = state["next_topic_index"] % len(TOPICS)

    print(f"Starting topic  : {TOPICS[start_index]}")
    print()

    ctx = {
        "seen": seen,
        "skipped": skipped,
        "next_id": next_id,
        "state": state,
        "new": []
    }

    interrupted = False

    try:

        for offset in range(len(TOPICS)):

            collected_so_far = len(ctx["new"])

            if collected_so_far >= wanted:
                break

            topic_index = (start_index + offset) % len(TOPICS)

            topic = TOPICS[topic_index]

            if topic in exhausted_topics:

                print(f"Skipping used-up topic: {topic}")

                continue

            # If this run is interrupted, the next run resumes here
            state["next_topic_index"] = topic_index

            print()
            print("=" * 60)
            print(f"TOPIC: {topic}")
            print("=" * 60)

            found, exhausted = collect_from_topic(
                topic,
                wanted - collected_so_far,
                ctx
            )

            print()
            print(f"Collected {found} new article(s) from {topic}")

            if exhausted:

                print(f"No new articles left in {topic}")

                exhausted_topics.add(topic)

            # Next run starts at the following topic
            state["next_topic_index"] = (topic_index + 1) % len(TOPICS)

    except KeyboardInterrupt:

        interrupted = True

        print()
        print("Stopped by user. Collected articles are saved;")
        print("run again to continue.")

    finally:

        state["skipped"] = sorted(ctx["skipped"])
        state["exhausted_topics"] = sorted(exhausted_topics)

        save_state(state)

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    new_articles = ctx["new"]

    print()
    print("=" * 60)

    print(
        f"NEW ARTICLES COLLECTED: "
        f"{len(new_articles)}"
    )

    if not interrupted and len(new_articles) < wanted:

        print()
        print(
            f"WARNING: Fewer than {wanted} new articles "
            "were found."
        )

    print()
    print(
        f"✓ Appended to:"
    )

    print(
        f"  {OUTPUT_FILE}"
    )

    print()
    print("Collected articles:")

    for article_id, title, topic in new_articles:

        print(
            f"{article_id} | {title}  [{topic}]"
        )

    print()
    print(
        f"Next run starts at topic: "
        f"{TOPICS[state['next_topic_index'] % len(TOPICS)]}"
    )

    print()
    print("=" * 60)
    print("COLLECTION FINISHED")
    print("=" * 60)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
