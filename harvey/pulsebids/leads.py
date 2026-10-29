"""Reading the PulseBids lead export and the researched contacts.

The lead CSV is ``build_lead_list`` output (one row per losing bidder). It
carries facts that must never reach a prospect: the winner's name, award
values and the number of recorded losses. This module parses the one tender
the email may mention (reference, title, buyer) and DISCARDS the rest, so the
renderer cannot leak what it never receives.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

# The feed cuts titles at 80 characters, often mid-word; 78+ is treated as cut.
TITLE_TRUNCATION_AT = 78
_SEP = " · "          # " · "


@dataclass
class Tender:
    """The one tender an email may mention. No winner, no value."""

    reference: str
    title: str
    buyer: str
    url: str = ""
    closed: str = ""             # "June 2026"; blank when the export has none
    title_truncated: bool = False
    review_reasons: list[str] = field(default_factory=list)


@dataclass
class Lead:
    company: str
    key: str                      # normalised name; the identity across re-runs
    tender: Tender | None
    recorded_losses: int          # a FLOOR; stored as an observation, never quoted
    recorded_wins: int
    last_bid: str
    evidence_url: str
    # Researched contact (from the workbook), when there is one.
    email: str = ""
    phone: str = ""
    website: str = ""
    contact_name: str = ""
    contact_title: str = ""
    contact_confidence: str = ""
    what_they_do: str = ""
    status: str = ""              # workbook pipeline status
    date_contacted: str = ""


def company_key(name: str) -> str:
    """Normalised company name: case, punctuation and legal suffixes removed."""
    text = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    words = [w for w in text.split() if w not in {"pty", "ltd", "limited", "cc", "proprietary"}]
    return " ".join(words)


def parse_notable_loss(text: str, url: str = "") -> Tender | None:
    """``REF · TITLE · BUYER · lost to WINNER · R1,234`` -> Tender.

    Returns None when the line doesn't have that shape: a tender we cannot
    read with confidence is a tender we do not mention. The winner and the
    value are dropped here and nowhere else are they parsed.
    """
    parts = [p.strip() for p in (text or "").split(_SEP)]
    if len(parts) < 4:
        return None
    lost_at = next((i for i, p in enumerate(parts) if p.lower().startswith("lost to")), -1)
    if lost_at < 3:
        return None
    reference = parts[0]
    buyer = parts[lost_at - 1]
    title = _SEP.join(parts[1:lost_at - 1]).strip()
    if not (reference and buyer and title):
        return None
    tender = Tender(reference=reference, title=title, buyer=buyer, url=url)
    _clean_title(tender)
    return tender


def _clean_title(tender: Tender) -> None:
    """Feed titles are truncated mid-word and RAF titles are boilerplate."""
    title = re.sub(r"\s+", " ", tender.title).strip()
    if "road accident fund" in tender.buyer.lower() or tender.buyer.strip().upper() == "RAF":
        tender.title = f"RAF tender {tender.reference}"
        tender.review_reasons.append("RAF title is boilerplate; led with the reference")
        return
    if len(title) >= TITLE_TRUNCATION_AT:
        tender.title_truncated = True
        # Dropping the last word can never leave a mid-word fragment behind.
        title = title.rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
        tender.review_reasons.append("feed title was truncated; last word dropped")
    tender.title = title


def read_leads(csv_path: str | Path) -> list[Lead]:
    leads: list[Lead] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("company_name") or "").strip()
            if not name:
                continue
            url = (row.get("evidence_url") or "").strip()
            tender = parse_notable_loss(row.get("notable_loss_1", ""), url)
            if tender is not None:
                tender.closed = (row.get("tender_closed") or "").strip()
            leads.append(Lead(
                company=name,
                key=company_key(name),
                tender=tender,
                recorded_losses=_int(row.get("recorded_losses")),
                recorded_wins=_int(row.get("recorded_wins")),
                last_bid=(row.get("last_bid") or "").strip(),
                evidence_url=url,
            ))
    return leads


# Column names in the workbook's "Outreach Pipeline" sheet.
_PIPELINE_COLUMNS = {
    "company": "Company", "email": "Email", "phone": "Phone", "website": "Website",
    "name": "Contact name", "title": "Contact title", "confidence": "Contact confidence",
    "status": "Status", "contacted": "Date contacted", "what": "What they do",
}


def read_contacts(path: str | Path) -> tuple[dict[str, dict], set[str]]:
    """Researched contacts keyed by company key, plus every address already
    approached (the Contact Log sheet, when the file is the workbook).

    Accepts the workbook (.xlsx) or a CSV with the same column names.
    """
    path = Path(path)
    contacts: dict[str, dict] = {}
    spent_emails: set[str] = set()
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as e:     # pragma: no cover - message only
            raise SystemExit("Reading the workbook needs openpyxl: pip install openpyxl "
                             "(or export the Outreach Pipeline sheet to CSV).") from e
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet = wb["Outreach Pipeline"]
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
        header, body = rows[0], rows[1:]
        if "Contact Log" in wb.sheetnames:
            for r in wb["Contact Log"].iter_rows(values_only=True):
                for cell in r:
                    spent_emails.update(re.findall(
                        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", str(cell or "")))
        records = [dict(zip(header, r)) for r in body]
    else:
        with open(path, newline="", encoding="utf-8-sig") as f:
            records = list(csv.DictReader(f))
    for rec in records:
        company = str(rec.get(_PIPELINE_COLUMNS["company"]) or "").strip()
        if not company:
            continue
        contacts[company_key(company)] = {
            k: str(rec.get(col) or "").strip() for k, col in _PIPELINE_COLUMNS.items()
        }
    # support@ is the sender, never a recipient.
    spent_emails = {e.lower() for e in spent_emails if not e.lower().startswith("support@pulsebids")}
    return contacts, spent_emails


def attach_contacts(leads: list[Lead], contacts: dict[str, dict]) -> int:
    """Merge researched contact details into the leads; returns matches."""
    matched = 0
    for lead in leads:
        c = contacts.get(lead.key)
        if not c:
            continue
        matched += 1
        lead.email = c.get("email", "").lower()
        lead.phone = c.get("phone", "")
        lead.website = c.get("website", "")
        lead.contact_name = c.get("name", "")
        lead.contact_title = c.get("title", "")
        lead.contact_confidence = c.get("confidence", "")
        lead.what_they_do = c.get("what", "")
        lead.status = c.get("status", "")
        lead.date_contacted = c.get("contacted", "")
    return matched


# Pipeline statuses that mean "nobody has approached this company yet". Any
# other value (Contacted, Replied, Declined, ...) is treated as spent: a
# second approach is the one thing POPIA s69(2) does not allow.
OPEN_STATUSES = {"", "not started", "ready", "ready to contact", "ready to call",
                 "researched", "to contact"}


def is_spent(lead: Lead, spent_emails: set[str]) -> str:
    """Why this lead must not be approached, or "" when it may be."""
    if lead.status.lower() in OPEN_STATUSES and not lead.date_contacted:
        if lead.email and lead.email in spent_emails:
            return "address is in the Contact Log"
        return ""
    if lead.date_contacted:
        return f"already approached ({lead.date_contacted})"
    return f"pipeline status '{lead.status}'"


def _int(value) -> int:
    try:
        return int(float(str(value).strip() or 0))
    except ValueError:
        return 0
