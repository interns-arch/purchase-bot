"""Vendor Code identification: the permanent, unique identifier used
throughout the app for inventory imports (manual and WhatsApp alike),
replacing WhatsApp sender-number identification -- every vendor messages the
same shared WhatsApp Business number, so the sender's phone number can never
tell them apart. A vendor's own inventory filename carries the code instead
(`<VENDOR_CODE>_Inventory.xlsx`, e.g. `CT_SBM_Inventory.xlsx`).

FORMAT (changed 5 Oct 2026, Boodmo-style): `CT_` + the name's initials --
Yash Gupta -> CT_YG, Northend Distributors -> CT_ND. Codes used to be the
other way round (ND_CT); every existing code was renamed, and an old-style
code in a filename or anywhere else is still understood (`canonical_code`).

Identity rules:
- `vendor_code` is a STABLE, UNIQUE identifier. It is generated ONCE, when a
  vendor is first onboarded, and never regenerated from the name afterwards.
- An existing vendor is found by EXACT code (`get_vendor_by_code`) or, for
  name-only files, EXACT normalized name (`vendor_service.get_vendor_by_name`,
  a unique `lower(name)` index) -- never by prefix / first-N-letters / similar-
  name matching, which could wrongly merge different vendors such as
  "Shree Balaji Motors" vs "Shree Balaji Auto Parts".

Code generation derives a short, readable code from the vendor NAME as an
initial suggestion, but ALWAYS checks for collisions and, on collision,
produces another meaningful, name-derived unique code rather than a blind
`_2`/`_3` suffix:

    Shree Balaji Motors      -> CT_SBM
    Shree Balaji Auto Parts  -> CT_SBA
    Shree Balaji Enterprises -> CT_SBE

Single-word names keep the historical two-letter form (MAHINDRA -> CT_MA,
BIJVASAN -> CT_BI, DELHI -> CT_DE), lengthening only on collision
(MAHINDRA -> CT_MA, MARUTI -> CT_MAR).

Pure business logic -- no FastAPI/print()/input() here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Vendor

_CODE_PREFIX = "CT_"
# A filename starts with the code, then "_": CT_AR_Inventory.xlsx. The stem is
# a run of alphanumerics (CT_AR, CT_SBM, hash-fallback CT_SBM3F), optionally
# with a legacy numeric suffix (CT_MA_2). Case-insensitive.
_CODE_PREFIX_PATTERN = re.compile(r"^(CT_[A-Z0-9]+(?:_\d+)?)_")
# The OLD format (AR_CT_Inventory.xlsx), still accepted and translated.
_LEGACY_PREFIX_PATTERN = re.compile(r"^([A-Z0-9]+_CT(?:_\d+)?)_")
_LEGACY_CODE = re.compile(r"^([A-Z0-9]+)_CT((?:_\d+)?)$")

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_MAX_CONCISE_INITIALS = 3


def _words(name: str) -> list[str]:
    return [w.upper() for w in _WORD_RE.findall(name or "")]


def _candidate_stems(name: str) -> Iterator[str]:
    """Yield increasingly specific, deterministic, name-derived code stems
    (most concise first). The caller appends `_CT` and returns the first stem
    whose code is free."""
    words = _words(name)
    if not words:
        yield "XX"
        return

    seen: set[str] = set()

    def _emit(stem: str) -> Iterator[str]:
        if stem and stem not in seen:
            seen.add(stem)
            yield stem

    if len(words) == 1:
        word = words[0]
        # Two letters, then three, four, ... up to the whole word.
        for length in range(2, max(2, len(word)) + 1):
            yield from _emit(word[:length])
    else:
        initials = "".join(word[0] for word in words)
        # 1) initials of the first few words, 2) initials of all words.
        yield from _emit(initials[:_MAX_CONCISE_INITIALS])
        yield from _emit(initials)
        # 3) initials + progressively more letters from each word, for more
        #    uniqueness while staying derived from the actual name.
        extended = initials
        for word in words:
            for extra_char in word[1:3]:
                extended += extra_char
                yield from _emit(extended)


def generate_vendor_code(name: str, session: Session) -> str:
    """Generate a short, readable, UNIQUE vendor code from `name`. Tries
    name-derived stems (word initials, then longer forms) and returns the
    first whose `<stem>_CT` code isn't already taken. If every readable stem
    collides (extremely unlikely), falls back to a deterministic short hash of
    the name -- still name-derived and unique, never a blind `_2`/`_3`.

    Call this ONLY when onboarding a genuinely new vendor -- never to
    re-derive an existing vendor's code.
    """
    for stem in _candidate_stems(name):
        code = f"{_CODE_PREFIX}{stem}"
        if get_vendor_by_code(code, session) is None:
            return code

    # Deterministic name-derived fallback (guarantees termination + uniqueness).
    base = next(_candidate_stems(name), "XX")
    digest = hashlib.sha1((name or "").strip().lower().encode("utf-8")).hexdigest().upper()
    for index in range(len(digest) - 1):
        code = f"{_CODE_PREFIX}{base}{digest[index:index + 2]}"
        if get_vendor_by_code(code, session) is None:
            return code

    raise RuntimeError(f"Unable to generate a unique vendor code for {name!r}.")


def canonical_code(code: str) -> str:
    """The current form of a vendor code: 'ND_CT' -> 'CT_ND', 'MA_CT_2' ->
    'CT_MA_2'; a code already in CT_ form is returned upper-cased."""
    text = (code or "").strip().upper()
    legacy = _LEGACY_CODE.match(text)
    if legacy:
        return f"{_CODE_PREFIX}{legacy.group(1)}{legacy.group(2)}"
    return text


def parse_vendor_code_from_filename(filename: str) -> str | None:
    """Return the leading vendor code (in the CURRENT CT_ form) from a
    filename like "CT_SBM_Inventory.xlsx" -- or the old "SBM_CT_Inventory.xlsx"
    -- or `None` if the filename has no code-shaped prefix. Case-insensitive."""
    name = filename.strip().upper()
    match = _CODE_PREFIX_PATTERN.match(name)
    if match:
        return match.group(1)
    legacy = _LEGACY_PREFIX_PATTERN.match(name)
    return canonical_code(legacy.group(1)) if legacy else None


def is_legacy_style(filename: str) -> bool:
    """True when the filename used the OLD 'XX_CT_' prefix."""
    name = filename.strip().upper()
    return not _CODE_PREFIX_PATTERN.match(name) and bool(_LEGACY_PREFIX_PATTERN.match(name))


def get_vendor_by_code(code: str, session: Session) -> Vendor | None:
    """Exact code match, accepting either the CT_ form or the old XX_CT form."""
    wanted = {canonical_code(code), (code or "").strip().upper()}
    return session.execute(
        select(Vendor).where(func.upper(Vendor.vendor_code).in_(wanted))
    ).scalars().first()
