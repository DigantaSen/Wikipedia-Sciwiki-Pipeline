# Wikipedia → SciWiki Translation Pipeline

Collects Wikipedia articles under a chosen topic, translates them into a
regional language with Gemini, cleans up the wikitext so it renders
correctly on a SciWiki MediaWiki install (no red-linked templates, no
broken citations, no leftover image markup), and uploads them.

Each team member runs their own copy of this pipeline against their own
topic, their own target language, and their own assigned article ID range.

## Pipeline

```
01_collect.py    Find N substantive articles under your chosen topic
       |         -> data/articles.csv
       v
02_download.py   Download the raw English wikitext for each article
       |         -> data/raw/{id}.txt
       v
03_translate.py  Translate each article with Gemini, generate its title
       |         -> data/translated/{id}.txt, data/translated/{id}_title.txt
       v
04_upload.py     Clean the wikitext (sanitize.py) and upload to SciWiki
                 -> live pages on ltrc.sciwiki.in
```

`05_fix_titles.py` is an optional utility for manually fixing an individual
article's title if you spot a bad one after translation — you won't need it
most of the time, since `03_translate.py` already generates titles.

Every stage is resumable: re-running a script skips work it already did
(existing files, or existing SciWiki pages) and only processes what's left.

## 1. Setup

```bash
cd IS
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Gemini API key

Get your own key from [Google AI Studio](https://aistudio.google.com/apikey)
— do not reuse anyone else's key. Then:

```bash
cp .env.example .env
```

Edit `.env` and set:

```
GEMINI_API_KEY=your-own-key-here
```

`.env` is git-ignored — never commit it, and never paste your key into
`config.py` or any other tracked file.

### SciWiki account

Use your existing SciWiki contributor account — do not share logins between
members. Put your username in `config.py`'s `SCIWIKI_USERNAME` (see the
table in the next section).

If someone on the team doesn't have an account yet, self-registration is
open at <https://ltrc.sciwiki.in/index.php/Special:CreateAccount> (username
+ password + a simple CAPTCHA).

**Never write your password anywhere in this project.** It is not stored
in `config.py`, `.env`, or any file — `04_upload.py` asks for it
interactively, only at the moment it logs in, every time you run it.

## 2. Configure `config.py`

Open `config.py` and set these for yourself:

| Setting | What it is | Example |
|---|---|---|
| `TOPICS` | Subtopic pages from Wikipedia's ["List of lists of lists"](https://en.wikipedia.org/wiki/List_of_lists_of_lists); each run starts at the topic after the last one used | `["Lists of plants", "Lists of diseases"]` |
| `EXCLUDED_TOPICS` | Topics taken by other members — never used or explored into | `["Lists of astronomical objects"]` |
| `TARGET_LANGUAGE` | Your regional language, in English | `"Hindi"`, `"Tamil"`, `"Telugu"` |
| `START_ID` / `END_ID` | Your assigned article ID range — see table below | `10001` / `15000` |
| `MAX_ARTICLES` | How many NEW articles to collect per run (and the upload batch size) | `100` |
| `SCIWIKI_USERNAME` | Your SciWiki username | `"YourUsername"` |
| `ATTRIBUTION_TEMPLATE` | The 3-line attribution footer added to every page | translate once into your language |

`MAX_ARTICLES` must fit inside your `START_ID`–`END_ID` range — the scripts
will refuse to run otherwise.

### Team article ID assignment

Each member gets a non-overlapping block of 5,000 IDs, so nobody's uploads
can ever collide on SciWiki. Update the table as people are added.

| Member | START_ID | END_ID | Range size |
|---|---|---|---|
| 1 (Sourav) | 10001 | 15000 | 5,000 |
| 2 | 15001 | 20000 | 5,000 |
| 3 | 20001 | 25000 | 5,000 |
| 4 | 25001 | 30000 | 5,000 |
| 5 | 30001 | 35000 | 5,000 |

Everyone starts with `MAX_ARTICLES = 100` for their first batch and raises
it later, up to their range's 5,000-article ceiling.

## 3. Run the pipeline

```bash
python3 run_pipeline.py   # collect -> download -> translate
python3 04_upload.py      # publish to SciWiki, when you choose to
```

Run the same commands whenever you like, with nothing to edit between
runs. `run_pipeline.py` never uploads; `04_upload.py` asks for your
SciWiki password and uploads everything translated but not yet uploaded.
Each step works out what's already done and continues from there:

- **IDs** continue automatically from the last one used. Set `START_ID` /
  `END_ID` once to your assigned range and never touch them again.
- **Batches:** each run works on `MAX_ARTICLES` articles. Articles from
  earlier runs that aren't uploaded yet (e.g. translation stopped when the
  API quota ran out) count toward that, so unfinished work is completed
  before new articles are collected.
- **No duplicates:** an article is never collected twice, even across runs
  and through redirects. The history is kept in `data/collect_state.json`,
  so don't delete that file.
- **Uploads:** each uploaded row is marked `uploaded` in the CSV the moment
  it succeeds, so `04_upload.py` always resumes from the next article and
  goes through all remaining translated ones.

The steps can also be run one at a time:

```bash
python3 01_collect.py     # appends the next batch to data/articles.csv
python3 02_download.py    # writes data/raw/{id}.txt
python3 03_translate.py   # writes data/translated/{id}.txt + {id}_title.txt
python3 04_upload.py      # sanitizes and uploads to SciWiki
```

Steps 1–3 are safe to re-run any time (they skip already-completed work) and
need no confirmation. **Step 4 writes to a live, shared wiki — read the
next section before running it.**

### Uploading safely

`04_upload.py` has two safety switches at the top of the file:

```python
DRY_RUN = True     # True = just print what WOULD happen, upload nothing
TEST_MODE = True   # True = only process the IDs in TEST_ARTICLES
TEST_ARTICLES = ["10001", "10002", "10003"]
```

Recommended flow the first time you use it:

1. Leave both `True`. Run it — check the printed preview.
2. Set `DRY_RUN = False`, keep `TEST_MODE = True`. Run it — this uploads
   just your 2–3 test articles. Open them on SciWiki and check they render
   cleanly (no `Template:...` red links, no "Cite error" text, no visible
   `{{...}}`).
3. Once satisfied, set `TEST_MODE = False` and run it again — this processes
   all `MAX_ARTICLES` articles in your range.

Re-running `04_upload.py` on an article that's already live just updates
that page in place (MediaWiki's normal edit behavior) — it never creates a
duplicate.

## What the sanitizer (`sanitize.py`) actually does

SciWiki does not have any of Wikipedia's citation/formatting templates
installed. It does show images from Wikimedia Commons, but only files
that really exist there. Left untouched, translated wikitext would show raw
`Template:Cite journal`-style red links and broken references. Before
upload, every article is passed through `sanitize_for_sciwiki()`, which:

- Converts citation templates (`{{cite journal}}`, `{{cite web}}`, ...) into
  plain readable text with the URL/DOI kept as a link.
- Converts `{{reflist}}`/`{{efn}}`/`{{refn}}`/etc. into native `<ref>` /
  `<references />` tags (SciWiki's Cite extension supports these natively).
- Repairs `<ref>` tags whose name got inconsistently translated (a known
  Gemini quirk with digits inside reference names), recovering the original
  definition from the raw English article when needed.
- Keeps images (`[[File:...]]`, `<gallery>`, and the picture from
  infoboxes/taxoboxes) when `04_upload.py` confirms the file exists on
  Wikimedia Commons; drops the rest, rescuing any citation in the caption.
  At most 15 images per page: SciWiki takes ~2–3 s per image to render and
  gives up after 120 s.
- Rewrites internal links so none is red: a link to an article you
  translated goes to your SciWiki page, any other English target goes to
  English Wikipedia, Wiktionary/Wikisource/etc. use SciWiki's interwiki
  prefixes, categories and interlanguage links are removed, and anything
  else becomes plain text.
- Leaves tables, headings, bold/italic and external links untouched.
- Falls back safely for any template it doesn't specifically recognize —
  it never lets a raw `{{...}}` or a broken reference reach the page.

This runs automatically inside `04_upload.py` — you don't call it directly.

## Known limitations

- **Only Commons images, at most 15 per page.** Images that exist only on
  English Wikipedia (non-free files) can't be shown and are left out.
- **Title extraction can occasionally fail** on articles with unusual
  infobox structure (e.g. an HTML comment where a name field should be) —
  `04_upload.py` will report that article as `FAILED` rather than upload a
  broken title. Fix it with a `MANUAL_TITLES` entry in `05_fix_titles.py`
  for that one article ID, or re-check its `{id}_title.txt` by hand.
- **Generic-template fallback isn't perfect fidelity.** Templates outside
  the common set the sanitizer specifically handles get a best-effort
  plain-text rendering — never a red link or error, but occasionally not
  ideally formatted. Spot-check a few articles from each new topic (same as
  the safe-upload flow above) rather than assuming zero surprises at scale.

## Troubleshooting

- **"CONFIG ERROR: MAX_ARTICLES exceeds your assigned ID range"** — lower
  `MAX_ARTICLES` or double check `START_ID`/`END_ID` against the team table.
- **"GEMINI_API_KEY is not set"** — create `.env` from `.env.example` and
  fill in your key.
- **An upload shows `ACTION: FAILED`** — the script prints the specific
  API error or exception right above that line; that's the fastest way to
  diagnose it. Paste it when asking for help rather than just "it failed."
