"""Photo uploads: JPEG/PNG → PDF conversion at the upload boundary.

A phone photo is a legitimate customs document, but nothing downstream of the
upload gate knows what an image is — OCR envelope reuse, the evidence viewer,
its ``#page=N`` deep links and the content-type defaults all assume "a stored
document is a PDF".  Converting here keeps that invariant instead of teaching
four subsystems a second format.  The conversion is lossless: img2pdf embeds
the original JPEG/PNG bytes into the PDF container without recompressing, so
OCR reads exactly the pixels the camera produced.

Several photos merge into ONE multi-page PDF (one photo per page): a 3-page
invoice photographed page by page is one document, and uploading the pages as
three separate documents would declare three invoices — the document-boundary
failure mode this project has been bitten by before.
"""
from __future__ import annotations

import io

from .config import get_settings
from .domain.errors import BlockingValidationError

# ISO-BMFF brands iPhones stamp on HEIC/HEIF photos (offset 4 is "ftyp").
_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"heif", b"mif1", b"msf1")


class _PhotoTooLarge(Exception):
    """Internal: a photo refused on pixel count before it was ever decoded."""


def image_kind(data: bytes) -> str | None:
    """The accepted photo formats, by magic bytes: 'jpeg', 'png' or None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def looks_like_heic(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS


def _flatten_alpha(img) -> bytes:
    """PNG with transparency onto white — img2pdf refuses alpha channels, and
    PDF viewers/OCR would otherwise render the transparent regions black."""
    from PIL import Image

    rgba = img.convert("RGBA")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.getchannel("A"))
    buf = io.BytesIO()
    flat.save(buf, format="PNG")
    return buf.getvalue()


def photos_to_pdf(photos: list[tuple[str, bytes]]) -> bytes:
    """One multi-page PDF from JPEG/PNG photos, one photo per page, in order.

    Every photo must already have passed the upload gate (accepted image kind,
    size, non-empty).  What is checked HERE is what needs the decoded pixels:
    that the file decodes at all, and that it holds enough resolution to OCR —
    a blurry 640px photo of an invoice extracts confidently wrong numbers into
    a legal declaration, which is worse than this refusal.
    """
    try:
        import img2pdf
        from PIL import Image
    except ImportError:
        raise BlockingValidationError(
            "IMAGE_SUPPORT_UNAVAILABLE",
            "Photo uploads need the 'img2pdf' and 'Pillow' packages on the server — "
            "run `pip install -r requirements.txt` and retry, or attach a PDF instead.",
            scope="DOCUMENT")

    settings = get_settings()
    min_px = settings.min_photo_px
    # Pillow's own guard only ERRORS above 2 x MAX_IMAGE_PIXELS (~179M); between
    # ~89M and that it emits a WARNING and decodes anyway.  A 25 MB PNG can sit
    # inside that band at ~170M pixels, which is ~500 MB as RGB — and the alpha
    # path below converts to RGBA and composites onto a fresh RGB, roughly
    # tripling it.  With no cap on how many photos one request may carry, that
    # is the cheapest way to exhaust this server's memory.  Refuse the band
    # rather than decode it: no phone produces a page photo this large.
    max_px = settings.max_photo_pixels
    pages: list[bytes] = []
    if settings.max_photos_per_document and len(photos) > settings.max_photos_per_document:
        raise BlockingValidationError(
            "TOO_MANY_PHOTOS",
            f"{len(photos)} photos were sent as one document — the limit is "
            f"{settings.max_photos_per_document}. Attach the remaining pages as a second "
            f"document, or scan the whole thing to a PDF.", scope="DOCUMENT")
    for filename, data in photos:
        try:
            img = Image.open(io.BytesIO(data))
            w, h = img.size                 # header only — before any decode
            if max_px and w * h > max_px:
                raise _PhotoTooLarge(f"{w}x{h}")
            img.load()                      # full decode: catches truncated files now,
        except _PhotoTooLarge as e:
            raise BlockingValidationError(
                "PHOTO_TOO_LARGE",
                f"{filename!r} is {e} pixels, past the {max_px:,}-pixel decode limit. "
                f"Re-take or re-export it at a normal camera resolution.", scope="DOCUMENT")
        except Exception:                   # not as a FAILED extraction after paid OCR
            raise BlockingValidationError(
                "UNREADABLE_IMAGE",
                f"{filename!r} could not be decoded as an image — the file is damaged or "
                f"was cut off in transfer. Re-take or re-export the photo and attach it again.",
                scope="DOCUMENT")
        # EXIF orientation swaps the effective edges; the gate should judge the
        # resolution of the page as read, not as stored.
        w, h = img.size
        if min(w, h) < min_px:
            raise BlockingValidationError(
                "PHOTO_RESOLUTION_TOO_LOW",
                f"{filename!r} is {w}×{h}px — below the {min_px}px minimum short edge needed "
                f"to read the numbers reliably. Retake the photo closer to the document, in "
                f"good light, at the camera's full resolution.",
                scope="DOCUMENT")
        has_alpha = ("A" in img.getbands()) or (img.mode == "P" and "transparency" in img.info)
        pages.append(_flatten_alpha(img) if has_alpha else data)
    # ifvalid: apply the EXIF rotation phones record (the pixels are stored
    # sideways), ignore a malformed orientation tag instead of refusing.
    return img2pdf.convert(pages, rotation=img2pdf.Rotation.ifvalid)
