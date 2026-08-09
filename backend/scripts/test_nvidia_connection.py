"""Standalone NVIDIA connectivity check -- run this BEFORE enabling the LLM
fallback anywhere.

    python -m backend.scripts.test_nvidia_connection

It verifies, in order:
  1. NVIDIA_API_KEY is present (masked in output -- the key is never printed).
  2. The account can list models, and whether the configured AI_MODEL exists.
  3. The model answers a trivial prompt with strict JSON.
  4. The model can normalize a realistic messy inventory sample end-to-end,
     through the real provider + the Phase 1 strict validator.

Touches nothing else: no database, no imports, no business logic.
Exit code 0 = usable, 1 = not usable (with the reason printed).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before reading settings, exactly like the app does.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.app.ai.compact import compact_grid  # noqa: E402
from backend.app.ai.provider import UnderstandRequest  # noqa: E402
from backend.app.ai.providers.nvidia import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    NvidiaDocumentProvider,
)
from core.services.normalized_validation import validate_normalized_document  # noqa: E402

SAMPLE_GRID = [
    ["Part search Details"],
    [""],
    ["Part Num", "Root Part", "Part Descr", "MRP", "Current St", "Float Stock", "Bin"],
    ["A-81550-0K250", "RP1", "LAMP RR COMBINATION", "1499", "5", "2", "B1"],
    ["A-04465-0D130", "RP2", "PAD KIT, BRAKE ,FR", "2250", "4", "0", "B2"],
    ["01550-06207", "RP3", "BOLT", "88", "7", "1", "C3"],
]


def _mask(value: str) -> str:
    return f"{value[:4]}…{value[-4:]} (len={len(value)})" if len(value) > 10 else "(too short)"


def main() -> int:
    api_key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("AI_API_KEY")
    model = os.environ.get("AI_MODEL") or DEFAULT_MODEL
    base_url = os.environ.get("NVIDIA_BASE_URL") or DEFAULT_BASE_URL

    print("=" * 68)
    print("NVIDIA connection test")
    print("=" * 68)
    print(f"  base_url : {base_url}")
    print(f"  model    : {model}")

    # --- 1. key present ---------------------------------------------------
    if not api_key:
        print("  api_key  : NOT SET")
        print("\nFAIL: set NVIDIA_API_KEY in backend/.env (or the environment).")
        print("      The application is unaffected -- it falls back to NullProvider.")
        return 1
    print(f"  api_key  : {_mask(api_key)}")

    provider = NvidiaDocumentProvider(api_key, model=model, base_url=base_url, timeout_seconds=60)

    # --- 2. model catalogue ----------------------------------------------
    print("\n[2] Listing models available to this account…")
    models = provider.list_models()
    if models is None:
        print("    Could not list models (endpoint refused or unreachable).")
    else:
        print(f"    {len(models)} model(s) visible.")
        if model in models:
            print(f"    OK: {model!r} is available.")
        else:
            print(f"    WARNING: {model!r} was NOT in the catalogue.")
            close = [m for m in models if "gemma" in m.lower()]
            if close:
                print("    Gemma models this account can use:")
                for name in sorted(close):
                    print(f"      - {name}")
            else:
                print("    (no Gemma models visible; showing first 15)")
                for name in sorted(models)[:15]:
                    print(f"      - {name}")

    # --- 3. trivial JSON round trip ---------------------------------------
    print("\n[3] Round-tripping a trivial JSON prompt…")
    ok, message = provider.test_connection()
    print(f"    {'OK' if ok else 'FAIL'}: {message}")
    if not ok:
        print("\nFAIL: the model is not usable. Application behaviour is unchanged.")
        return 1

    # --- 4. realistic extraction + strict validation -----------------------
    print("\n[4] Extracting a messy inventory sample (metadata rows + MRP + Float Stock)…")
    compact = compact_grid(SAMPLE_GRID, file_name="SAMPLE_VENDOR.xlsx")
    document = provider.understand_document(
        UnderstandRequest(
            document_type="vendor_inventory",
            compact_text=compact,
            file_name="SAMPLE_VENDOR.xlsx",
        )
    )
    if document is None:
        print("    FAIL: provider returned None (see logged reason).")
        return 1

    mapping = document.meta.column_mapping
    print(f"    column_mapping : {mapping}")
    print(f"    confidence     : {document.meta.confidence}")
    print(f"    rows extracted : {len(document.rows)}")
    for row in document.rows[:5]:
        print(f"      {row.part_number!r} -> qty={row.available_quantity!r}")

    result = validate_normalized_document(document, minimum_confidence=0.0)
    print(f"\n    validator: {'ACCEPTED' if result.is_valid else 'REJECTED'}")
    if not result.is_valid:
        print(f"    reason   : {result.reason}")

    expected = {"A-81550-0K250": "5", "A-04465-0D130": "4", "01550-06207": "7"}
    got = {r.part_number: str(r.quantity).rstrip("0").rstrip(".") for r in result.rows}
    correct = all(got.get(part) == qty for part, qty in expected.items())
    print(f"    quantities correct (from 'Current St', not MRP/Float Stock): {correct}")
    if not correct:
        print(f"      expected {expected}")
        print(f"      got      {got}")

    print("\n" + "=" * 68)
    verdict = ok and result.is_valid and correct
    print("RESULT:", "USABLE for document understanding." if verdict else "NOT reliable yet — review above.")
    print("=" * 68)
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
