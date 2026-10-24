"""Shared parsing and normalization utilities across pension providers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_PAREN_SUFFIX_RE = re.compile(r"\s*\(([^()]+)\)\s*$")
_DIGITS_ONLY_RE = re.compile(r"\D+")


def split_policy_id(raw_policy_id: str) -> tuple[str, str | None]:
    """Split a display policy ID like ``'001-234-567890 (01234567)'`` into
    ``(normalized_policy_id, internal_account_id)``.

    HaPhoenix and several other Israeli providers display a parenthesized internal
    account number whose zero-padding can change over time. Stripping the parenthesized
    suffix produces a stable ``policy_id`` while preserving ``internal_account_id``.
    """
    text = (raw_policy_id or "").strip()
    match = _PAREN_SUFFIX_RE.search(text)
    if not match:
        return text, None
    base = text[: match.start()].strip()
    internal = match.group(1).strip()
    return (base or text), (internal or None)


def policy_id_key(raw_policy_id: str) -> str:
    """Return a reformat-insensitive key for deduplicating deposit identifiers."""
    base, _ = split_policy_id(raw_policy_id)
    return base.replace(" ", "")


def normalize_israeli_id(id_number: str) -> str:
    """Zero-pad an Israeli ID number to 9 digits."""
    digits = _DIGITS_ONLY_RE.sub("", str(id_number or ""))
    if not digits:
        raise ValueError("ID number must contain digits")
    return digits.zfill(9)


def normalize_israeli_phone(phone: str) -> str:
    """Normalize an Israeli mobile number to local 10-digit ``05XXXXXXXX`` format."""
    digits = _DIGITS_ONLY_RE.sub("", str(phone or ""))
    if digits.startswith("972") and len(digits) == 12:
        digits = "0" + digits[3:]
    elif len(digits) == 9 and digits.startswith("5"):
        digits = "0" + digits
    return digits


def extract_number(val: Any, default: float | None = 0.0) -> float | None:
    """Extract a float from a plain number, percentage string, or ``{'value': x}`` dict."""
    if val is None or val == "":
        return default
    if isinstance(val, dict):
        if "percentageData" in val and isinstance(val["percentageData"], dict):
            raw = val["percentageData"].get("value")
        else:
            raw = val.get("value")
    else:
        raw = val
    if raw is None or raw == "":
        return default
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        cleaned = (
            raw.replace("%", "")
            .replace("₪", "")
            .replace('ש"ח', "")
            .replace("ש''ח", "")
            .replace("שח", "")
            .replace(",", "")
            .strip()
        )
        if not cleaned:
            return default
        try:
            return float(cleaned)
        except ValueError:
            m = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
            if m:
                return float(m.group(0))
            return default
    return default


def parse_il_date(value: str | None) -> str | None:
    """Parse common Israeli provider date formats into ISO ``YYYY-MM-DD``.

    Supports:
    - ``YYYY-MM-DD`` or ISO timestamps ``YYYY-MM-DDTHH:MM:SS...``
    - ``DD.MM.YYYY`` (e.g. ``20.10.2026``)
    - ``DD.MM.YY`` (e.g. ``06.09.26``)
    - ``DD/MM/YYYY``
    - ``MM.YYYY`` or ``MM/YYYY`` -> ``YYYY-MM-01``
    - Embedded date inside Hebrew text (e.g. ``החל מ31.01.2031``)
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith("0001-"):
        return None

    # ISO timestamp or date
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]

    # Exact DD.MM.YYYY or DD/MM/YYYY
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d.%m.%y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # MM.YYYY or MM/YYYY (used in monthly accumulation graphs)
    m_month = re.match(r"^(\d{2})[./](\d{4})$", text)
    if m_month:
        month, year = m_month.groups()
        return f"{year}-{month}-01"

    # Embedded DD.MM.YYYY or DD/MM/YYYY in Hebrew text
    m_embedded = re.search(r"(\d{2})[./](\d{2})[./](\d{4})", text)
    if m_embedded:
        day, month, year = m_embedded.groups()
        return f"{year}-{month}-{day}"

    return None


def parse_salary_month(value: str | None) -> str | None:
    """Normalize a salary month like ``'08/2026'`` or ``'2026-08'`` to ``'YYYY-MM'``."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    m = re.match(r"^(\d{2})[./-](\d{4})$", text)
    if m:
        month, year = m.groups()
        return f"{year}-{month}"
    m_iso = re.match(r"^(\d{4})-(\d{2})", text)
    if m_iso:
        year, month = m_iso.groups()
        return f"{year}-{month}"
    return None
