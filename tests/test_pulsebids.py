"""PulseBids outreach: parsing, copy rules, import, staging, gate, mailbox.

All data here is invented (example.com style), per CONTRIBUTING.md.
"""

import imaplib
import os
import tempfile

import pytest
import pytest_asyncio

from harvey.config import HarveyConfig
from harvey.gate import pre_send_check
from harvey.integrations.smtp_mail import SmtpImapProvider
from harvey.models.prospect import Prospect
from harvey.pulsebids import is_pulsebids_source
from harvey.pulsebids.copy import copy_violations, render, subject_for
from harvey.pulsebids.importer import import_leads, stage_outbox
from harvey.pulsebids.leads import (
    Lead, attach_contacts, company_key, is_spent, parse_notable_loss, read_leads,
)
from harvey.state import StateManager

LOSS = ("REF/01/2026 · Supply and delivery of office furniture · Example Municipality "
        "· lost to ACME SUPPLIERS · R12,322,940")
URL = "https://www.etenders.gov.za/Home/opportunities?id=1"
CSV_HEADER = ("rank,score,company_name,sector,province,recorded_losses,recorded_wins,"
              "measured_win_rate,first_bid,last_bid,days_since_last_bid,median_bid_value_zar,"
              "largest_loss_zar,size_proxy_band,enterprise_type,departments_bid_to,top_department,"
              "notable_loss_1,notable_loss_2,notable_loss_3,evidence_url,pitch_angle")


def csv_row(name, loss=LOSS, losses=2, wins=0):
    return (f'1,70,{name},Goods,Gauteng,{losses},{wins},0,2025-01-01,2026-06-02,70,1,1,mid,,,,'
            f'"{loss}",,,{URL},angle')


def write_csv(tmp_path, *rows):
    path = tmp_path / "leads.csv"
    path.write_text(CSV_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def lead(**kw):
    base = dict(company="Example Trading", key="example trading",
                tender=parse_notable_loss(LOSS, URL), recorded_losses=2, recorded_wins=0,
                last_bid="2026-06-02", evidence_url=URL, email="owner@example.co.za")
    base.update(kw)
    return Lead(**base)


# ── parsing ───────────────────────────────────────────────────────────────

def test_parse_drops_winner_and_value():
    t = parse_notable_loss(LOSS, URL)
    assert (t.reference, t.buyer) == ("REF/01/2026", "Example Municipality")
    blob = repr(t)
    assert "ACME" not in blob and "12,322" not in blob


def test_parse_rejects_unreadable_line():
    assert parse_notable_loss("just some text", URL) is None
    assert parse_notable_loss("REF · title · buyer", URL) is None


def test_truncated_title_loses_its_last_word_and_is_flagged():
    long_title = "Request for Proposal for the Appointment of Travel Management Company to provid"
    t = parse_notable_loss(f"R/1 · {long_title} · Buyer · lost to X · R1", URL)
    assert t.title_truncated and t.title.endswith("…") and "provid" not in t.title
    assert t.review_reasons


def test_raf_title_leads_with_reference():
    t = parse_notable_loss("RAF/9 · BOILERPLATE TEXT · Road Accident Fund · lost to X · R1", URL)
    assert t.title == "RAF tender RAF/9"


def test_company_key_ignores_suffix_and_case():
    assert company_key("EXAMPLE TRADING (PTY) LTD") == company_key("Example Trading")


# ── spent leads ───────────────────────────────────────────────────────────

def test_contacted_status_is_spent():
    assert is_spent(lead(status="Contacted"), set())
    assert is_spent(lead(status="Not started", date_contacted="2026-08-13"), set())
    assert is_spent(lead(status="Not started"), {"owner@example.co.za"})
    assert is_spent(lead(status="Not started"), set()) == ""
    assert is_spent(lead(status="Something new"), set())     # unknown = spent


# ── copy ──────────────────────────────────────────────────────────────────

def test_rendered_email_passes_the_gate():
    l = lead(contact_name="Jane Doe", contact_title="Owner", contact_confidence="High")
    subject, body = render(l)
    assert body.startswith("Hi Jane,")
    assert len(subject) <= 45 and "?" not in subject
    p = Prospect(email="owner@example.co.za", email_status="verified",
                 source="pulsebids:person", status="queued")
    result = pre_send_check("owner@example.co.za", subject, body, prospect=p)
    assert result.ok, result.reasons


def test_unnamed_or_unsure_contact_gets_good_day():
    assert render(lead(contact_name="Jane Doe", contact_confidence="Medium"))[1].startswith("Good day,")
    assert render(lead())[1].startswith("Good day,")


def test_email_never_contains_winner_value_or_count():
    subject, body = render(lead(recorded_losses=7))
    assert "ACME" not in body and "12,322" not in body and "7" not in body.replace("2026", "")
    assert copy_violations(subject, body) == []
    assert "Awarded to another bidder." in body


def test_no_tender_needs_what_they_do():
    assert render(lead(tender=None)) is None
    subject, body = render(lead(tender=None, what_they_do="office cleaning"))
    assert "office cleaning" in body and "Government tenders for office cleaning" == subject


@pytest.mark.parametrize("text", [
    "You lost to another firm", "Awarded to ACME Suppliers", "It was worth R12,322,940",
    "You have bid on 3 tenders", "We counted two bids", "a recorded loss",
])
def test_copy_rules_catch_leaks(text):
    assert copy_violations("Your bid on tender X/1", f"Good day,\n\n{text}\n")


def test_tender_reference_is_not_money():
    assert copy_violations("Your bid on tender R123/2026", "Rand Water issued tender R123/2026.") == []


def test_gate_blocks_a_leak_only_for_pulsebids_prospects():
    p = Prospect(email="a@example.co.za", email_status="verified", source="pulsebids:inbox")
    other = Prospect(email="a@example.co.za", email_status="verified", source="web")
    body = "Good day,\n\nYou lost to ACME Suppliers on this one."
    assert not pre_send_check("a@example.co.za", "Your bid on tender X", body, prospect=p).ok
    assert pre_send_check("a@example.co.za", "Your bid on tender X", body, prospect=other).ok


def test_source_helper():
    assert is_pulsebids_source("pulsebids:person") and not is_pulsebids_source("web")


# ── import and staging ────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


def make_config():
    return HarveyConfig(
        persona={"name": "Test", "company": "Test Co", "role": "x", "email": "s@example.com", "linkedin": "", "tone": "x"},
        product={"name": "P", "description": "d", "pricing": "p", "key_benefits": [], "objection_responses": {}},
        icp={"industries": [], "company_size": "", "titles": [], "geography": []},
        pulsebids={"enabled": True},
    )


@pytest.mark.asyncio
async def test_import_is_idempotent_and_stores_no_winner(state, tmp_path):
    path = write_csv(tmp_path, csv_row("Example Trading"), csv_row("Other Co"))
    leads = read_leads(path)
    attach_contacts(leads, {"example trading": {"email": "a@example.co.za", "name": "Jane Doe",
                                                "title": "Owner", "confidence": "High"}})
    first = await import_leads(state, leads)
    second = await import_leads(state, leads)
    assert (first.companies_new, second.companies_new, second.companies_existing) == (2, 0, 2)
    company = await state.get_company_by_external_id("pulsebids:example trading")
    assert company.location == ""
    obs = await state.get_observations(company_id=company.id)
    codes = [o["signal_code"] for o in obs]
    assert codes.count("TENDER_LOSS") == 1
    assert "ACME" not in repr(obs) and "12,322" not in repr(obs)


@pytest.mark.asyncio
async def test_spent_lead_is_never_staged(state, tmp_path):
    path = write_csv(tmp_path, csv_row("Example Trading"))
    leads = read_leads(path)
    attach_contacts(leads, {"example trading": {"email": "a@example.co.za", "status": "Contacted"}})
    report = await import_leads(state, leads)
    assert report.spent == 1
    prospect = await state.get_prospect_by_email("a@example.co.za")
    assert prospect.status == "contacted"
    staged = await stage_outbox(state, make_config())
    assert staged.staged == 0


@pytest.mark.asyncio
async def test_stage_needs_verified_email_and_stages_once(state, tmp_path):
    path = write_csv(tmp_path, csv_row("Example Trading"))
    leads = read_leads(path)
    attach_contacts(leads, {"example trading": {"email": "a@example.co.za", "status": "Not started"}})
    await import_leads(state, leads)
    unverified = await stage_outbox(state, make_config())
    assert unverified.staged == 0 and "email not verified" in unverified.skipped

    prospect = await state.get_prospect_by_email("a@example.co.za")
    async with state._connect() as db:
        await db.execute("UPDATE prospects SET email_status = 'verified' WHERE id = ?", (prospect.id,))
        await db.commit()
    first = await stage_outbox(state, make_config())
    second = await stage_outbox(state, make_config())
    assert first.staged == 1 and second.staged == 0
    items = await state.get_outbox(status="pending_review")
    assert len(items) == 1 and items[0]["step"] == 1


@pytest.mark.asyncio
async def test_writer_ignores_pulsebids_prospects(state, tmp_path):
    from harvey.agents.writer import Writer
    path = write_csv(tmp_path, csv_row("Example Trading"))
    leads = read_leads(path)
    attach_contacts(leads, {"example trading": {"email": "a@example.co.za", "status": "Not started"}})
    await import_leads(state, leads, verify=False)
    prospect = await state.get_prospect_by_email("a@example.co.za")
    async with state._connect() as db:
        await db.execute("UPDATE prospects SET email_status='verified', status='new' WHERE id=?", (prospect.id,))
        await db.commit()

    class NoBrain:
        def load_skills_for_agent(self, *_): return ""
        def __getattr__(self, name): raise AssertionError("the model must not be used")

    await Writer(NoBrain(), state, make_config()).run()
    assert await state.get_campaigns_by_status("draft") == []


@pytest.mark.asyncio
async def test_decision_in_pulsebids_mode_never_writes_or_prospects(state):
    from harvey.main import decide_next_action
    summary = {"new_prospects": 5, "writable_prospects": 5, "draft_campaigns": 0, "open_conversations": 0}
    assert await decide_next_action(None, state, make_config(), summary=summary) == "idle"


# ── shared mailbox: read-only, known senders only ─────────────────────────

class FakeImap:
    """Records every command so the test can assert nothing was modified."""

    log: list = []

    def __init__(self, *a, **k):
        self.messages = {
            b"1": (b"From: Customer <customer@example.net>\r\nSubject: Invoice\r\n\r\n", b"customer body"),
            b"2": (b"From: Jane <a@example.co.za>\r\nSubject: Re: Your bid\r\n\r\n", b"yes please"),
            b"3": (b"From: Other <z@example.org>\r\nSubject: Re: x\r\nIn-Reply-To: <sent-1@pulsebids>\r\n\r\n", b"hi"),
        }

    def login(self, *a): FakeImap.log.append("login")
    def select(self, box, readonly=False): FakeImap.log.append(("select", box, readonly)); return "OK", [b"3"]
    def search(self, *a): FakeImap.log.append(("search",) + a); return "OK", [b"1 2 3"]
    def store(self, *a): FakeImap.log.append(("STORE",) + a); return "OK", []
    def logout(self): pass

    def fetch(self, msg_id, spec):
        FakeImap.log.append(("fetch", spec))
        head, body = self.messages[msg_id]
        raw = head + (body if "HEADER" not in spec else b"")
        return "OK", [(b"x", raw), b")"]


def provider(monkeypatch):
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    FakeImap.log = []
    env = type("E", (), {"smtp_host": "h", "smtp_username": "u@example.com", "smtp_password": "p"})()
    prov = SmtpImapProvider(make_config(), env)
    prov.set_known_correspondents({"a@example.co.za"}, {"<sent-1@pulsebids>"})
    return prov


@pytest.mark.asyncio
async def test_shared_mailbox_is_read_only_and_filtered(monkeypatch):
    prov = provider(monkeypatch)
    got = await prov.get_replies()
    assert sorted(m.from_email for m in got) == ["a@example.co.za", "z@example.org"]
    assert ("select", "INBOX", True) in FakeImap.log           # opened read-only
    assert not any(c[0] == "STORE" for c in FakeImap.log if isinstance(c, tuple))
    assert all("PEEK" in c[1] for c in FakeImap.log if isinstance(c, tuple) and c[0] == "fetch")


@pytest.mark.asyncio
async def test_unknown_senders_are_never_fetched_in_full(monkeypatch):
    prov = provider(monkeypatch)
    await prov.get_replies()
    full = [c for c in FakeImap.log if isinstance(c, tuple) and c[0] == "fetch" and "HEADER" not in c[1]]
    assert len(full) == 2        # customer@example.net's mail was only header-peeked


# ── end to end through Harvey's real sender ───────────────────────────────

@pytest.mark.asyncio
async def test_staged_email_sends_once_with_footer_and_never_follows_up(state, tmp_path):
    from harvey.config import ComplianceConfig
    from tests.test_outbox_native import Cfg, Env, FakeProvider
    from harvey.agents.sender import Sender

    path = write_csv(tmp_path, csv_row("Example Trading"))
    leads = read_leads(path)
    attach_contacts(leads, {"example trading": {
        "email": "a@example.co.za", "name": "Jane Doe", "title": "Owner",
        "confidence": "High", "status": "Not started"}})
    await import_leads(state, leads)
    prospect = await state.get_prospect_by_email("a@example.co.za")
    async with state._connect() as db:
        await db.execute("UPDATE prospects SET email_status = 'verified' WHERE id = ?", (prospect.id,))
        await db.commit()
    assert (await stage_outbox(state, make_config())).staged == 1
    assert await state.approve_outbox() == 1

    cfg = Cfg()
    cfg.compliance = ComplianceConfig(
        postal_address="Example Product is operated by Example Co (Pty) Ltd, South Africa.",
        opt_out_line='One-time approach, I won\'t follow up unless you reply. Reply "no thanks" and I will remove you.')
    provider = FakeProvider()
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sender.provider, sender.send_pacing = provider, False

    await sender._run_native()
    await sender._run_native()
    assert len(provider.sent) == 1
    mail = provider.sent[0]
    assert mail["to"] == "a@example.co.za" and mail["body"].startswith("Hi Jane,")
    assert mail["body"].rstrip().endswith('Reply "no thanks" and I will remove you.')
    assert "ACME" not in mail["body"] and "12,322" not in mail["body"]
    assert (await state.get_prospect(prospect.id)).status == "contacted"
    all_rows = await state.get_outbox()
    assert len(all_rows) == 1, "a second email exists for a one-approach lead"
    # And it can never be staged again.
    assert (await stage_outbox(state, make_config())).staged == 0


def test_longest_realistic_email_with_footer_fits_the_gate():
    from harvey.config import compliance_footer_lines
    from harvey.config import ComplianceConfig
    t = parse_notable_loss(
        "RFQ/2026/LONG-REFERENCE-001 \u00b7 " + "Supply installation commissioning and maintenance of equipment " * 2
        + " \u00b7 The Very Long Named Provincial Department of Public Works Roads and Transport"
        " \u00b7 lost to X \u00b7 R1", URL)
    t.closed = "September 2026"
    l = lead(tender=t, contact_name="Jane Doe", contact_confidence="High", company="A Rather Long Company Name Trading and Projects")
    subject, body = render(l)
    cc = ComplianceConfig(postal_address="Example Product is operated by Example Co (Pty) Ltd, South Africa.",
                          opt_out_line='One-time approach, I won\'t follow up unless you reply. Reply "no thanks" and I will remove you.')
    full = body + "\n\n" + "\n".join(compliance_footer_lines(type("C", (), {"compliance": cc})()))
    p = Prospect(email="owner@example.co.za", email_status="verified", source="pulsebids:person", status="queued")
    result = pre_send_check("owner@example.co.za", subject, full, prospect=p,
                            postal_address=cc.postal_address)
    assert result.ok, result.reasons


# ── contact finder (fake search / fetch, invented companies) ────────────────

from harvey.pulsebids import enrich  # noqa: E402
from harvey.pulsebids.leads import read_contacts, read_leads  # noqa: E402


def _lead(name, **kw):
    return Lead(company=name, key=company_key(name), tender=None, recorded_losses=1,
                recorded_wins=0, last_bid="", evidence_url="", **kw)


def _site(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def test_distinctive_tokens_drop_generic_words():
    assert enrich.distinctive_tokens("Zimele Trading and Projects (Pty) Ltd") == ["zimele"]
    assert enrich.distinctive_tokens("General Construction Services CC") == []


def test_registrable_handles_za_suffixes():
    assert enrich.registrable("mail.zimele.co.za") == "zimele.co.za"
    assert enrich.registrable("www.zimele.com") == "zimele.com"


def test_confirmed_site_gives_high_confidence_on_domain_email():
    pages = {"https://zimele.co.za": _site("Zimele Civils | Home", "Polokwane. info@zimele.co.za")}
    found = enrich.find_contact(
        _lead("Zimele Civils (Pty) Ltd"),
        lambda q: [enrich.Hit("Zimele Civils", "https://zimele.co.za/")],
        pages.get)
    assert (found.email, found.confidence) == ("info@zimele.co.za", "High")


def test_directory_and_wrong_company_rejected_never_guessed():
    pages = {"https://other.co.za": _site("Other Works", "South Africa. info@other.co.za")}
    found = enrich.find_contact(
        _lead("Zimele Civils (Pty) Ltd"),
        lambda q: [enrich.Hit("Zimele Civils", "https://www.facebook.com/zimele"),
                   enrich.Hit("Zimele", "https://other.co.za")],
        pages.get)
    assert found.email == "" and "same company" in found.note


def test_off_domain_and_noreply_emails_ignored():
    text = "write to zimele@gmail.com or noreply@zimele.co.za or sales@zimele.co.za"
    assert enrich.extract_emails(text, "zimele.co.za") == ["sales@zimele.co.za"]


def test_contact_page_is_read_when_home_has_no_address():
    pages = {"https://zimele.co.za": _site("Zimele Civils", "Polokwane"),
             "https://zimele.co.za/contact": _site("Contact", "admin@zimele.co.za")}
    found = enrich.find_contact(
        _lead("Zimele Civils"), lambda q: [enrich.Hit("Zimele Civils", "https://zimele.co.za")],
        pages.get)
    assert found.email == "admin@zimele.co.za" and found.source.endswith("/contact")


def test_enrich_skips_known_resumes_and_writes_pipeline_csv(tmp_path):
    pages = {"https://zimele.co.za": _site("Zimele Civils", "info@zimele.co.za")}
    leads = [_lead("Zimele Civils"), _lead("Has Email Co", email="a@b.co.za"),
             _lead("Nobody Known")]
    searches = []

    def search(q):
        searches.append(q)
        return [enrich.Hit("Zimele Civils", "https://zimele.co.za")] if "Zimele" in q else []

    out = tmp_path / "found.csv"
    r1 = enrich.enrich_leads(leads, set(), out, search, pages.get)
    assert (r1.found, r1.skipped_have_email) == (1, 1)
    first_searches = len(searches)
    r2 = enrich.enrich_leads(leads, set(), out, search, pages.get)
    assert r2.searched == 0 and r2.skipped_done == 2 and len(searches) == first_searches
    contacts, _ = read_contacts(out)
    assert contacts[company_key("Zimele Civils")]["email"] == "info@zimele.co.za"


def test_multi_loss_lead_holds_link_unless_explicit(tmp_path):
    header = "company_name,notable_loss_1,notable_loss_2,evidence_url,notable_loss_1_url\n"
    row = ('Acme,"T1/2026 · Roads · City of X · lost to Y",other,https://e/other,{}\n')
    p = tmp_path / "l.csv"
    p.write_text(header + row.format(""))
    assert read_leads(p)[0].tender.link_uncertain
    p.write_text(header + row.format("https://e/t1"))
    lead = read_leads(p)[0]
    assert not lead.tender.link_uncertain and lead.tender.url == "https://e/t1"


def test_generic_word_name_is_only_medium_unless_in_domain():
    pages = {"https://assessortrainer.co.za": _site("Accreditation Training Services", "info@assessortrainer.co.za")}
    found = enrich.find_contact(
        _lead("Accreditation and Training Services"),
        lambda q: [enrich.Hit("Accreditation Training Services", "https://assessortrainer.co.za")],
        pages.get)
    assert found.email and found.confidence == "Medium"


def test_directory_domain_goes_to_review_not_import(tmp_path):
    pages = {"https://supplierlist.co.za": _site("Blvck Elephant listing", "South Africa info@supplierlist.co.za")}
    out = tmp_path / "f.csv"
    r = enrich.enrich_leads(
        [_lead("Blvck Elephant")], set(), out,
        lambda q: [enrich.Hit("Blvck Elephant", "https://supplierlist.co.za")], pages.get)
    assert (r.found, r.needs_review) == (0, 1)
    assert "supplierlist" not in out.read_text()
    assert "supplierlist" in enrich.review_path_for(out).read_text()


def test_audit_resorts_existing_file(tmp_path):
    out = tmp_path / "f.csv"
    out.write_text("Company,Email,Website\nZimele Civils,info@zimele.co.za,https://zimele.co.za\n"
                   "Dante Languages,info@x.co.za,https://umhlangatourism.co.za\n")
    assert enrich.audit_found(out) == (1, 1)
    assert "Dante" not in out.read_text() and "Dante" in enrich.review_path_for(out).read_text()
