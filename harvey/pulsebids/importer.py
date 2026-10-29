"""Import the PulseBids lead list into Harvey, and stage the one email each.

``import_leads`` records every lead as a Harvey company with observations
(the facts), and a prospect when a contact address was researched. It never
sends and never stages anything. ``stage_outbox`` renders the email for each
eligible prospect and puts it in the Outbox for approval; from there Harvey's
normal sender takes over (approval, pacing, daily cap, warmup, the pre-send
gate, the footer).

One approach per company, ever (POPIA s69(2)):
  * a lead whose pipeline status or contact log says it was approached is
    imported as 'contacted' and never staged;
  * staging skips any address that already has an outbox row in a live or
    sent state, in any campaign;
  * the campaign has exactly one step, so no follow-up exists to send.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from harvey.collectors.discover import normalize_domain
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.pulsebids import (
    PULSEBIDS_CAMPAIGN_NAME,
    PULSEBIDS_INBOX_SOURCE,
    PULSEBIDS_PERSON_SOURCE,
    is_pulsebids_source,
)
from harvey.pulsebids.copy import Sender, copy_violations, render
from harvey.pulsebids.leads import Lead, Tender, is_spent

logger = logging.getLogger("harvey.pulsebids")

COLLECTOR = "pulsebids"
LIVE_OUTBOX_STATUSES = ("pending_review", "approved", "sent")


@dataclass
class ImportReport:
    leads: int = 0
    companies_new: int = 0
    companies_existing: int = 0
    with_tender: int = 0
    prospects_new: int = 0
    spent: int = 0
    no_contact: int = 0
    verified: int = 0
    unverified: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class StageReport:
    staged: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    needs_review: list[str] = field(default_factory=list)
    previews: list[tuple[str, str, str]] = field(default_factory=list)

    def skip(self, reason: str):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _tender_json(t: Tender) -> str:
    return json.dumps({
        "reference": t.reference, "title": t.title, "buyer": t.buyer, "url": t.url,
        "closed": t.closed, "title_truncated": t.title_truncated,
        "review_reasons": t.review_reasons,
    })


def _tender_from_json(text: str, url: str) -> Tender | None:
    try:
        d = json.loads(text)
        return Tender(
            reference=d["reference"], title=d["title"], buyer=d["buyer"],
            url=d.get("url") or url, closed=d.get("closed", ""),
            title_truncated=bool(d.get("title_truncated")),
            review_reasons=list(d.get("review_reasons") or []),
        )
    except (ValueError, KeyError, TypeError):
        return None


async def import_leads(state, leads: list[Lead], spent_emails: set[str] | None = None,
                       env=None, verify: bool = False) -> ImportReport:
    """Record leads, facts and contacts. Idempotent: a re-run adds nothing
    for a company it already knows."""
    from harvey.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    spent_emails = spent_emails or set()
    report = ImportReport(leads=len(leads))
    run_id = f"pulsebids-{_utcnow_iso()}"

    for lead in leads:
        external_id = f"pulsebids:{lead.key}"
        known = await state.get_company_by_external_id(external_id)
        domain = normalize_domain(lead.website) if lead.website else ""
        company_id = known.id if known else await state.add_company(Company(
            name=lead.company, domain=domain, website=lead.website, phone=lead.phone,
            # Not the tender's province: that is where the BUYER is.
            location="", source="pulsebids:losing_bidder", source_url=lead.evidence_url,
            external_id=external_id,
        ))
        if known:
            report.companies_existing += 1
        else:
            report.companies_new += 1
            await state.add_observations(_observations(lead, company_id), run_id=run_id)
        if lead.tender is not None:
            report.with_tender += 1

        if not lead.email:
            report.no_contact += 1
            continue
        reason = is_spent(lead, spent_emails)
        if reason:
            report.spent += 1
        status = "contacted" if reason else "queued"
        email_status = ""
        if verify and not reason:
            from harvey.integrations.email_finder import verify_published_address
            email_status = await verify_published_address(lead.email, env)
        if email_status == "verified":
            report.verified += 1
        elif not reason:
            report.unverified += 1

        parts = lead.contact_name.split()
        named = len(parts) >= 2 and bool(lead.contact_title)
        prospect = Prospect(
            company_id=company_id,
            first_name=parts[0] if named else "",
            last_name=" ".join(parts[1:]) if named else "",
            email=lead.email, email_status=email_status,
            email_verified=email_status == "verified",
            phone=lead.phone,
            title=lead.contact_title if named else (lead.contact_title or "Company inbox"),
            source=PULSEBIDS_PERSON_SOURCE if named else PULSEBIDS_INBOX_SOURCE,
            source_url=lead.evidence_url, status=status, company=lead.company,
        )
        before = await state.get_prospect_by_email(lead.email)
        await state.add_prospect(prospect)
        if before is None:
            report.prospects_new += 1
    return report


def _observations(lead: Lead, company_id: str) -> list[dict]:
    def row(code, *, num=None, text="", url=""):
        return {"company_id": company_id, "signal_code": code, "collector": COLLECTOR,
                "value_num": num, "value_text": text, "confidence": 1.0, "evidence_url": url}

    rows = [row("LOSS_RECORDED_FLOOR", num=float(lead.recorded_losses)),
            row("NEVER_WON_ON_RECORD", num=1.0 if lead.recorded_wins == 0 else 0.0)]
    if lead.last_bid:
        rows.append(row("LAST_BID_DATE", text=lead.last_bid))
    if lead.tender is not None:
        rows.append(row("TENDER_LOSS", text=_tender_json(lead.tender), url=lead.tender.url or lead.evidence_url))
    if lead.contact_confidence:
        rows.append(row("CONTACT_CONFIDENCE", text=lead.contact_confidence))
    if lead.what_they_do:
        rows.append(row("WHAT_THEY_DO", text=lead.what_they_do))
    return rows


async def _lead_from_db(state, prospect: Prospect) -> Lead | None:
    """Rebuild what the renderer needs from the stored facts."""
    obs = await state.get_observations(company_id=prospect.company_id, latest_only=True)
    by_code = {o["signal_code"]: o for o in obs}
    tender = None
    if "TENDER_LOSS" in by_code:
        tender = _tender_from_json(by_code["TENDER_LOSS"]["value_text"],
                                   by_code["TENDER_LOSS"]["evidence_url"])
    first = prospect.first_name
    return Lead(
        company=prospect.company, key="", tender=tender, recorded_losses=0, recorded_wins=0,
        last_bid="", evidence_url=prospect.source_url, email=prospect.email,
        contact_name=f"{first} {prospect.last_name}".strip(),
        contact_title=prospect.title,
        contact_confidence=(by_code.get("CONTACT_CONFIDENCE") or {}).get("value_text", ""),
        what_they_do=(by_code.get("WHAT_THEY_DO") or {}).get("value_text", ""),
    )


async def _campaign_id(state) -> str:
    for campaign in await state.get_campaigns_by_status("active"):
        if campaign.name == PULSEBIDS_CAMPAIGN_NAME:
            return campaign.id
    # One step, no follow-up: the campaign row exists so the sender can look
    # the step up; the email itself is rendered per lead.
    return await state.add_campaign(Campaign(
        id="", name=PULSEBIDS_CAMPAIGN_NAME, status="active",
        sequence=[EmailStep(step=1, subject="(rendered per lead)",
                            body="(rendered per lead)", delay_days=0)],
    ))


async def _already_in_outbox(state, email: str) -> bool:
    async with state._connect() as db:
        marks = ",".join("?" * len(LIVE_OUTBOX_STATUSES))
        async with db.execute(
            f"SELECT 1 FROM outbox WHERE to_email = ? AND status IN ({marks}) LIMIT 1",
            (email.strip().lower(), *LIVE_OUTBOX_STATUSES),
        ) as cursor:
            return await cursor.fetchone() is not None


async def stage_outbox(state, config, limit: int = 10, allow_risky: bool = False,
                       provider_name: str = "smtp") -> StageReport:
    """Render and queue the email for up to ``limit`` eligible prospects.

    Everything lands as 'pending_review': this command never approves.
    """
    report = StageReport()
    sender = Sender.from_config(config)
    campaign_id = None
    wanted = ("verified", "risky") if allow_risky else ("verified",)
    for status in ("queued", "new"):
        for prospect in await state.get_prospects_by_status(status):
            if report.staged >= limit:
                return report
            if not is_pulsebids_source(prospect.source):
                continue
            email = (prospect.email or "").strip().lower()
            if (prospect.email_status or "guess") not in wanted:
                report.skip("email not verified")
                continue
            if await _already_in_outbox(state, email):
                report.skip("already has an email in the outbox or sent")
                continue
            lead = await _lead_from_db(state, prospect)
            rendered = render(lead, sender) if lead else None
            if rendered is None:
                report.skip("no tender and no researched 'what they do'")
                continue
            subject, body = rendered
            problems = copy_violations(subject, body)
            if problems:
                report.skip("breaks a copy rule: " + "; ".join(problems))
                continue
            if lead.tender is not None and lead.tender.review_reasons:
                report.needs_review.append(
                    f"{prospect.company}: " + "; ".join(lead.tender.review_reasons))
            if campaign_id is None:
                campaign_id = await _campaign_id(state)
            item_id = await state.add_outbox_item(
                prospect_id=prospect.id, campaign_id=campaign_id, step=1, kind="sequence",
                to_email=email, subject=subject, body=body, send_at=_utcnow_iso(),
                status="pending_review", provider=provider_name,
            )
            if item_id is None:
                report.skip("slot already used")
                continue
            await state.queue_prospect_if_unsent(prospect.id)
            report.staged += 1
            report.previews.append((prospect.company, subject, body))
    return report
