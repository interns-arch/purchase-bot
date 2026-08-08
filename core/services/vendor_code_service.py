"""Vendor Code identification: the permanent, unique identifier used
throughout the app for inventory imports (manual and WhatsApp alike),
replacing WhatsApp sender-number identification -- every vendor messages the
same shared WhatsApp Business number, so the sender's phone number can never
tell them apart. A vendor's own inventory filename carries the code instead
(`<VENDOR_CODE>_Inventory.xlsx`, e.g. `SBM_CT_Inventory.xlsx`).

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

    Shree Balaji Motors      -> SBM_CT
    Shree Balaji Auto Parts  -> SBA_CT
    Shree Balaji Enterprises -> SBE_CT

Single-word names keep the historical two-letter form (MAHINDRA -> MA_CT,
BIJVASAN -> BI_CT, DELHI -> DE_CT), lengthening only on collision
(MAHINDRA -> MA_CT, MARUTI -> MAR_CT).

Pure business logic -- no FastAPI/print()/input() here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Vendor

_CODE_SUFFIX = "_CT"
# A code is a run of alphanumerics + "_CT" (variable-length stem, so 2-letter
# AR_CT and 3+-letter SBM_CT / hash-fallback SBM3F_CT all parse), optionally
# followed by a legacy numeric suffix (_2/_3 from older data), then "_" before
# the rest of the filename. Case-insensitive (filename is upper()ed first).
_CODE_PREFIX_PATTERN = re.compile(r"^([A-Z0-9]+_CT(?:_\d+)?)_")

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
        code = f"{stem}{_CODE_SUFFIX}"
        if get_vendor_by_code(code, session) is None:
            return code

    # Deterministic name-derived fallback (guarantees termination + uniqueness).
    base = next(_candidate_stems(name), "XX")
    digest = hashlib.sha1((name or "").strip().lower().encode("utf-8")).hexdigest().upper()
    for index in range(len(digest) - 1):
        code = f"{base}{digest[index:index + 2]}{_CODE_SUFFIX}"
        if get_vendor_by_code(code, session) is None:
            return code

    raise RuntimeError(f"Unable to generate a unique vendor code for {name!r}.")


def parse_vendor_code_from_filename(filename: str) -> str | None:
    """Return the leading vendor-code prefix (e.g. "AR_CT", "SBM_CT",
    "MA_CT_2") from a filename like "SBM_CT_Inventory.xlsx", or `None` if the
    filename has no code-shaped prefix at all. Case-insensitive."""
    match = _CODE_PREFIX_PATTERN.match(filename.strip().upper())
    return match.group(1) if match else None


def get_vendor_by_code(code: str, session: Session) -> Vendor | None:
    return session.execute(
        select(Vendor).where(func.upper(Vendor.vendor_code) == code.upper())
    ).scalar_one_or_none()
