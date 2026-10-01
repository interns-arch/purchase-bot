"""Reading a PHOTO of a stock list (or a scanned PDF page) into text lines.

The model's only job is to transcribe "PART QUANTITY" lines off the image.
What happens to those lines is decided elsewhere and is never the model's
call:
  * they are read by the SAME typed-list reader a WhatsApp message goes
    through (`vendor_text_stock.parse_stock_text`) -- a line without a
    quantity is dropped and reported, never imported as 1;
  * the vendor is shown every line and must reply "haan" before anything
    is imported (`photo_stock`). A misread digit is caught by the person who
    knows his own stock, not by us.

Model choice, measured 1 Oct 2026 on a synthetic photographed list of 12
rows, through this account's key: meta/llama-3.2-90b-vision-instruct read
12/12 exactly in ~14 s; meta/llama-3.2-11b-vision-instruct 11/12 in ~4 s
(one row missing, nothing wrong or invented). Gemma 3 and Phi-3 Vision are
listed but not enabled for the account (HTTP 404). 90b is the default, 11b
the fallback.

Never raises: any failure returns (None, reason).
"""

from __future__ import annotations

import base64
import io
import os

import httpx

from backend.app.core.config import settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
MODEL = os.environ.get("AI_VISION_MODEL", "meta/llama-3.2-90b-vision-instruct").strip()
FALLBACK_MODEL = os.environ.get("AI_VISION_FALLBACK_MODEL", "meta/llama-3.2-11b-vision-instruct").strip()
# Per model. Measured live 1 Oct 2026: the 90b endpoint sometimes queues past
# 2 minutes while 11b answers in seconds -- a vendor should not wait that
# long, so the first model gets 45 s and then the fallback is tried.
TIMEOUT_SECONDS = float(os.environ.get("AI_VISION_TIMEOUT_SECONDS", "45"))
ENABLED = os.environ.get("AI_VISION_ENABLED", "true").strip().lower() == "true"
# Inline images are kept small: NVIDIA's hosted endpoints refuse large inline
# payloads, and a stock list stays legible well below this.
MAX_IMAGE_BYTES = 170_000
MAX_SIDE_PX = 1600

PROMPT = (
    "This is a photo of an auto-parts stock list. Read EVERY row of the table. "
    "Reply with ONLY one line per row, in the form: PART_NUMBER QUANTITY. "
    "Copy each part number exactly, character by character, including dashes. "
    "The quantity is the stock / closing stock / qty column -- never MRP, rate, "
    "price or amount. Do not add headings, totals or explanations. Do not guess: "
    "if a part number or a quantity cannot be read, write ? in its place."
)


def is_configured() -> bool:
    from backend.app.ai import llm

    return ENABLED and (llm.is_configured("gemini") or bool(settings.nvidia_api_key or settings.ai_api_key))


def shrink(image_bytes: bytes) -> bytes:
    """A JPEG small enough to send inline, still legible. Never upscales."""
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image)  # phone photos carry their rotation in EXIF
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    side = MAX_SIDE_PX
    quality = 82
    while True:
        copy = image.copy()
        copy.thumbnail((side, side))
        buffer = io.BytesIO()
        copy.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= MAX_IMAGE_BYTES or side <= 700:
            return data
        if quality > 55:
            quality -= 9
        else:
            side = int(side * 0.85)


def pdf_pages_as_images(pdf_bytes: bytes, max_pages: int = 5) -> list[bytes]:
    """A scanned PDF's pages as PNG images (first `max_pages` only)."""
    import pypdfium2

    document = pypdfium2.PdfDocument(pdf_bytes)
    pages = []
    try:
        for index in range(min(len(document), max_pages)):
            bitmap = document[index].render(scale=2.0)
            buffer = io.BytesIO()
            bitmap.to_pil().save(buffer, format="PNG")
            pages.append(buffer.getvalue())
    finally:
        document.close()
    return pages


def _ask(model: str, jpeg: bytes) -> str | None:
    key = settings.nvidia_api_key or settings.ai_api_key
    base = (settings.nvidia_base_url or DEFAULT_BASE_URL).rstrip("/")
    response = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        json={
            "model": model,
            "max_tokens": 4096,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
                    ],
                }
            ],
        },
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        logger.warning("Vision model %s answered HTTP %s.", model, response.status_code)
        return None
    content = (response.json().get("choices") or [{}])[0].get("message", {}).get("content")
    return content if isinstance(content, str) and content.strip() else None


_GEMINI_SYSTEM = (
    "You transcribe a photo of an auto-parts stock list. Reply with JSON only: "
    '{"rows": [{"part": "<part number exactly as printed>", "quantity": <stock number or null>}]}. '
    "One entry per table row. The quantity is the stock / closing stock / qty column -- never MRP, "
    "rate, price or amount. If a part number or quantity cannot be read, use null. Do not guess."
)


def _gemini_lines(jpeg: bytes) -> str | None:
    """Gemini first (the sales bot's vision model), as PART QTY lines; a row it
    could not read becomes 'PART ?', which the photo flow reports, never imports."""
    from backend.app.ai import llm

    if not llm.is_configured("gemini"):
        return None
    data, provider = llm.ask_json(_GEMINI_SYSTEM, "Read the stock list in this photo.", purpose="vision", image=(jpeg, "image/jpeg"))
    if provider != "gemini" or not data or not isinstance(data.get("rows"), list):
        return None
    lines = []
    for row in data["rows"]:
        if not isinstance(row, dict) or not row.get("part"):
            continue
        quantity = row.get("quantity")
        lines.append(f"{row['part']} {quantity if quantity not in (None, '') else '?'}")
    return "\n".join(lines) or None


def read_stock_image(image_bytes: bytes) -> tuple[str | None, str]:
    """The lines read off one image, and which model read them (or why not)."""
    from backend.app.ai import llm

    if not (ENABLED and (llm.is_configured("gemini") or settings.nvidia_api_key or settings.ai_api_key)):
        return None, "photo reading is not configured (AI_VISION_ENABLED / GEMINI_API_KEY / NVIDIA_API_KEY)"
    try:
        jpeg = shrink(image_bytes)
    except Exception as exc:  # noqa: BLE001 -- an unreadable image is a reason, not a crash
        return None, f"the image could not be opened ({exc})"
    try:
        text = _gemini_lines(jpeg)
    except Exception:  # noqa: BLE001
        text = None
    if text:
        return text, "gemini:" + llm.GEMINI_MODELS["vision"]
    if not (settings.nvidia_api_key or settings.ai_api_key):
        return None, "Gemini could not read the image"
    for model in [MODEL, FALLBACK_MODEL]:
        if not model:
            continue
        try:
            text = _ask(model, jpeg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vision model %s failed: %s", model, exc)
            continue
        if text:
            return text, model
    return None, "no vision model could read the image"
