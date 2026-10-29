"""The PulseBids outreach email, rendered from facts, plus the rules it must keep.

Two plain-text versions of the existing HTML templates:

  * tender known: opens on the one tender the reader can verify;
  * no tender: only for a lead with a researched ``what_they_do``. The
    specific tender IS the hook, so without both there is nothing honest to
    say and no email is produced.

The renderer is given a Tender (reference, title, buyer) and nothing else
about the loss: no winner, no value, no count. ``copy_violations`` is the
second line, run by the pre-send gate on everything that leaves.
"""

import re

from dataclasses import dataclass

from harvey.pulsebids.leads import Lead

MAX_SUBJECT = 45


@dataclass
class Sender:
    """Who the email is from. Built from harvey.yaml, so no person or
    company is written into the code."""

    name: str = "Your Name"
    role: str = "Co-founder"
    company: str = "Your Company"
    email: str = "you@example.com"
    website: str = ""

    @property
    def first_name(self) -> str:
        return (self.name.split() or [""])[0]

    @property
    def signature(self) -> str:
        contact = f"{self.email} \u00b7 {self.website}" if self.website else self.email
        return f"{self.name}\n{self.role}, {self.company}\n{contact}"

    @classmethod
    def from_config(cls, config) -> "Sender":
        persona, pb = config.persona, getattr(config, "pulsebids", None)
        return cls(name=persona.name, role=persona.role, company=persona.company,
                   email=persona.email, website=getattr(pb, "website", "") or "")


def greeting(lead: Lead) -> str:
    """First name only where the research is High confidence; otherwise the
    standard South African greeting. A wrong name is worse than no name."""
    first = (lead.contact_name or "").split()[0] if (lead.contact_name or "").strip() else ""
    if first and lead.contact_confidence.lower() == "high":
        return f"Hi {first},"
    return "Good day,"


def subject_for(lead: Lead) -> str:
    """Reference-led, under 45 characters, no question mark, never 'lost'."""
    if lead.tender is not None:
        subject = f"Your bid on tender {lead.tender.reference}"
        if len(subject) <= MAX_SUBJECT:
            return subject
        subject = f"Tender {lead.tender.reference}"
        if len(subject) <= MAX_SUBJECT:
            return subject
        return subject[:MAX_SUBJECT].rstrip(" /-")
    what = (lead.what_they_do or "").strip().rstrip(".")
    subject = f"Government tenders for {what}"
    if len(subject) <= MAX_SUBJECT:
        return subject
    return "Tenders you may not be seeing"


def render(lead: Lead, sender: Sender | None = None) -> tuple[str, str] | None:
    """(subject, body) for one lead, or None when there is nothing honest to say."""
    sender = sender or Sender()
    if lead.tender is not None:
        return subject_for(lead), _tender_body(lead, sender)
    if (lead.what_they_do or "").strip():
        return subject_for(lead), _no_tender_body(lead, sender)
    return None


def _tender_body(lead: Lead, s: Sender) -> str:
    t = lead.tender
    detail = f"{t.buyer} · ref {t.reference}"
    if t.closed:
        detail += f" · closed {t.closed}"
    return (
        f"{greeting(lead)}\n\n"
        f"I'm {s.first_name}, a co-founder of {s.company}. I'll keep this short.\n\n"
        f"{lead.company} appears in the public government award records for this tender:\n\n"
        f"{t.title}\n"
        f"{detail}\n"
        "Awarded to another bidder.\n"
        f"Verify it on eTenders: {t.url}\n\n"
        "You did the full job on that one: CSD registration, tax clearance, the B-BBEE "
        "certificate, the pricing, the courier. It went elsewhere, and that cost lands "
        "the same whether the bid converts or not.\n\n"
        f"{s.company} exists so that effort goes where it can actually convert. We read every "
        "tender published on eTenders and the municipal portals, pull out the closing date, "
        "the requirements and the submission method, and show you the ones that match what "
        f"{lead.company} is already registered and qualified to do, while there is still "
        "time to prepare properly.\n\n"
        "If that is worth ten minutes, I will walk you through it and set your keywords up "
        "with you on the call. Or just reply to this email, it comes straight to me.\n\n"
        f"{s.signature}\n\n"
        f"You're receiving this because {lead.company} appears in South Africa's public "
        "government tender award records."
    )


def _no_tender_body(lead: Lead, s: Sender) -> str:
    what = lead.what_they_do.strip().rstrip(".")
    return (
        f"{greeting(lead)}\n\n"
        f"I'm {s.first_name}, a co-founder of {s.company}. I'll keep this short.\n\n"
        f"I came across {lead.company} in South Africa's public government tender records, "
        f"and from what I can see you do {what}, which is work the state buys constantly "
        "and advertises badly.\n\n"
        f"{s.company} reads every tender on eTenders and the municipal portals each day, pulls "
        "out the closing date, the requirements and how to submit, and shows you only the "
        "ones matching what you are registered to do.\n\n"
        "The hard part was never finding tenders. It is finding them early enough to prepare "
        "properly, and not spending a week on a bid you were never going to qualify for. "
        "Every one of those costs the same whether it converts or not: CSD, tax clearance, "
        "the B-BBEE certificate, the pricing, the courier.\n\n"
        f"Tell me what {lead.company} is registered and qualified to do and I will set your "
        "keywords up myself, then show you what is open right now. Ten minutes and you will "
        "know whether it is useful to you. Or just reply to this email, it comes straight "
        "to me.\n\n"
        f"{s.signature}\n\n"
        f"You're receiving this because {lead.company} appears in South Africa's public "
        "government tender records."
    )


# ── The rules from lead-generation / outreach-execution, as code ──────────

# "R12,322,940", "R 4 706 237", "R2.5m", "ZAR". A bare "R123/2026" is a tender
# reference, not money, and "Rand Water" is a buyer, so neither matches.
_CURRENCY = re.compile(
    r"(?<![A-Za-z0-9])R\s?\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?"
    r"|(?<![A-Za-z0-9])R\s?\d+(?:\.\d+)?\s?(?:m|k|bn|million|billion)\b"
    r"|\bZAR\b", re.IGNORECASE)
_COUNT = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|several|multiple|many)\s+"
    r"(?:recorded\s+)?(?:other\s+)?(?:tenders?|bids?|losses|awards?)\b", re.IGNORECASE)
_AWARDED = re.compile(r"awarded\s+to\s+(?!another\s+bidder\b)", re.IGNORECASE)


def copy_violations(subject: str, body: str) -> list[str]:
    """Why this email breaks a PulseBids copy rule; empty when it keeps them.

    Recorded losses are a floor, award values are unreliable (29 flagged
    rows) and naming the winner is out, so none of them may appear.
    """
    problems: list[str] = []
    text = f"{subject}\n{body}"
    if _CURRENCY.search(text):
        problems.append("quotes a rand amount (award values are unreliable)")
    if _COUNT.search(text):
        problems.append("states a number of tenders or bids (recorded losses are a floor)")
    if re.search(r"\blost\b|\bloss", subject, re.IGNORECASE):
        problems.append("the word lost/loss in the subject")
    if re.search(r"\blost to\b|\byou(?:'ve| have)? lost\b|\brecorded loss|\bnever won\b|\bno win\b",
                 body, re.IGNORECASE):
        problems.append("says they lost, how often, or that they never won")
    if _AWARDED.search(text):
        problems.append("says who the tender was awarded to (only 'another bidder' is allowed)")
    if "?" in subject:
        problems.append("question mark in subject")
    if len(subject) > MAX_SUBJECT:
        problems.append(f"subject over {MAX_SUBJECT} characters")
    if re.match(r"\s*(re|fwd?)\s*:", subject, re.IGNORECASE):
        problems.append("fake Re:/Fwd: subject")
    if re.search(r"\bfollowing up\b|\bjust circling back\b|\bbumping this\b", text, re.IGNORECASE):
        problems.append("follow-up wording on a first and only contact")
    return problems
