import requests
import getpass
import csv
import json
import os
import sys
import time
from pathlib import Path

import config
from sanitize import sanitize_for_sciwiki, find_image_names, MAX_IMAGES_PER_PAGE


# ============================================================
# CONFIG
# ============================================================

API_URL = config.SCIWIKI_API_URL

USERNAME = config.SCIWIKI_USERNAME

# IMPORTANT:
# Keep True for the first run.
# Change to False only after checking the dry-run output.
DRY_RUN = False

ARTICLES_CSV = config.ARTICLES_CSV
TRANSLATED_DIR = config.TRANSLATED_DIR
RAW_DIR = config.RAW_DIR

# TEST_MODE restricts processing to only the article IDs listed in
# TEST_ARTICLES. Use this to safely verify the
# sanitizer on a few representative pages (re-uploading updates the existing
# page in place, it does not create duplicates) before running the full batch.
TEST_MODE = False
TEST_ARTICLES = ["15001", "15002", "15003"]


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "WikipediaToSciWikiUploader/1.0"
})


# ============================================================
# LOGIN
# ============================================================

def get_login_token():

    response = session.get(
        API_URL,
        params={
            "action": "query",
            "meta": "tokens",
            "type": "login",
            "format": "json"
        }
    )

    response.raise_for_status()

    data = response.json()

    return data["query"]["tokens"]["logintoken"]


def login(password):

    login_token = get_login_token()

    response = session.post(
        API_URL,
        data={
            "action": "clientlogin",
            "username": USERNAME,
            "password": password,
            "loginreturnurl": "https://ltrc.sciwiki.in/",
            "logintoken": login_token,
            "format": "json"
        }
    )

    response.raise_for_status()

    data = response.json()

    if data.get("clientlogin", {}).get("status") != "PASS":

        print("\nLOGIN FAILED")
        print(data)

        return False

    print("\nLOGIN SUCCESSFUL")

    return True


# ============================================================
# CSRF TOKEN
# ============================================================

def get_csrf_token():

    response = session.get(
        API_URL,
        params={
            "action": "query",
            "meta": "tokens",
            "format": "json"
        }
    )

    response.raise_for_status()

    data = response.json()

    return data["query"]["tokens"]["csrftoken"]


# ============================================================
# READ BENGALI TITLE
# ============================================================

def get_bengali_title(article_id):

    title_file = (
        TRANSLATED_DIR /
        f"{article_id}_title.txt"
    )

    if not title_file.exists():

        print(
            f"ERROR: Translated title file not found:"
        )

        print(title_file)

        return None

    title = title_file.read_text(
        encoding="utf-8"
    ).strip()

    if not title:

        print(
            f"ERROR: Translated title file is empty:"
        )

        print(title_file)

        return None

    return title


# ============================================================
# BUILD SCIWIKI CONTENT
# ============================================================

def build_content(
    translated_text,
    english_title
):

    attribution = config.ATTRIBUTION_TEMPLATE.format(
        english_title=english_title,
        english_title_url=(
            "https://en.wikipedia.org/wiki/"
            + english_title.replace(" ", "_")
        )
    )

    return (
        translated_text.rstrip()
        + "\n\n"
        + attribution.strip()
    )


# ============================================================
# UPLOAD ONE ARTICLE
# ============================================================

# Long runs can outlive the login session; these mean "log in again".
SESSION_ERRORS = {
    "assertuserfailed", "assertnameduserfailed", "badtoken", "notloggedin",
}


class SessionExpired(Exception):
    pass


def upload_article(
    csrf_token,
    bengali_title,
    content,
    english_title
):

    response = session.post(
        API_URL,
        data={
            "action": "edit",

            # THIS IS THE ACTUAL SCIWIKI PAGE TITLE
            "title": bengali_title,

            # Bengali translated article
            "text": content,

            "summary": (
                f"ইংরেজি উইকিপিডিয়ার "
                f"“{english_title}” নিবন্ধের "
                f"বাংলা অনুবাদ"
            ),

            "token": csrf_token,

            # Refuse the edit if we're no longer logged in as USERNAME,
            # instead of silently saving it under our IP address.
            "assert": "user",
            "assertuser": USERNAME,

            "format": "json"
        }
    )

    response.raise_for_status()

    data = response.json()

    if data.get("error", {}).get("code") in SESSION_ERRORS:
        raise SessionExpired(data["error"]["code"])

    if "error" in data:

        print("\nUPLOAD ERROR:")
        print(data)

        return False

    if data.get("edit", {}).get("result") == "Success":

        return True

    print("\nUNKNOWN RESPONSE:")
    print(data)

    return False


# ============================================================
# IMAGES: WHICH FILES EXIST ON WIKIMEDIA COMMONS
# ============================================================
#
# SciWiki shows Commons images, but a file that isn't on Commons would be a
# red link, so the sanitizer keeps only files confirmed here. Results are
# cached in data/commons_files.json so each file is looked up once.

COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"

COMMONS_CACHE_FILE = config.DATA_DIR / "commons_files.json"


class CommonsFiles:

    def __init__(self):

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": config.wikipedia_user_agent(
                "WikipediaToSciWikiUploader"
            )
        })

        try:
            self.known = json.loads(
                COMMONS_CACHE_FILE.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            self.known = {}

    def exists(self, name):

        return self.known.get(name, False)

    def check(self, names):

        todo = sorted(n for n in names if n not in self.known)

        for start in range(0, len(todo), 50):

            batch = todo[start:start + 50]

            data = self._query(batch)

            if data is None:
                # Unknown: treat as missing this time, ask again next run
                continue

            query = data.get("query", {})

            normalized = {
                item["from"]: item["to"]
                for item in query.get("normalized", [])
            }

            redirects = {
                item["from"]: item["to"]
                for item in query.get("redirects", [])
            }

            pages = {
                page["title"]: page
                for page in query.get("pages", [])
            }

            for name in batch:

                title = normalized.get(f"File:{name}", f"File:{name}")
                title = redirects.get(title, title)

                page = pages.get(title, {})

                self.known[name] = bool(
                    page
                    and not page.get("missing")
                    and not page.get("invalid")
                )

        if todo:

            COMMONS_CACHE_FILE.write_text(
                json.dumps(self.known, ensure_ascii=False, indent=0),
                encoding="utf-8"
            )

    def _query(self, batch):

        for attempt in range(4):

            try:

                response = self.session.post(
                    COMMONS_API_URL,
                    data={
                        "action": "query",
                        "titles": "|".join(f"File:{n}" for n in batch),
                        "redirects": "1",
                        "format": "json",
                        "formatversion": "2"
                    },
                    timeout=60
                )

                if response.status_code == 429:

                    time.sleep(10 * (attempt + 1))

                    continue

                response.raise_for_status()

                return response.json()

            except (requests.RequestException, ValueError) as e:

                print(f"Commons lookup failed: {e}")

                time.sleep(5 * (attempt + 1))

        return None


# ============================================================
# LINKS: ENGLISH TITLE -> OUR SCIWIKI PAGE
# ============================================================

def is_translated(article_id):

    return (
        (TRANSLATED_DIR / f"{article_id}.txt").exists()
        and (TRANSLATED_DIR / f"{article_id}_title.txt").exists()
    )


def build_local_pages(rows):

    # Links to an article we translated go to our own SciWiki page
    # instead of English Wikipedia.

    local = {}

    for row in rows:

        title_file = TRANSLATED_DIR / f"{row['article_id']}_title.txt"

        if not title_file.exists():
            continue

        bengali_title = title_file.read_text(encoding="utf-8").strip()

        if bengali_title:
            local[row["english_title"]] = bengali_title

    return local


# ============================================================
# SAVE CSV (records upload_status so the next run moves on)
# ============================================================

def save_csv(fieldnames, rows):

    tmp_file = ARTICLES_CSV.with_suffix(".csv.tmp")

    with open(
        tmp_file,
        "w",
        encoding="utf-8",
        newline=""
    ) as f:

        writer = csv.DictWriter(f, fieldnames=fieldnames)

        writer.writeheader()

        writer.writerows(rows)

    tmp_file.replace(ARTICLES_CSV)


# ============================================================
# MAIN
# ============================================================

def main():

    errors = config.validate()

    if errors:

        print("CONFIG ERROR:")

        for e in errors:
            print(f"  - {e}")

        return

    print("=" * 70)
    print("SCIWIKI UPLOADER")
    print("=" * 70)

    print(f"\nTarget language : {config.TARGET_LANGUAGE}")
    print(f"SciWiki user    : {USERNAME}")
    print(f"DRY_RUN         = {DRY_RUN}")

    if DRY_RUN:

        print(
            "No articles will be uploaded."
        )

    else:

        print(
            "WARNING: Articles WILL be uploaded."
        )


    # ========================================================
    # READ CSV
    # ========================================================

    if not ARTICLES_CSV.exists():

        print(
            f"\nERROR: CSV not found:"
        )

        print(
            ARTICLES_CSV
        )

        return


    with open(
        ARTICLES_CSV,
        "r",
        encoding="utf-8",
        newline=""
    ) as f:

        reader = csv.DictReader(f)

        fieldnames = reader.fieldnames

        all_rows = list(reader)


    # ========================================================
    # LIMIT TO TEST ARTICLES OR NEXT N NOT YET UPLOADED
    # ========================================================

    if TEST_MODE:

        wanted = set(TEST_ARTICLES)

        rows = [
            row for row in all_rows
            if str(row["article_id"]) in wanted
        ]

        print(
            f"\nTEST_MODE enabled — restricting to: {TEST_ARTICLES}"
        )

    else:

        not_uploaded = [
            row for row in all_rows
            if row.get("upload_status") != "uploaded"
        ]

        # Every translated article that isn't uploaded yet, in ID order,
        # so each run carries on from where the last one stopped. The
        # rest (still being translated) are picked up by a later run.
        rows = [
            row for row in not_uploaded
            if is_translated(row["article_id"])
        ]

        print(
            f"\nAlready uploaded   : {len(all_rows) - len(not_uploaded)}"
        )

        if rows:
            print(
                f"Resuming from ID   : {rows[0]['article_id']}"
            )

        waiting = len(not_uploaded) - len(rows)

        if waiting:

            print(
                f"Waiting for translation: {waiting} "
                f"(uploaded by a later run)"
            )

    print(
        f"\nArticles to process: {len(rows)}"
    )

    if not rows:

        print("Nothing to upload.")

        return


    # ========================================================
    # LOGIN
    # ========================================================

    if not DRY_RUN:

        # run_pipeline.py asks once at the start and passes it here
        # in memory; it is never written anywhere.
        password = os.environ.get("SCIWIKI_PASSWORD") or getpass.getpass(
            "SciWiki password: "
        )

        if not login(password):

            sys.exit(1)

        csrf_token = get_csrf_token()

        print(
            "CSRF token obtained."
        )

    else:

        csrf_token = None


    # ========================================================
    # PROCESS ARTICLES
    # ========================================================

    commons = CommonsFiles()

    local_pages = build_local_pages(all_rows)

    successful = 0
    failed = 0

    try:

        for index, row in enumerate(
            rows,
            start=1
        ):

            article_id = str(
                row["article_id"]
            )

            english_title = str(
                row["english_title"]
            )


            print(
                "\n" + "=" * 70
            )

            print(
                f"[{index}/{len(rows)}] "
                f"{article_id} - {english_title}"
            )


            # ====================================================
            # CHECK TRANSLATED ARTICLE
            # ====================================================

            translated_file = (
                TRANSLATED_DIR /
                f"{article_id}.txt"
            )

            if not translated_file.exists():

                print(
                    "ERROR: Translation file not found:"
                )

                print(
                    translated_file
                )

                failed += 1

                continue


            # ====================================================
            # CHECK BENGALI TITLE
            # ====================================================

            bengali_title = get_bengali_title(
                article_id
            )

            if bengali_title is None:

                failed += 1

                continue


            print(
                f"English title : {english_title}"
            )

            print(
                f"Translated title : {bengali_title}"
            )

            print(
                f"SCIWIKI PAGE  : {bengali_title}"
            )


            # ====================================================
            # READ TRANSLATED ARTICLE
            # ====================================================

            try:

                translated_text = (
                    translated_file.read_text(
                        encoding="utf-8"
                    )
                )

            except Exception as e:

                print(
                    f"ERROR reading article: {e}"
                )

                failed += 1

                continue


            if not translated_text.strip():

                print(
                    "ERROR: Translated article is empty."
                )

                failed += 1

                continue


            # ====================================================
            # SANITIZE + BUILD CONTENT
            # ====================================================

            raw_file = (
                RAW_DIR /
                f"{article_id}.txt"
            )

            raw_text = (
                raw_file.read_text(encoding="utf-8")
                if raw_file.exists()
                else None
            )

            image_names = find_image_names(translated_text)

            commons.check(image_names)

            clean_text = sanitize_for_sciwiki(
                translated_text,
                raw_text=raw_text,
                file_ok=commons.exists,
                local_pages=local_pages
            )

            kept_images = sum(
                1 for name in image_names
                if commons.exists(name)
            )

            print(
                f"Images        : {kept_images} of "
                f"{len(image_names)} found on Commons, "
                f"{min(kept_images, MAX_IMAGES_PER_PAGE)} used"
            )

            content = build_content(
                clean_text,
                english_title
            )


            # ====================================================
            # DRY RUN
            # ====================================================

            if DRY_RUN:

                print(
                    "ACTION        : "
                    "DRY RUN - NOT UPLOADED"
                )

                successful += 1

                continue


            # ====================================================
            # ACTUAL UPLOAD
            # ====================================================

            try:

                try:

                    result = upload_article(
                        csrf_token,
                        bengali_title,
                        content,
                        english_title
                    )

                except SessionExpired as e:

                    print(
                        f"Login expired ({e}). Logging in again..."
                    )

                    if not login(password):
                        sys.exit(1)

                    csrf_token = get_csrf_token()

                    result = upload_article(
                        csrf_token,
                        bengali_title,
                        content,
                        english_title
                    )

                if result:

                    print(
                        "ACTION        : UPLOADED"
                    )

                    successful += 1

                    row["upload_status"] = "uploaded"

                    save_csv(fieldnames, all_rows)

                else:

                    print(
                        "ACTION        : FAILED"
                    )

                    failed += 1

            except Exception as e:

                print(
                    f"UPLOAD EXCEPTION: {e}"
                )

                failed += 1


            # ====================================================
            # SMALL DELAY
            # ====================================================

            time.sleep(1)

    except KeyboardInterrupt:

        print(
            "\n\nStopped by user. Everything uploaded so far is "
            "saved; run again to continue from the next article."
        )


    # ========================================================
    # SUMMARY
    # ========================================================

    print(
        "\n" + "=" * 70
    )

    print(
        "PROCESS FINISHED"
    )

    print(
        f"Successful : {successful}"
    )

    print(
        f"Failed     : {failed}"
    )

    print(
        "=" * 70
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()