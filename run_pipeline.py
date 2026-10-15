"""
run_pipeline.py

Runs steps 1-3 of the pipeline in one go. Just run it whenever you like --
nothing needs to be edited between runs:

    01_collect.py   -> collect new articles (only enough to fill a batch
                       of MAX_ARTICLES, counting unfinished earlier ones)
    02_download.py  -> download anything not yet downloaded
    03_translate.py -> translate anything not yet translated

Every step works out what is already done from the files in data/ and the
upload_status column of data/articles.csv, and continues from there, so an
interrupted or quota-limited run is simply finished by the next one.

Uploading is deliberately NOT part of this: run 04_upload.py yourself when
you want to publish. It uploads every translated article that isn't
uploaded yet, continuing from where it last stopped.
"""

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

STEPS = [
    "01_collect.py",
    "02_download.py",
    "03_translate.py",
]


def main():

    results = {}

    for step in STEPS:

        print()
        print("#" * 70)
        print(f"# RUNNING {step}")
        print("#" * 70)
        print(flush=True)

        try:

            result = subprocess.run(
                [sys.executable, str(BASE_DIR / step)],
                cwd=BASE_DIR
            )

        except KeyboardInterrupt:

            print(f"\nStopped by user during {step}.")
            print("Run this script again to continue.")

            sys.exit(130)

        results[step] = result.returncode

        # Keep going: e.g. if collecting fails (Wikipedia down), the
        # articles already collected can still be downloaded and translated.
        if result.returncode != 0:
            print(f"\n{step} failed (exit code {result.returncode}); continuing.")

    print()
    print("#" * 70)
    print("# STEPS 1-3 FINISHED")

    for step, code in results.items():
        print(f"#   {step:16} {'ok' if code == 0 else f'FAILED (exit code {code})'}")

    print("#")
    print("# To publish: python 04_upload.py")
    print("# Run this script again any time to continue with the next batch.")
    print("#" * 70)

    sys.exit(0 if all(code == 0 for code in results.values()) else 1)


if __name__ == "__main__":
    main()
