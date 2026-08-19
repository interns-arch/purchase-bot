"""Founder-declared part-number equivalences.

The Founder's rule (18 Aug 2026): "MF390300ML32 and MF390300ML are the same
part -- treat both numbers as the same."

Nothing in the automatic pipeline can work this out safely. Part-number
normalisation deliberately only removes special characters, so 'ML32' and
'ML33' stay different parts -- which is correct, they are different items.
`PartAlias` only learns the pairs a vendor prints side by side in one row.
An equivalence like this one is a business fact, so it is only ever created
by a human saying so (`backend/scripts/link_part_numbers.py`), never guessed.

Once declared, the link is honoured everywhere a part is matched: vendor
comparison, automatic allocation and its reservation ledger, top-up, the
stock-gap table and part intelligence -- because all of them resolve numbers
through `vendor_selection_service._matchable_part_numbers` or the comparison
offer index, and both consult this table.

Pure business logic -- no print()/input() here."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.ingestion.column_detector import normalise_part_number
from core.models import PartNumberLink


def linked_numbers(numbers: Iterable[str], session: Session) -> set[str]:
    """Every normalised number declared equivalent to any of `numbers`
    (including the inputs' own group members). Empty set when none of them
    take part in a declared equivalence -- the overwhelmingly common case,
    answered by one indexed query."""
    wanted = {number for number in numbers if number}
    if not wanted:
        return set()
    groups = set(
        session.execute(
            select(PartNumberLink.group_key).where(
                PartNumberLink.normalized_part_number.in_(wanted)
            )
        ).scalars()
    )
    if not groups:
        return set()
    return set(
        session.execute(
            select(PartNumberLink.normalized_part_number).where(
                PartNumberLink.group_key.in_(groups)
            )
        ).scalars()
    )


def apply_links_to_index(index: dict, session: Session) -> None:
    """Give an existing part-number lookup index the founder's equivalences
    as extra keys, in place. A number already in the index keeps its own
    entry (canonical/alias matches stay authoritative); an equivalent number
    that is not in the index is pointed at the entry its partner already has,
    so ordering by either spelling finds the same offers."""
    if not index:
        return
    equivalents = linked_numbers(index.keys(), session)
    missing = equivalents - set(index)
    if not missing:
        return
    for number in missing:
        for partner in linked_numbers([number], session):
            entry = index.get(partner)
            if entry is not None:
                index.setdefault(number, entry)
                break


def link_part_numbers(
    first: str, second: str, session: Session, *, declared_by: str | None = None
) -> dict:
    """Declare two part numbers to be the same part. Idempotent, and safe to
    call in any order: linking A=B then B=C puts all three in one group, and
    linking two numbers that are already in DIFFERENT groups merges those
    groups rather than splitting the part in two.

    Returns a small summary of what changed (for the caller to report)."""
    left = normalise_part_number(first)
    right = normalise_part_number(second)
    if not left or not right:
        raise ValueError("Both part numbers must contain letters or digits.")
    if left == right:
        return {"action": "identical", "group": left, "numbers": [left]}

    rows = {
        row.normalized_part_number: row
        for row in session.execute(
            select(PartNumberLink).where(
                PartNumberLink.normalized_part_number.in_([left, right])
            )
        ).scalars()
    }
    left_row = rows.get(left)
    right_row = rows.get(right)

    if left_row is None and right_row is None:
        group_key = left
        session.add(
            PartNumberLink(
                normalized_part_number=left, group_key=group_key, declared_by=declared_by
            )
        )
        session.add(
            PartNumberLink(
                normalized_part_number=right, group_key=group_key, declared_by=declared_by
            )
        )
        action = "created"
    elif left_row is not None and right_row is None:
        group_key = left_row.group_key
        session.add(
            PartNumberLink(
                normalized_part_number=right, group_key=group_key, declared_by=declared_by
            )
        )
        action = "joined"
    elif left_row is None and right_row is not None:
        group_key = right_row.group_key
        session.add(
            PartNumberLink(
                normalized_part_number=left, group_key=group_key, declared_by=declared_by
            )
        )
        action = "joined"
    elif left_row.group_key == right_row.group_key:
        return {
            "action": "already_linked",
            "group": left_row.group_key,
            "numbers": sorted(linked_numbers([left], session)),
        }
    else:
        # Two existing groups become one -- never leave a part split.
        group_key = left_row.group_key
        session.execute(
            update(PartNumberLink)
            .where(PartNumberLink.group_key == right_row.group_key)
            .values(group_key=group_key)
        )
        action = "merged"

    session.flush()
    return {
        "action": action,
        "group": group_key,
        "numbers": sorted(linked_numbers([left], session)),
    }


def list_links(session: Session) -> list[tuple[str, list[str]]]:
    """[(group_key, [numbers])] for every declared equivalence."""
    groups: dict[str, list[str]] = {}
    for row in session.execute(
        select(PartNumberLink).order_by(PartNumberLink.group_key, PartNumberLink.id)
    ).scalars():
        groups.setdefault(row.group_key, []).append(row.normalized_part_number)
    return sorted(groups.items())
