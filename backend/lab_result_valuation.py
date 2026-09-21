"""Background, conservative price estimates for laboratory-result PDFs.

The admin report must never download or parse PDFs while it is being opened.
This module stores a cached estimate when a user receives result documents.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import database as db


MAX_PDF_BYTES = 15 * 1024 * 1024
VALUATION_VERSION = "v3-ocr-rus-eng-deduplicate-packages"
# OCR is CPU-intensive. A single worker keeps the web service responsive on a
# small shared server even when several result documents arrive together.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lab-value")
_scheduled: set[tuple[str, str]] = set()
_scheduled_lock = threading.Lock()


ANALYTE_ALIASES: dict[str, tuple[str, ...]] = {
    "ferritin": ("ферритин", "ferritin"),
    "iron": ("железо сывороточное", "сывороточное железо", "железо", "iron"),
    "tsh": ("тиреотропный гормон", "ттг", "tsh"),
    "vitamin_d": ("25 oh витамин d", "25 он витамин d", "витамин d", "vitamin d"),
    "alt": ("аланинаминотрансфераза", "алт", "alt"),
    "ast": ("аспартатаминотрансфераза", "аст", "ast"),
    "total_protein": ("общий белок", "белок общий", "total protein"),
    "free_t3": ("т3 свободный", "свободный т3", "св т3", "free t3"),
    "free_t4": ("т4 свободный", "свободный т4", "св т4", "free t4"),
    "crp": ("с реактивный белок", "срб", "crp"),
    "estradiol": ("эстрадиол", "estradiol"),
    "testosterone": ("тестостерон", "testosterone"),
    "triglycerides": ("триглицериды", "triglycerides"),
    "hdl": ("липопротеины высокой плотности", "холестерин лпвп", "лпвп", "hdl"),
    "ldl": ("липопротеины низкой плотности", "холестерин лпнп", "лпнп", "ldl"),
    "creatinine": ("креатинин", "creatinine"),
    "bilirubin_total": ("билирубин общий", "общий билирубин", "total bilirubin"),
    "bilirubin_direct": ("билирубин прямой", "прямой билирубин", "bilirubin direct"),
    "amylase": ("альфа амилаза", "амилаза панкреатическая", "амилаза", "amylase"),
    "alkaline_phosphatase": ("щелочная фосфатаза", "alkaline phosphatase"),
    "urea": ("мочевина", "urea"),
    "albumin": ("альбумин", "albumin"),
    "uric_acid": ("мочевая кислота", "uric acid"),
    "rheumatoid_factor": ("ревматоидный фактор", "рф", "rheumatoid factor"),
    "anti_tpo": ("антитела к тиреоидной пероксидазе", "ат тпо", "anti tpo"),
    "anti_tg": ("антитела к тиреоглобулину", "ат тг", "anti tg"),
    "fsh": ("фолликулостимулирующий гормон", "фсг", "fsh"),
    "lh": ("лютеинизирующий гормон", "лг", "lh"),
    "prolactin": ("пролактин", "prolactin"),
    "progesterone": ("прогестерон", "progesterone"),
    "psa_total": ("пса общий", "простат специфический антиген общий", "psa total"),
    "psa_free": ("пса свободный", "простат специфический антиген свободный", "psa free"),
    "ca125": ("са 125", "ca 125"),
    "ca153": ("са 15 3", "ca 15 3"),
    "ca199": ("са 19 9", "ca 19 9"),
}


def _normalize(value: object) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^a-zа-я0-9]+", " ", text).split())


def recognized_analytes(text: str) -> set[str]:
    normalized = f" {_normalize(text)} "
    found: set[str] = set()
    for analyte, aliases in ANALYTE_ALIASES.items():
        if any(f" {_normalize(alias)} " in normalized for alias in aliases):
            found.add(analyte)
    return found


def _catalog_analytes(item: dict) -> set[str]:
    return recognized_analytes(str(item.get("includes") or ""))


def estimate_catalog_value(texts: list[str], catalog: list[dict]) -> dict:
    """Return a conservative closest-package estimate from extracted text."""
    detected_per_document = [recognized_analytes(text) for text in texts if text.strip()]
    detected = set().union(*detected_per_document) if detected_per_document else set()
    candidates = []
    for item in catalog:
        expected = _catalog_analytes(item)
        if not expected:
            continue
        overlap = expected & detected
        coverage = len(overlap) / len(expected)
        minimum = 1 if len(expected) == 1 else 2
        if len(overlap) >= minimum and coverage >= 0.6:
            candidates.append({
                "id": str(item.get("id") or ""),
                "price": max(0, int(item.get("price") or 0)),
                "expected": expected,
                "overlap": overlap,
                "coverage": coverage,
            })

    # Prefer the most complete and broad matching package. Add another package
    # only when it explains at least two analytes not covered by earlier picks.
    candidates.sort(
        key=lambda item: (item["coverage"], len(item["overlap"]), len(item["expected"]), -item["price"]),
        reverse=True,
    )
    selected = []
    explained: set[str] = set()
    for item in candidates:
        new = item["overlap"] - explained
        if selected and len(new) < 2:
            continue
        if selected and len(new) / max(1, len(item["expected"])) < 0.4:
            continue
        selected.append(item)
        explained.update(item["overlap"])

    # A broader package may be selected after its fully matched basic version
    # (for example when one sex-specific marker is intentionally absent from
    # the result).  The basic package is already contained in the broader one,
    # so charging both would double-count the same analyses.
    selected = [
        item for item in selected
        if not any(
            item["expected"] < other["expected"]
            and item["overlap"] <= other["overlap"]
            for other in selected
        )
    ]

    confidence = (
        sum(item["coverage"] * len(item["expected"]) for item in selected)
        / sum(len(item["expected"]) for item in selected)
        if selected else 0.0
    )
    return {
        "estimated_amount": sum(item["price"] for item in selected),
        "confidence": round(confidence, 4),
        "matched_exam_ids": [item["id"] for item in selected],
        "recognized_analytes": sorted(detected),
    }


def extract_pdf_text(payload: bytes) -> str:
    if not payload.startswith(b"%PDF-"):
        raise ValueError("Документ не является PDF")
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(payload), strict=False)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_pdf_text_with_ocr(payload: bytes) -> tuple[str, bool]:
    """Read embedded text first; OCR up to eight pages only when it is absent."""
    text = extract_pdf_text(payload)
    if len(_normalize(text)) >= 80:
        return text, False
    pdftoppm = shutil.which("pdftoppm")
    tesseract = shutil.which("tesseract")
    if not pdftoppm or not tesseract:
        return text, False
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="consilium-lab-ocr-") as temp_dir:
        source = Path(temp_dir) / "source.pdf"
        prefix = Path(temp_dir) / "page"
        source.write_bytes(payload)
        subprocess.run(
            [
                pdftoppm, "-f", "1", "-l", "8", "-r", "150", "-png",
                str(source), str(prefix),
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=90,
        )
        pages = sorted(Path(temp_dir).glob("page-*.png"), key=lambda path: path.name)[:8]
        ocr_parts = []
        for page in pages:
            result = subprocess.run(
                [tesseract, str(page), "stdout", "-l", "rus+eng", "--psm", "6"],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=45,
            )
            ocr_parts.append(result.stdout.decode("utf-8", errors="replace"))
        ocr_text = "\n".join(ocr_parts).strip()
    if len(_normalize(ocr_text)) > len(_normalize(text)):
        return ocr_text, True
    return text, False


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Разрешены только публичные HTTPS-ссылки")
    for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError("Ссылка ведёт во внутреннюю сеть")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_pdf(url: str, timeout: float = 15.0) -> bytes:
    _validate_public_url(url)
    request = Request(url, headers={"User-Agent": "ConsiliumLabValue/1.0", "Accept": "application/pdf"})
    with build_opener(_SafeRedirectHandler()).open(request, timeout=timeout) as response:
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > MAX_PDF_BYTES:
            raise ValueError("PDF превышает допустимый размер")
        payload = response.read(MAX_PDF_BYTES + 1)
    if len(payload) > MAX_PDF_BYTES:
        raise ValueError("PDF превышает допустимый размер")
    return payload


def document_fingerprint(documents: list[dict]) -> str:
    urls = sorted(str(item.get("analysis_url") or item.get("url") or "") for item in documents)
    return hashlib.sha256(
        (VALUATION_VERSION + "\n" + "\n".join(urls)).encode("utf-8")
    ).hexdigest()


def calculate_and_store(chel_id: str, med_id: str, documents: list[dict]) -> dict:
    fingerprint = document_fingerprint(documents)
    texts = []
    errors = []
    ocr_document_count = 0
    for document in documents:
        try:
            url = str(document.get("analysis_url") or document.get("url") or "")
            text, used_ocr = extract_pdf_text_with_ocr(download_pdf(url))
            texts.append(text)
            ocr_document_count += int(used_ocr)
        except Exception as exc:  # one bad document must not discard the rest
            errors.append(str(exc)[:160])
    valuation = estimate_catalog_value(texts, db.list_examinations())
    analyzed = len(texts)
    status = "ready" if analyzed == len(documents) and valuation["matched_exam_ids"] else (
        "partial" if analyzed and valuation["matched_exam_ids"] else
        "unrecognized" if analyzed else "error"
    )
    return db.save_lab_result_value_estimate(chel_id, {
        "med_id": med_id,
        "document_fingerprint": fingerprint,
        "status": status,
        "document_count": len(documents),
        "analyzed_document_count": analyzed,
        "ocr_document_count": ocr_document_count,
        "error": "; ".join(errors),
        **valuation,
    })


def schedule_estimate(chel_id: str, med_id: str, documents: list[dict]) -> bool:
    if not chel_id or not documents:
        return False
    fingerprint = document_fingerprint(documents)
    existing = db.get_lab_result_value_estimate(chel_id)
    if existing and existing.get("document_fingerprint") == fingerprint and existing.get("status") in {
        "ready", "partial", "unrecognized",
    }:
        return False
    key = (chel_id, fingerprint)
    with _scheduled_lock:
        if key in _scheduled:
            return False
        _scheduled.add(key)
    db.save_lab_result_value_estimate(chel_id, {
        "med_id": med_id, "document_fingerprint": fingerprint,
        "status": "pending", "document_count": len(documents),
    })

    def work() -> None:
        try:
            calculate_and_store(chel_id, med_id, documents)
        except Exception as exc:
            db.save_lab_result_value_estimate(chel_id, {
                "med_id": med_id, "document_fingerprint": fingerprint,
                "status": "error", "document_count": len(documents),
                "error": str(exc)[:500],
            })
        finally:
            with _scheduled_lock:
                _scheduled.discard(key)

    _executor.submit(work)
    return True
