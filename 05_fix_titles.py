from pathlib import Path
import pandas as pd
import re

import config


# ============================================================
# CONFIG
# ============================================================
#
# This is an OPTIONAL cleanup utility. 03_translate.py already generates a
# title for every article as part of translation (see {id}_title.txt); you
# only need this script if you spot a specific article with a broken title
# (e.g. an infobox field, an HTML comment, a caption) and want to fix it
# from the raw article text without re-running Gemini.
# ============================================================

CSV_FILE = config.ARTICLES_CSV
TRANSLATED_DIR = config.TRANSLATED_DIR


# ============================================================
# MANUAL TITLE OVERRIDES
# ============================================================
#
# Article IDs where automatic extraction is unreliable, mapped to the
# correct title. This is specific to YOUR dataset -- add entries here as you
# spot bad titles for your own article IDs.
# ============================================================

MANUAL_TITLES = {
    "10035": "IAU designated constellations",
    "10036": "Abell 133",

    "10087": "মিল্কিওয়ের উপগ্রহ গ্যালাক্সি",
    "10091": "HATNet প্রকল্প",
    "10093": "কোরোট",
    "10096": "আরসিডব্লিউ ক্যাটালগ",
}


# ============================================================
# HELPER
# ============================================================

def contains_target_script(text):
    """True if `text` has any non-ASCII character -- i.e. it looks like it
    was actually translated, rather than being leftover English/markup.
    Works for any target language, not just Bengali."""

    return any(ord(ch) > 127 for ch in text)


# ============================================================
# CLEAN TITLE
# ============================================================

def clean_title(title):

    if not title:
        return ""

    title = title.strip()

    # Remove bold markup
    title = title.replace("'''", "")
    title = title.replace("''", "")

    # [[English|Bengali]]
    match = re.search(
        r"\[\[[^|\]]+\|([^\]]+)\]\]",
        title
    )

    if match:
        title = match.group(1)

    # [[Bengali]]
    title = title.replace("[[", "")
    title = title.replace("]]", "")

    # Remove unwanted surrounding characters
    title = title.strip(
        " |,:;-=*\"'“”‘’"
    )

    return title.strip()


# ============================================================
# EXTRACT TITLE FROM INFOBOX
# ============================================================

def extract_from_name_field(text):

    # Only inspect beginning of article
    lines = text.splitlines()[:200]

    for line in lines:

        match = re.match(
            r"\s*\|\s*name\s*=\s*(.*)",
            line,
            flags=re.IGNORECASE
        )

        if not match:
            continue

        value = match.group(1).strip()

        # ----------------------------------------------------
        # Example:
        #
        # [[Abell catalogue|অ্যাবেল]] ৩৭০
        # ----------------------------------------------------

        match2 = re.search(
            r"\[\[[^|\]]+\|([^\]]+)\]\]",
            value
        )

        if match2:

            title = match2.group(1)

            remaining = value[
                match2.end():
            ]

            title += remaining

            title = clean_title(title)

            if contains_target_script(title):

                return title

        # ----------------------------------------------------
        # Example:
        #
        # | name = ক্রেটার
        # ----------------------------------------------------

        title = clean_title(value)

        if contains_target_script(title):

            return title

    return None


# ============================================================
# EXTRACT FIRST BOLD TITLE
# ============================================================

def extract_from_bold(text):

    # Skip initial infobox/template area
    closing = text.find("}}")

    if closing != -1:
        body = text[closing + 2:]
    else:
        body = text

    matches = re.findall(
        r"'''(.*?)'''",
        body,
        flags=re.DOTALL
    )

    for match in matches:

        title = clean_title(match)

        if (
            contains_target_script(title)
            and len(title) <= 150
        ):

            return title

    return None


# ============================================================
# MAIN
# ============================================================

def main():

    df = pd.read_csv(
        CSV_FILE,
        dtype={"article_id": str}
    )

    print("=" * 70)
    print("FIXING BENGALI TITLES")
    print("=" * 70)

    success = 0
    failed = 0

    for _, row in df.iterrows():

        article_id = str(
            row["article_id"]
        )

        english_title = str(
            row["english_title"]
        )

        translated_file = (
            TRANSLATED_DIR /
            f"{article_id}.txt"
        )

        title_file = (
            TRANSLATED_DIR /
            f"{article_id}_title.txt"
        )

        print(
            f"\n[{article_id}] "
            f"{english_title}"
        )

        # ----------------------------------------------------
        # If title file already exists
        # ----------------------------------------------------

        if title_file.exists():

            title = title_file.read_text(
                encoding="utf-8"
            ).strip()

            if title:

                print(
                    f"Already exists: {title}"
                )

                success += 1
                continue

        # ----------------------------------------------------
        # Manual title
        # ----------------------------------------------------

        if article_id in MANUAL_TITLES:

            title = MANUAL_TITLES[
                article_id
            ]

            print(
                f"Manual title: {title}"
            )

        else:

            # ------------------------------------------------
            # Check translated article
            # ------------------------------------------------

            if not translated_file.exists():

                print(
                    "ERROR: Translation file "
                    "not found."
                )

                failed += 1
                continue

            text = translated_file.read_text(
                encoding="utf-8"
            )

            # ------------------------------------------------
            # Try infobox
            # ------------------------------------------------

            title = extract_from_name_field(
                text
            )

            # ------------------------------------------------
            # Try first bold title
            # ------------------------------------------------

            if not title:

                title = extract_from_bold(
                    text
                )

            # ------------------------------------------------
            # Failed
            # ------------------------------------------------

            if not title:

                print(
                    "WARNING: Could not determine "
                    "title."
                )

                print(
                    "Will need manual title."
                )

                failed += 1
                continue

            print(
                f"Extracted title: {title}"
            )

        # ----------------------------------------------------
        # Save title
        # ----------------------------------------------------

        title_file.write_text(
            title,
            encoding="utf-8"
        )

        print(
            f"Saved: {title_file.name}"
        )

        success += 1

    # ========================================================
    # SUMMARY
    # ========================================================

    print("\n" + "=" * 70)
    print("TITLE FIX FINISHED")
    print("=" * 70)

    print(
        f"Successful : {success}"
    )

    print(
        f"Failed     : {failed}"
    )

    print(
        f"Output     : {TRANSLATED_DIR}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()