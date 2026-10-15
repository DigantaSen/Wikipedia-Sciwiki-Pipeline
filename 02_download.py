import csv
import os
import time
import requests

import config


# ============================================================
# CONFIG
# ============================================================

API_URL = "https://en.wikipedia.org/w/api.php"

CSV_FILE = str(config.ARTICLES_CSV)
OUTPUT_DIR = str(config.RAW_DIR)

HEADERS = {
    "User-Agent": config.wikipedia_user_agent(
        "WikipediaToSciWikiDownloader"
    )
}


# ============================================================
# SESSION
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# API REQUEST WITH RETRY
# ============================================================

def api_request(params):

    for attempt in range(5):

        try:

            response = session.get(
                API_URL,
                params=params,
                timeout=30
            )

            if response.status_code == 429:

                # Use the wait Wikipedia asks for, if it sent one
                try:
                    wait_time = max(
                        1, int(response.headers.get("Retry-After", ""))
                    )
                except ValueError:
                    wait_time = min(10 * 2 ** attempt, 120)

                print(
                    f"    Rate limited. "
                    f"Waiting {wait_time}s..."
                )

                time.sleep(wait_time)
                continue

            response.raise_for_status()

            return response.json()

        except requests.RequestException as e:

            print(f"    Request error: {e}")

            if attempt < 4:

                time.sleep(
                    3 * (attempt + 1)
                )

            else:

                return None

    return None


# ============================================================
# DOWNLOAD ARTICLE CONTENT
# ============================================================

def download_article(title):

    params = {
        "action": "query",
        "prop": "revisions",
        "titles": title,
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
        "formatversion": "2"
    }

    data = api_request(params)

    if data is None:
        return None

    pages = data.get(
        "query",
        {}
    ).get(
        "pages",
        []
    )

    if not pages:
        return None

    page = pages[0]

    if page.get("missing"):
        return None

    revisions = page.get(
        "revisions",
        []
    )

    if not revisions:
        return None

    content = revisions[0].get(
        "slots",
        {}
    ).get(
        "main",
        {}
    ).get(
        "content",
        ""
    )

    return content


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("WIKIPEDIA ARTICLE DOWNLOADER")
    print("=" * 60)

    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True
    )

    # --------------------------------------------------------
    # Read CSV
    # --------------------------------------------------------

    with open(
        CSV_FILE,
        "r",
        encoding="utf-8"
    ) as file:

        articles = list(
            csv.DictReader(file)
        )

    print(
        f"\nFound {len(articles)} articles in CSV."
    )

    successful = 0
    failed = 0

    # --------------------------------------------------------
    # Download each article
    # --------------------------------------------------------

    for index, article in enumerate(articles, start=1):

        article_id = article["article_id"]
        title = article["english_title"]

        output_file = os.path.join(
            OUTPUT_DIR,
            f"{article_id}.txt"
        )

        print()
        print(
            f"[{index}/{len(articles)}] "
            f"{article_id} | {title}"
        )

        # ----------------------------------------------------
        # Skip if already downloaded
        # ----------------------------------------------------

        if os.path.exists(output_file):

            print(
                "    ✓ Already downloaded — skipping"
            )

            successful += 1
            continue

        # ----------------------------------------------------
        # Download
        # ----------------------------------------------------

        content = download_article(title)

        if content is None:

            print(
                "    ✗ Download failed"
            )

            failed += 1

            continue

        # ----------------------------------------------------
        # Save
        # ----------------------------------------------------

        with open(
            output_file,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(content)

        print(
            f"    ✓ Saved "
            f"({len(content):,} characters)"
        )

        successful += 1

        # Be polite to Wikipedia
        time.sleep(1)

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("DOWNLOAD FINISHED")
    print("=" * 60)

    print(
        f"Successful : {successful}"
    )

    print(
        f"Failed     : {failed}"
    )

    print(
        f"Files saved: {OUTPUT_DIR}/"
    )


if __name__ == "__main__":
    main()