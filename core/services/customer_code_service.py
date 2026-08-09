"""Customer Code identification: mirrors `core.services.vendor_code_service`
for Customer Orders arriving over WhatsApp, where multiple customers may
send files one after another from the founder's side -- there is no
per-customer WhatsApp number, so a Customer Order's filename carries the
code instead (`AB_CO_Order.xlsx`), exactly like a vendor's inventory file.

Code generation mirrors the robustness of `vendor_code_service`: a short,
readable, DETERMINISTIC code derived from the customer name, checked for
collisions and extended only when needed (never a blind `_2`/`_3` suffix on
first use). Critically, DIFFERENT customers with the same first two letters
(aman/amit -> previously both "AM_CO") now get distinct, meaningful codes by
extending the candidate stem (e.g. "AMA_CO", "AMI_CO") before ever falling
back to a numeric suffix.

Pure business logic -- no FastAPI/print()/input() here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Customer

_CODE_SUFFIX = "_CO"
_CODE_PREFIX_PATTERN = re.compile(r"^([A-Z0-9]+_CO(?:_\d+)?)_")

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_MAX_CONCISE_INITIALS = 3


def _words(name: str) -> list[str]:
    return [w.upper() for w in _WORD_RE.findall(name or "")]


def _candidate_stems(name: str) -> Iterator[str]:
    """Yield increasingly specific, deterministic, name-derived code stems
    (most concise first). The caller appends `_CO` and returns the first stem
    whose code is free. Mirrors `vendor_code_service._candidate_stems`."""
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


def generate_customer_code(name: str, session: Session) -> str:
    """Generate a short, readable, UNIQUE customer code from `name`. Tries
    name-derived stems (word initials, then longer forms) and returns the
    first whose `<stem>_CO` code isn't already taken. If every readable stem
    collides (extremely unlikely), falls back to a deterministic short hash of
    the name -- still name-derived and unique, never a blind `_2`/`_3`.

    Call this ONLY when onboarding a genuinely new customer -- never to
    re-derive an existing customer's code."""
    for stem in _candidate_stems(name):
        code = f"{stem}{_CODE_SUFFIX}"
        if get_customer_by_code(code, session) is None:
            return code

    # Deterministic name-derived fallback (guarantees termination + uniqueness).
    base = next(_candidate_stems(name), "XX")
    digest = hashlib.sha1((name or "").strip().lower().encode("utf-8")).hexdigest().upper()
    for index in range(len(digest) - 1):
        code = f"{base}{digest[index:index + 2]}{_CODE_SUFFIX}"
        if get_customer_by_code(code, session) is None:
            return code

    raise RuntimeError(f"Unable to generate a unique customer code for {name!r}.")


def parse_customer_code_from_filename(filename: str) -> str | None:
    """Returns the leading customer-code prefix (e.g. "AB_CO", "AMA_CO") from
    a filename like "AB_CO_Order.xlsx", or `None` if the filename has no
    code-shaped prefix at all. Case-insensitive."""
    match = _CODE_PREFIX_PATTERN.match(filename.strip().upper())
    return match.group(1) if match else None


def get_customer_by_code(code: str, session: Session) -> Customer | None:
    return session.execute(
        select(Customer).where(func.upper(Customer.customer_code) == code.upper())
    ).scalar_one_or_none()
