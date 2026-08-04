"""Reading contacts, messages and call logs through Android's content providers.

``adb shell content query`` runs as the *shell* user, which on stock builds
holds READ_CONTACTS / READ_SMS / READ_CALL_LOG. That is what makes a
PC-side export possible with no app installed on the phone. It is not
universal - some builds (and most of Android 14+ on some OEMs) restrict it -
so every reader here reports failure cleanly and the caller falls back to the
companion app.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# `content query` prints: "Row: 0 _id=1, address=+44700900000, body=hi, there"
# Values can contain commas, so split only where the next token looks like a
# new column name.
_ROW_SPLIT = re.compile(r",\s+(?=[A-Za-z_][A-Za-z0-9_]*=)")
_ROW_PREFIX = re.compile(r"^Row:\s*\d+\s*")

CONTACTS_URI = "content://com.android.contacts/contacts"
VCARD_URI = "content://com.android.contacts/contacts/as_vcard/"
SMS_URI = "content://sms"
CALLS_URI = "content://call_log/calls"

SMS_COLUMNS = ("_id", "thread_id", "address", "person", "date", "date_sent", "read", "type", "subject", "body")
CALL_COLUMNS = ("_id", "number", "date", "duration", "type", "name", "numbertype")


@dataclass
class ProviderError(Exception):
    message: str
    raw: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def parse_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        body = _ROW_PREFIX.sub("", line)
        row: dict[str, str] = {}
        for part in _ROW_SPLIT.split(body):
            key, sep, value = part.partition("=")
            if not sep:
                continue
            value = value.strip()
            row[key.strip()] = "" if value == "NULL" else value
        if row:
            rows.append(row)
    return rows


def check_provider_output(res_output: str) -> None:
    """Raise :class:`ProviderError` when the shell was refused."""
    low = res_output.lower()
    if "security exception" in low or "permission denial" in low:
        raise ProviderError(
            "The phone refused to share this over USB (permission denied for the shell user). "
            "Install the companion app on the phone and export from there instead.",
            res_output,
        )
    if "unable to resolve" in low or "does not exist" in low:
        raise ProviderError("This phone has no provider for that data.", res_output)


def rows_to_vcard(rows: Iterable[dict[str, str]]) -> str:
    """Minimal vCard 3.0 from raw contact rows.

    Used only when the per-contact vCard read is unavailable; it carries names
    and numbers, which is the part people actually need.
    """
    out: list[str] = []
    for row in rows:
        name = row.get("display_name") or row.get("display_name_alt") or ""
        number = row.get("number") or row.get("data1") or ""
        if not name and not number:
            continue
        out.append("BEGIN:VCARD")
        out.append("VERSION:3.0")
        out.append(f"FN:{escape_vcard(name)}")
        out.append(f"N:{escape_vcard(name)};;;;")
        if number:
            out.append(f"TEL;TYPE=CELL:{escape_vcard(number)}")
        out.append("END:VCARD")
    return "\r\n".join(out) + ("\r\n" if out else "")


def escape_vcard(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def normalise_sms(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for r in rows:
        out.append(
            {
                "address": r.get("address", ""),
                "date": _int(r.get("date")),
                "date_sent": _int(r.get("date_sent")),
                "type": _int(r.get("type")),  # 1 inbox, 2 sent
                "read": _int(r.get("read")),
                "subject": r.get("subject", ""),
                "body": r.get("body", ""),
                "thread_id": _int(r.get("thread_id")),
            }
        )
    out.sort(key=lambda m: m.get("date") or 0)
    return out


def normalise_calls(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for r in rows:
        out.append(
            {
                "number": r.get("number", ""),
                "name": r.get("name", ""),
                "date": _int(r.get("date")),
                "duration": _int(r.get("duration")),
                "type": _int(r.get("type")),  # 1 in, 2 out, 3 missed
            }
        )
    out.sort(key=lambda c: c.get("date") or 0)
    return out


def _int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
