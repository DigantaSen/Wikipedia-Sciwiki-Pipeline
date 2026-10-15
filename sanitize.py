"""
sanitize.py

Converts English-Wikipedia-flavoured wikitext (already translated into Bengali,
templates/markup left untouched by the translator) into markup that renders
correctly on LTRC SciWiki.

Why this exists:
SciWiki (MediaWiki 1.43) has the Cite extension (so <ref>/<references /> work
natively) and ParserFunctions/Scribunto are installed, but NONE of the
Wikipedia citation/formatting templates exist there (verified via the API:
Template:Cite journal, Template:Reflist, Template:Nobr, Template:Infobox,
Module:Citation/CS1 all return "missing"). Wikimedia Commons images do render
(InstantCommons), but only for files that really exist there. When MediaWiki
hits an unresolved {{template}}, it silently drops every parameter and shows
only a red link to "Template:X" -- that is why translated citation text and
table cells appeared to "lose" their content and show bare template names.

This module walks the wikitext, resolves every {{template}} bottom-up using a
brace-depth parser (so nested templates such as {{cite ...| ... {{sfnRef|...}} }}
are handled correctly), and re-renders each one as plain/native MediaWiki
markup that needs no external template definition. Native constructs that
already work everywhere (<ref>, tables, headings, bold/italic, [[links]],
external [http://... links]) are left untouched; images and internal links
are checked and rewritten so none of them shows up as a red link.
"""

import re


# ============================================================
# Brace / bracket aware parsing helpers
# ============================================================

def _find_template_end(text, start):
    """start points at the opening '{{'. Returns index right after the matching '}}'."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        if text[i:i + 2] == "{{":
            depth += 1
            i += 2
            continue
        if text[i:i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return n


def _find_link_end(text, start):
    """start points at the opening '[['. Returns index right after the matching ']]'."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        if text[i:i + 2] == "[[":
            depth += 1
            i += 2
            continue
        if text[i:i + 2] == "]]":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return n


def _split_top_level(s, sep="|"):
    """Split on `sep` but skip over anything nested inside {{..}} or [[..]]."""
    parts = []
    current = []
    depth_brace = 0
    depth_bracket = 0
    i = 0
    n = len(s)
    while i < n:
        two = s[i:i + 2]
        if two == "{{":
            depth_brace += 1
            current.append(two)
            i += 2
            continue
        if two == "}}":
            depth_brace = max(0, depth_brace - 1)
            current.append(two)
            i += 2
            continue
        if two == "[[":
            depth_bracket += 1
            current.append(two)
            i += 2
            continue
        if two == "]]":
            depth_bracket = max(0, depth_bracket - 1)
            current.append(two)
            i += 2
            continue
        ch = s[i]
        if ch == sep and depth_brace == 0 and depth_bracket == 0:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


_NAMED_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_ -]{0,40})\s*=(?!=)(.*)$", re.DOTALL)


def _is_named(param):
    return bool(_NAMED_RE.match(param))


def _split_named(param):
    m = _NAMED_RE.match(param)
    if not m:
        return None, param.strip()
    return m.group(1).strip().lower(), m.group(2).strip()


def _positional_only(params):
    return [p.strip() for p in params if not _is_named(p)]


def _named_dict(params):
    out = {}
    for p in params:
        k, v = _split_named(p)
        if k is not None and v:
            out[k] = v
    return out


_NUMERIC_RE = re.compile(r"^[+\-−]?[\d০-৯][\d০-৯,.]*$")


def _looks_numeric(tok):
    return bool(_NUMERIC_RE.match(tok.strip()))


# ============================================================
# Template category tables
# ============================================================

MAGIC_WORDS = {
    "defaultsort", "displaytitle", "pagename", "basepagename", "namespace",
    "fullpagename", "currentyear", "currentmonth", "currentday", "currentday2",
    "currenttime", "sitename", "server", "revisionid", "lc", "uc", "ucfirst",
    "lcfirst", "padleft", "padright", "formatnum", "plural", "ns",
}

DROP_ALWAYS = {
    "short description", "use dmy dates", "use mdy dates", "use british english", "use american english",
    "use australian english", "use indian english", "use canadian english",
    "featured article", "good article", "authority control", "portal bar",
    "portal", "commons category", "commonscat", "commons", "pd-notice",
    "div col", "div col end", "colend", "columns-list", "clear", "catalogs",
    "sfnref", "image frame", "legend", "redirect",
    "redirect2", "redirect-distinguish", "distinguish", "for", "otheruses",
    "otheruses4", "other uses", "about", "self-published inline",
    "citation needed", "cn", "when", "who", "clarify", "better source needed",
    "image", "gallery", "stack",
}

HATNOTE_LABELS = {
    "main": "মূল নিবন্ধ",
    "further": "আরও দেখুন",
    "see also": "আরও দেখুন",
    "main article": "মূল নিবন্ধ",
}

CITATION_NAMES = {
    "cite journal", "cite web", "cite book", "cite news", "citation",
    "cite magazine", "cite encyclopedia", "cite conference", "cite av media",
    "cite press release", "cite arxiv", "cite arxiv2", "cite simbad",
    "cite thesis", "cite report", "cite interview", "cite court",
    "cite speech", "cite podcast", "cite techreport", "cite document",
}


def _passthrough_inline_fmt(open_tag, close_tag):
    def fn(params):
        pos = _positional_only(params)
        return f"{open_tag}{pos[0]}{close_tag}" if pos else ""
    return fn


def _format_convert(params):
    pos = _positional_only(params)
    if not pos:
        return ""
    tokens = []
    i = 0
    while i < len(pos) and (_looks_numeric(pos[i]) or pos[i].lower() in ("x", "×")):
        tokens.append(pos[i])
        i += 1
    if i < len(pos):
        tokens.append(pos[i])
    return " ".join(tokens) if tokens else pos[-1]


def _format_val(params):
    pos = _positional_only(params)
    named = _named_dict(params)
    if len(pos) >= 2:
        main = f"{pos[0]} ± {pos[1]}"
    elif pos:
        main = pos[0]
    else:
        main = ""
    if named.get("e"):
        main += f"×10^{named['e']}"
    unit = named.get("u") or named.get("ul")
    if unit:
        main += f" {unit}"
    return main.strip()


def _format_ra(params):
    pos = _positional_only(params)
    labels = ["h", "m", "s"]
    out = []
    for i, p in enumerate(pos[:3]):
        out.append(p + labels[i])
    return " ".join(out)


def _format_dec(params):
    pos = _positional_only(params)
    labels = ["°", "′", "″"]
    out = []
    for i, p in enumerate(pos[:3]):
        out.append(p + labels[i])
    return " ".join(out)


def _format_ipa(params):
    pos = _positional_only(params)
    tokens = ["" if p.strip() == "-" else p for p in pos]
    joined = "".join(tokens)
    return f"IPA: /{joined}/" if joined else ""


def _format_r(params):
    pos = _positional_only(params)
    out = []
    for p in pos:
        p = p.strip()
        if not p or re.fullmatch(r"[A-Za-z]", p):
            continue
        out.append(f'<ref name="{p}" />')
    return "".join(out)


def _clean_ref_name(name):
    if name is None:
        return None
    return name.strip().strip('"').strip("'").strip() or None


def _format_ref_template(default_group=None):
    """Shared renderer for {{efn}} (explanatory footnotes) and {{refn}}
    (general-purpose <ref> wrapper used when a citation needs to contain a
    literal '|', e.g. {{refn|name="x"|See {{harvnb|...}} for details.}}).
    Both compile down to plain <ref> tags, which SciWiki's Cite extension
    already understands natively."""

    def fn(params):
        named = _named_dict(params)
        pos = _positional_only(params)
        name = _clean_ref_name(named.get("name"))
        group = named.get("group") or default_group
        content = pos[0] if pos else ""
        attrs = ""
        if group:
            attrs += f' group="{group}"'
        if name:
            attrs += f' name="{name}"'
        if content:
            return f"<ref{attrs}>{content}</ref>"
        if name:
            return f"<ref{attrs} />"
        return ""
    return fn


def _format_reflist(default_group=None):
    def fn(params):
        named = _named_dict(params)
        group = named.get("group") or default_group
        group_attr = f' group="{group}"' if group else ""
        tag = f"<references{group_attr} />"
        refs_val = named.get("refs") or named.get("notes")
        return f"{refs_val}\n{tag}" if refs_val else tag
    return fn


_YEAR_RE = re.compile(r"^[\d০-৯]{4}[a-z]?$")


def _format_harv(params):
    pos = _positional_only(params)
    named = _named_dict(params)
    authors = list(pos)
    year = None
    if authors and _YEAR_RE.match(authors[-1]):
        year = authors.pop()
    pieces = []
    if authors:
        pieces.append(", ".join(authors))
    if year:
        pieces.append(year)
    if named.get("p"):
        pieces.append(f"p. {named['p']}")
    if named.get("pp"):
        pieces.append(f"pp. {named['pp']}")
    if named.get("loc"):
        pieces.append(named["loc"])
    return f"({', '.join(pieces)})" if pieces else ""


def _format_hatnote(label):
    def fn(params):
        pos = _positional_only(params)
        if not pos:
            return ""
        return f"''{label}: {', '.join(pos)}''"
    return fn


def _format_webarchive(params):
    named = _named_dict(params)
    url = named.get("url")
    if not url:
        return ""
    return f"[{url} সংরক্ষিত সংস্করণ]"


def _prettify_infobox_name(name_raw):
    rest = name_raw.split(None, 1)
    label = rest[1] if len(rest) > 1 else ""
    return label.strip().title() if label else "তথ্যছক"


_SKIP_INFOBOX_KEYS = ("image", "caption", "map", "logo", "alt", "photo", "flag", "signature", "footnotes", "upright")

# Infobox-style templates that aren't named "Infobox ..." (taxoboxes etc.).
# Without this they fell through to _generic_fallback, which printed their
# first parameter -- usually the image filename -- as bare text.
BOX_TEMPLATES = {
    "speciesbox", "automatic taxobox", "taxobox", "subspeciesbox",
    "infraspeciesbox", "hybridbox", "virusbox", "paraphyletic group",
    "drugbox", "chembox",
}

_INFOBOX_IMAGE_KEYS = ("image", "image1", "image_name", "photo", "img", "image_file")
_INFOBOX_CAPTION_KEYS = ("caption", "image_caption", "image caption", "caption1", "imagecaption")


def _image_name(value):
    """'Foo.jpg', 'File:Foo.jpg' or '[[File:Foo.jpg|...]]' -> 'Foo.jpg'."""
    v = value.strip()
    if v.startswith("[["):
        v = _split_top_level(v[2:-2] if v.endswith("]]") else v[2:], "|")[0]
    m = _FILE_PREFIX_RE.match("[[" + v)
    if m:
        v = v.split(":", 1)[1]
    v = v.strip()
    return v if _IMAGE_EXT_RE.search(v) else None


def _thumb(name, caption=""):
    caption = caption.strip()
    return f"[[File:{name}|thumb|{caption}]]" if caption else f"[[File:{name}|thumb]]"


def _format_infobox(name_raw, params):
    named_order = []
    for p in params:
        k, v = _split_named(p)
        if k and v:
            named_order.append((k, v))
    named = dict(named_order)
    header = None
    rows = []
    for k, v in named_order:
        if k == "name":
            header = v
            continue
        if any(skip in k for skip in _SKIP_INFOBOX_KEYS):
            continue
        rows.append((k, v))

    image = ""
    for key in _INFOBOX_IMAGE_KEYS:
        name = _image_name(named.get(key, ""))
        if name:
            caption = next((named[c] for c in _INFOBOX_CAPTION_KEYS if named.get(c)), "")
            image = _thumb(name, caption) + "\n"
            break

    if not rows and not header:
        return image
    if header is None:
        header = _prettify_infobox_name(name_raw)
    out = ['{| class="wikitable"', f'! colspan="2" | {header}']
    for k, v in rows:
        out.append("|-")
        out.append(f"| '''{k}''' || {v}")
    out.append("|}")
    return image + "\n".join(out)


def _format_multiple_image(params):
    """{{multiple image|image1=..|caption1=..|image2=..}} -> one thumbnail each."""
    named = _named_dict(params)
    out = []
    for i in [""] + [str(n) for n in range(1, 11)]:
        name = _image_name(named.get(f"image{i}", ""))
        if name:
            out.append(_thumb(name, named.get(f"caption{i}", "")))
    return "\n".join(out)


def _format_citation(params):
    named = _named_dict(params)

    def get(*keys):
        for k in keys:
            if named.get(k):
                return named[k]
        return None

    title = get("title")
    url = get("url", "archive-url", "chapter-url")
    journal = get("journal", "work", "website", "newspaper", "magazine", "encyclopedia")
    publisher = get("publisher")
    date = get("date", "year")
    volume = get("volume")
    issue = get("issue", "number")
    pages = get("pages", "page")
    isbn = get("isbn")
    doi = get("doi")

    authors = []
    if named.get("author"):
        authors.append(named["author"])
    else:
        for i in range(1, 10):
            suffix = "" if i == 1 else str(i)
            last = named.get(f"last{suffix}")
            first = named.get(f"first{suffix}")
            if not last and not first:
                break
            authors.append(", ".join(x for x in (last, first) if x))

    title_part = f'"{title}"' if title else ""
    if url and title:
        title_part = f"[{url} {title}]"
    elif url and not title:
        title_part = url

    pieces = []
    if authors:
        pieces.append("; ".join(authors))
    if date:
        pieces.append(f"({date})")
    if title_part:
        pieces.append(title_part)
    if journal:
        pieces.append(f"''{journal}''")
    vip = [x for x in (volume, f"({issue})" if issue else None, pages) if x]
    if vip:
        pieces.append(" ".join(vip))
    if publisher and not journal:
        pieces.append(publisher)
    if isbn:
        pieces.append(f"ISBN {isbn}")
    if doi:
        pieces.append(f"doi:[https://doi.org/{doi} {doi}]")

    return ". ".join(pieces) + ("." if pieces else "")


_DISPATCH = None


def _build_dispatch():
    d = {}
    for name in DROP_ALWAYS:
        d[name] = lambda params: ""
    for name in CITATION_NAMES:
        d[name] = _format_citation
    d["reflist"] = _format_reflist(None)
    for name in ("notelist", "notelist-ua", "notelist-lr", "notelist-ur"):
        d[name] = _format_reflist("note")
    for name in ("efn", "efn-ua", "efn-lr", "efn-ur"):
        d[name] = _format_ref_template("note")
    d["refn"] = _format_ref_template(None)
    for name in ("sfn", "harvnb", "harv", "sfnp"):
        d[name] = _format_harv
    d["r"] = _format_r
    for name in ("nobr", "nowrap"):
        d[name] = lambda params: (_positional_only(params) or [""])[0]
    d["small"] = _passthrough_inline_fmt("<small>", "</small>")
    d["sup"] = _passthrough_inline_fmt("<sup>", "</sup>")
    d["sub"] = _passthrough_inline_fmt("<sub>", "</sub>")
    for name in ("convert", "cvt"):
        d[name] = _format_convert
    d["val"] = _format_val
    d["ra"] = _format_ra
    d["dec"] = _format_dec
    d["ipac-en"] = _format_ipa
    d["lang"] = lambda params: (_positional_only(params) or [""])[-1]
    d["ill"] = lambda params: (_positional_only(params) or [""])[0]
    d["sfrac"] = lambda params: "/".join(_positional_only(params))
    d["nbsp"] = lambda params: "&nbsp;"
    d["snd"] = lambda params: " – "
    d["webarchive"] = _format_webarchive
    d["multiple image"] = _format_multiple_image
    for name, label in HATNOTE_LABELS.items():
        d[name] = _format_hatnote(label)
    return d


def _normalize_name(name_raw):
    return re.sub(r"[_\s]+", " ", name_raw.strip()).lower()


def _generic_fallback(params):
    pos = [p for p in _positional_only(params) if p]
    if pos:
        return pos[-1]
    named = _named_dict(params)
    if named:
        return next(iter(named.values()))
    return ""


def _render_template(raw_inner):
    global _DISPATCH
    if _DISPATCH is None:
        _DISPATCH = _build_dispatch()

    parts = _split_top_level(raw_inner, "|")
    name_raw = parts[0].strip()
    params_raw = parts[1:]

    first_segment = name_raw.split(":", 1)[0].strip().lower()
    if name_raw.startswith("#") or first_segment in MAGIC_WORDS:
        return "{{" + raw_inner + "}}"

    if name_raw == "!":
        return "|"
    if name_raw == "=":
        return "="

    name = _normalize_name(name_raw)
    resolved_params = [_resolve_templates(p) for p in params_raw]

    if name.startswith("infobox") or name in BOX_TEMPLATES:
        return _format_infobox(name_raw, resolved_params)

    handler = _DISPATCH.get(name)
    if handler is not None:
        return handler(resolved_params)

    return _generic_fallback(resolved_params)


def _resolve_templates(text):
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i:i + 2] == "{{":
            end = _find_template_end(text, i)
            raw_inner = text[i + 2:end - 2]
            out.append(_render_template(raw_inner))
            i = end
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


# ============================================================
# Images and links
#
# SciWiki renders Wikimedia Commons files ([[File:X.jpg]] shows the real
# image), but a file that isn't on Commons -- e.g. an English-Wikipedia-only
# "fair use" file, or a filename the translator altered -- shows as a red
# link. So each image is kept only if `file_ok` says the file exists;
# otherwise it is removed (rescuing any <ref> in its caption).
#
# Internal links are rewritten so every link works:
#   - an article we translated  -> our own SciWiki page
#   - any other English target  -> English Wikipedia ([[wikipedia:X|...]])
#   - Wiktionary/Wikisource/... -> SciWiki's matching interwiki prefix
#   - categories, interlanguage links -> removed
#   - anything else unresolvable (e.g. a target the translator turned into
#     Bengali) -> plain text, never a red link
# ============================================================

_FILE_PREFIX_RE = re.compile(r"^\[\[\s*:?\s*(file|image|media|চিত্র|ফাইল|ছবি)\s*:", re.IGNORECASE)
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|svg|gif|tiff?|webp|bmp|ogg|ogv|webm|pdf|djvu)\s*$", re.IGNORECASE)
_REF_TAG_RE = re.compile(r"<ref\b[^>]*(?<!/)>.*?</ref>|<ref\b[^>]*/\s*>", re.IGNORECASE | re.DOTALL)
_GALLERY_RE = re.compile(r"(<gallery\b[^>]*>)(.*?)(</gallery>)", re.IGNORECASE | re.DOTALL)
_BENGALI_RE = re.compile(r"[ঀ-৿]")

_CATEGORY_PREFIXES = {"category", "বিষয়শ্রেণী", "বিষয়শ্রেণি", "শ্রেণী", "বিভাগ"}

# Interwiki prefixes that work on SciWiki (from its siteinfo interwikimap),
# plus the aliases English Wikipedia uses for them.
_INTERWIKI = {
    "wikt": "wikt", "wiktionary": "wiktionary", "উইকিঅভিধান": "wiktionary",
    "wikisource": "wikisource", "s": "wikisource", "উইকিসংকলন": "wikisource", "উইকিসোর্স": "wikisource",
    "wikispecies": "wikispecies", "species": "wikispecies",
    "wikiquote": "wikiquote", "q": "wikiquote",
    "wikibooks": "wikibooks", "b": "wikibooks",
    "wikinews": "wikinews", "n": "wikinews",
    "wikiversity": "wikiversity", "v": "wikiversity",
    "wikivoyage": "wikivoyage", "voy": "wikivoyage",
    "wikidata": "wikidata", "d": "wikidata",
    "commons": "commons", "c": "commons",
    "wikipedia": "wikipedia", "w": "wikipedia", "উইকিপিডিয়া": "wikipedia",
    "meta": "metawikimedia", "m": "metawikimedia",
    "mw": "mw", "mediawikiwiki": "mediawikiwiki",
    "doi": "doi", "pmid": "pmid", "arxiv": "arxiv", "rfc": "rfc", "oeis": "oeis",
}

# English Wikipedia namespaces: point them at English Wikipedia.
_WIKIPEDIA_NAMESPACES = {
    "help", "h", "template", "template talk", "portal", "talk", "user",
    "user talk", "draft", "module", "mos", "wp", "project", "wikipedia talk",
    "file talk", "category talk", "book", "timedtext",
}

_LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(-[a-z]{2,8})*$")


def _norm_title(title):
    t = re.sub(r"[_\s]+", " ", title).strip()
    return t[:1].upper() + t[1:] if t else t


def _file_target(inner):
    """'File:Foo.jpg|thumb|...' -> ('Foo.jpg', ['thumb', ...]) or None."""
    parts = _split_top_level(inner, "|")
    head = parts[0].strip().lstrip(":")
    if ":" not in head:
        return None
    name = _norm_title(head.split(":", 1)[1])
    if not name:
        return None
    return name, parts[1:]


def _gallery_line_target(line):
    head = line.split("|", 1)[0]
    if _FILE_PREFIX_RE.match("[[" + head):
        return _file_target(line)
    return _file_target("File:" + line)


def _rescued_refs(chunk):
    return "".join(_REF_TAG_RE.findall(chunk))


class _LinkFixer:

    def __init__(self, file_ok, local_pages, max_images):
        self.file_ok = file_ok or (lambda name: True)
        self.images_left = max_images
        self.local = {_norm_title(k): v for k, v in (local_pages or {}).items()}
        self.local_bn = {_norm_title(v) for v in self.local.values()}

    # ---------------- images ----------------

    def _use_image(self, name):
        if self.images_left <= 0 or not self.file_ok(name):
            return False
        self.images_left -= 1
        return True

    def image(self, chunk):
        target = _file_target(chunk[2:-2])
        if not target or not self._use_image(target[0]):
            return _rescued_refs(chunk)
        name, params = target
        params = [self.fix(p) for p in params]
        return "[[" + "|".join([f"File:{name}"] + params) + "]]"

    def gallery(self, m):
        kept = []
        for line in m.group(2).split("\n"):
            line = line.strip()
            if not line:
                continue
            target = _gallery_line_target(line)
            if target and self._use_image(target[0]):
                name, params = target
                kept.append("|".join([f"File:{name}"] + [self.fix(p) for p in params]))
        if not kept:
            return _rescued_refs(m.group(0))
        return m.group(1) + "\n" + "\n".join(kept) + "\n" + m.group(3)

    # ---------------- links ----------------

    def link(self, chunk):
        inner = chunk[2:-2]
        if "|" in inner:
            target, display = inner.split("|", 1)
            display = self.fix(display)
        else:
            target, display = inner, None

        leading_colon = target.lstrip().startswith(":")
        target = target.strip().lstrip(":").strip()
        if not target:
            return display or ""

        prefix, sep, rest = target.partition(":")
        p_raw = prefix.strip()
        p = p_raw.lower()
        rest = rest.strip()

        if sep:
            shown = display if display is not None else rest
            if p in _CATEGORY_PREFIXES:
                return shown if leading_colon else ""
            if p in _INTERWIKI:
                return f"[[{_INTERWIKI[p]}:{rest}|{shown}]]"
            if p == "special":
                return chunk
            if p in _WIKIPEDIA_NAMESPACES:
                return f"[[wikipedia:{p_raw}:{rest}|{shown}]]"
            # Interlanguage link ([[fr:...]]) -- always a lowercase code
            if _LANG_CODE_RE.match(p_raw):
                if leading_colon and p_raw == "en":
                    return f"[[wikipedia:{rest}|{shown}]]"
                return shown if leading_colon else ""

        page, _, fragment = target.partition("#")
        key = _norm_title(page)
        anchor = f"#{fragment}" if fragment else ""
        shown = display if display is not None else target

        if not key:
            return shown  # [[#Section]]: keep the text only

        if key in self.local:
            return f"[[{self.local[key]}{anchor}|{shown}]]"

        if key in self.local_bn:
            return f"[[{key}{anchor}|{shown}]]"

        if not _BENGALI_RE.search(key):
            return f"[[wikipedia:{key}{anchor}|{shown}]]"

        return shown

    # ---------------- walk ----------------

    def fix(self, text):
        out = []
        i = 0
        n = len(text)
        while i < n:
            gallery = _GALLERY_RE.match(text, i) if text[i] == "<" else None
            if gallery:
                out.append(self.gallery(gallery))
                i = gallery.end()
                continue
            if text[i:i + 2] == "[[":
                end = _find_link_end(text, i)
                chunk = text[i:end]
                if not chunk.endswith("]]"):
                    out.append(chunk)
                elif _FILE_PREFIX_RE.match(chunk):
                    out.append(self.image(chunk))
                else:
                    out.append(self.link(chunk))
                i = end
                continue
            out.append(text[i])
            i += 1
        return "".join(out)


def find_image_names(text):
    """Every file name referenced by [[File:...]] or <gallery> in `text`,
    including infobox images (templates are resolved first)."""
    text = _resolve_templates(text)
    names = set()
    for m in _GALLERY_RE.finditer(text):
        for line in m.group(2).split("\n"):
            line = line.strip()
            if line:
                t = _gallery_line_target(line)
                if t:
                    names.add(t[0])
    for m in re.finditer(r"\[\[\s*:?\s*(?:file|image|media|চিত্র|ফাইল|ছবি)\s*:([^|\]]+)", text, re.IGNORECASE):
        name = _norm_title(m.group(1))
        if name:
            names.add(name)
    return names


# ============================================================
# Orphaned <ref name="X" /> repair
#
# Gemini's chunked translation sometimes translates the digits inside a
# <ref name="..."> attribute in one occurrence but not another (e.g.
# "oxforddic2" -> "oxforddic২" in a self-closing usage while the full
# definition keeps "oxforddic2"), which breaks MediaWiki's exact-string name
# matching and produces a visible "Cite error: ... no text was provided for
# refs named X". This pass repairs that class of bug, then falls back to the
# original English wikitext (data/raw/{id}.txt) to recover a definition that
# never survived translation at all, and as a last resort silently drops any
# self-closing reference that still has no definition anywhere, rather than
# let a raw Cite error reach the page.
# ============================================================

# Digit blocks for the Indic scripts this pipeline is likely to translate
# into (Bengali, Devanagari for Hindi/Marathi, Gurmukhi for Punjabi,
# Gujarati, Oriya, Telugu, Kannada, Malayalam, Tamil) -- all normalized to
# Latin digits so a <ref name="x2"> translated into any of these scripts
# still matches its original definition. Harmless no-op for scripts not
# present in a given article.
_INDIC_DIGIT_BLOCKS = [
    "০১২৩৪৫৬৭৮৯",  # Bengali
    "०१२३४५६७८९",  # Devanagari (Hindi, Marathi, Nepali)
    "੦੧੨੩੪੫੬੭੮੯",  # Gurmukhi (Punjabi)
    "૦૧૨૩૪૫૬૭૮૯",  # Gujarati
    "୦୧୨୩୪୫୬୭୮୯",  # Oriya
    "౦౧౨౩౪౫౬౭౮౯",  # Telugu
    "೦೧೨೩೪೫೬೭೮೯",  # Kannada
    "൦൧൨൩൪൫൬൭൮൯",  # Malayalam
    "௦௧௨௩௪௫௬௭௮௯",  # Tamil
]
_INDIC_DIGITS = str.maketrans(
    "".join(_INDIC_DIGIT_BLOCKS),
    "0123456789" * len(_INDIC_DIGIT_BLOCKS),
)

_FULL_REF_RE = re.compile(
    r'<ref\s+name\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'/>]+))[^>]*(?<!/)>(.*?)</ref>',
    re.IGNORECASE | re.DOTALL,
)
_SELF_REF_RE = re.compile(
    r'<ref\s+name\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'/>]+))[^>]*/\s*>',
    re.IGNORECASE,
)


def _ref_name_of(m):
    return m.group(1) or m.group(2) or m.group(3)


def _norm_ref_name(name):
    return name.translate(_INDIC_DIGITS)


def _repair_refs(text, raw_text=None):
    def defined_names(t):
        return {_ref_name_of(m) for m in _FULL_REF_RE.finditer(t)}

    defined = defined_names(text)
    defined_norm = {}
    for n in defined:
        defined_norm.setdefault(_norm_ref_name(n), n)

    def fix_digit_drift(m):
        n = _ref_name_of(m)
        if n in defined:
            return m.group(0)
        target = defined_norm.get(_norm_ref_name(n))
        return f'<ref name="{target}" />' if target else m.group(0)

    text = _SELF_REF_RE.sub(fix_digit_drift, text)

    if raw_text:
        defined = defined_names(text)
        raw_by_name = {}
        raw_by_norm = {}
        for m in _FULL_REF_RE.finditer(raw_text):
            n = _ref_name_of(m)
            raw_by_name.setdefault(n, m.group(4))
            raw_by_norm.setdefault(_norm_ref_name(n), m.group(4))

        filled = set()

        def fill_from_raw(m):
            n = _ref_name_of(m)
            if n in defined or n in filled:
                return m.group(0)
            content = raw_by_name.get(n) or raw_by_norm.get(_norm_ref_name(n))
            if content is None:
                return m.group(0)
            filled.add(n)
            return f'<ref name="{n}">{content}</ref>'

        text = _SELF_REF_RE.sub(fill_from_raw, text)

    defined = defined_names(text)

    def drop_unrecoverable(m):
        return m.group(0) if _ref_name_of(m) in defined else ""

    text = _SELF_REF_RE.sub(drop_unrecoverable, text)
    return text


# ============================================================
# Cleanup passes
# ============================================================

def _collapse_blank_lines(text):
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


# SciWiki looks up every Commons image when it renders a page (~2-3 s each,
# not cached), and gives up after 120 s -- a page with 60 images can't even
# be displayed or saved. Keep the first images (infobox picture first).
MAX_IMAGES_PER_PAGE = 15


def sanitize_for_sciwiki(text, raw_text=None, file_ok=None, local_pages=None,
                         max_images=MAX_IMAGES_PER_PAGE):
    """
    Convert translated Wikipedia wikitext into SciWiki-compatible markup.

    `raw_text` is the original (untranslated) English wikitext for the same
    article, if available -- it is only used to recover <ref> definitions
    that were dropped during translation and never appear in `text` at all.

    `file_ok(name) -> bool` says whether an image file exists on Wikimedia
    Commons (and so will render on SciWiki). If omitted, every image is kept.

    `local_pages` maps English article titles to the SciWiki page titles of
    articles we translated, so links to them stay on SciWiki.

    At most `max_images` images are kept, in page order.

    Order matters: templates are resolved first (this turns {{refn}}/{{efn}}
    into literal <ref> tags and infobox images into [[File:...]]), then
    dangling ref names are repaired, then images and links are fixed, then
    leftover blank-line noise is collapsed.
    """
    text = _resolve_templates(text)
    resolved_raw = _resolve_templates(raw_text) if raw_text else None
    text = _repair_refs(text, resolved_raw)
    text = _LinkFixer(file_ok, local_pages, max_images).fix(text)
    text = _collapse_blank_lines(text)
    return text
