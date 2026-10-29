"""Finding a published contact address for a losing bidder.

Scout's own discovery guesses ``.com`` / ``.io`` / ``.co`` domains. South
African SMMEs are overwhelmingly ``.co.za``, and a wrong guess here means
emailing a stranger's company, so this module works the other way round: it
SEARCHES for the company, then only accepts a site after confirming it is the
same business, and only takes an address that is printed on that site.

Rules (the same ones as the rest of PulseBids):

  * never guess an address (no ``info@`` + domain construction);
  * an address is accepted only if it appears on a page of the confirmed site
    and sits on that site's own domain;
  * directories, social networks and tender-aggregator sites are never the
    company's site, whatever they say about it;
  * ``High`` confidence needs a ``.za`` domain, the full distinctive company
    name in the page title, and an on-domain address. Anything weaker is
    ``Medium``, and anything unconfirmed produces no row at all.

Search and fetch are injected, so the logic is tested without a network. The
real implementations (Serper and httpx) are at the bottom.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from harvey.pulsebids.leads import Lead, company_key, is_spent

# Words that appear in thousands of company names and identify nothing.
GENERIC_WORDS = {
    "trading", "services", "service", "holdings", "holding", "construction", "management",
    "group", "projects", "project", "solutions", "enterprises", "enterprise", "general",
    "suppliers", "supplies", "supply", "distributors", "distribution", "contractors",
    "contracting", "consulting", "consultants", "investments", "investment", "africa",
    "south", "african", "sa", "and", "the", "of", "pty", "ltd", "cc", "co", "company",
    "business", "industries", "industrial", "logistics", "technologies", "technology",
    "engineering", "security", "cleaning", "civil", "building", "maintenance", "import",
    "export", "international", "national", "professional", "systems", "agency", "agencies",
}

# Never the company's own site.
BLOCKED_HOSTS = (
    "facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "tiktok.com", "pinterest.com", "wikipedia.org", "yellosa.co.za", "yellowpages.co.za",
    "bizcommunity.com", "brabys.com", "snupit.co.za", "cylex-sa.co.za", "hotfrog.co.za",
    "findthecompany.co.za", "companieslist.co.za", "opencorporates.com", "dnb.com",
    "zoominfo.com", "bizzabo.com", "etenders.gov.za", "treasury.gov.za", "gov.za",
    "tendersa.co.za", "tenderbulletins.co.za", "sa-tenders.co.za", "tendersontime.com",
    "bidnet.com", "gumtree.co.za", "olx.co.za", "jobs.co.za", "pnet.co.za", "indeed.com",
    "glassdoor.com", "google.com", "bing.com", "duckduckgo.com", "cipc.co.za", "bizportal.gov.za",
    "easyfind.co.za", "ananzi.co.za", "mapquest.com", "foursquare.com", "tripadvisor.com",
    "municipalities.co.za", "legalfundi.com", "government.co.za", "laws.africa", "sadcgov.com",
    "saflii.org", "polity.org.za", "gazettes.africa", "africanbusinesspages.com",
    "procompare.co.za", "selltostate.com", "yibaverified.co.za", "fastquotes.co.za",
    "medpages.info", "ilawyer.co.za", "defenceweb.co.za", "metropolis.org", "iziko.org.za",
    "nationalgovernment.co.za", "tenderalerts.co.za", "dsclaw.co.za", "pmg.org.za",
    "golegal.co.za", "engineeringnews.co.za", "southafricantenders.com", "tendersinfo.com", "globaltenders.com", "dnbhoovers.com",
)

# Addresses that are never a person or inbox to write to.
_BAD_LOCAL = re.compile(
    r"^(?:no-?reply|donotreply|do-not-reply|mailer-daemon|postmaster|abuse|privacy|webmaster|"
    r"hostmaster|root|admin@wordpress|example|test|sentry|wixpress)", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_ASSET_TAIL = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
# Inbox names ranked by how likely a tender-related enquiry is read there.
_PREFERRED_LOCALS = ("info", "admin", "office", "enquiries", "enquiry", "sales", "contact",
                     "hello", "tenders", "tender", "bids", "procurement", "accounts")
_SA_MARKERS = ("south africa", "+27", "(pty)", "pty ltd", "pty) ltd", "johannesburg", "pretoria",
               "cape town", "durban", "gauteng", "limpopo", "kwazulu", "mpumalanga",
               "polokwane", "bloemfontein", "port elizabeth", "gqeberha", "east london",
               "nelspruit", "mbombela", "rustenburg", "kimberley", "centurion", "sandton",
               "b-bbee", "bbbee", "cidb", "csd ")
_CONTACT_PATHS = ("/contact", "/contact-us", "/contactus", "/about", "/about-us")

PIPELINE_FIELDS = ["Company", "Email", "Phone", "Website", "Contact name", "Contact title",
                   "Contact confidence", "Status", "Date contacted", "What they do", "Source"]


@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""


@dataclass
class Found:
    company: str
    website: str = ""
    email: str = ""
    confidence: str = ""        # "High" | "Medium" | "" (no contact)
    source: str = ""            # the page the address was printed on
    note: str = ""              # why there is no row, when there is none


Search = Callable[[str], list[Hit]]
Fetch = Callable[[str], "str | None"]


def distinctive_tokens(company: str) -> list[str]:
    """The words of a company name that actually identify it."""
    words = [w for w in company_key(company).split() if len(w) > 1]
    return [w for w in words if w not in GENERIC_WORDS]


def host_of(url: str) -> str:
    host = (urlparse(url if "//" in url else f"//{url}").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_blocked(url: str) -> bool:
    host = host_of(url)
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


def registrable(host: str) -> str:
    """example.co.za / example.org.za -> example.co.za; sub.example.com -> example.com."""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-1] == "za" and parts[-2] in {"co", "org", "net", "web", "ac", "gov"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _name_in(text: str, tokens: list[str]) -> bool:
    flat = re.sub(r"[^a-z0-9]+", " ", text.lower())
    padded = f" {flat} "
    squashed = flat.replace(" ", "")
    return all((f" {t} " in padded) or (t in squashed) for t in tokens)


def _site_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def extract_emails(text: str, site_domain: str) -> list[str]:
    """Addresses printed in ``text`` that sit on ``site_domain``, best first."""
    found: dict[str, None] = {}
    for raw in _EMAIL.findall(text or ""):
        addr = raw.strip(".,;:)(<>").lower()
        if addr.endswith(_ASSET_TAIL) or _BAD_LOCAL.match(addr):
            continue
        local, _, domain = addr.partition("@")
        if registrable(domain) != site_domain:
            continue
        found[addr] = None

    def rank(a: str) -> tuple[int, str]:
        local = a.split("@")[0]
        return (_PREFERRED_LOCALS.index(local) if local in _PREFERRED_LOCALS
                else len(_PREFERRED_LOCALS), a)

    return sorted(found, key=rank)


def name_in_domain(company: str, website: str) -> bool:
    """True when one of the company's distinctive words is in the site's domain.

    Directories and lookalike sites (a procurement directory, a "verified
    suppliers" list) print addresses that belong to THEM, not to the company
    they list, and their domain never carries the company's name. Initials
    domains (dlm.co.za for Dupleix Liquid Meters) fail this test too and go to
    the review file rather than the importable one: a missed contact costs
    nothing, an email to the wrong company does.
    """
    tokens = distinctive_tokens(company)
    label = re.sub(r"[^a-z0-9]", "", registrable(host_of(website)).split(".")[0])
    return bool(label) and any(t in label for t in tokens)


def find_contact(lead: Lead, search: Search, fetch: Fetch, max_searches: int = 2) -> Found:
    """Search for the company, confirm the site is theirs, read an address off it."""
    tokens = distinctive_tokens(lead.company)
    if not tokens:
        return Found(lead.company, note="name is too generic to confirm a site")
    queries = [f'"{lead.company}" South Africa',
               f'{" ".join(tokens)} {lead.tender.buyer if lead.tender else "tender"} contact']
    seen: set[str] = set()
    last_note = "no search result looked like the company's own site"
    for query in queries[:max_searches]:
        for hit in search(query):
            if is_blocked(hit.url):
                continue
            root = _site_root(hit.url)
            if root in seen:
                continue
            seen.add(root)
            result = _try_site(lead, tokens, hit, root, fetch)
            if result.email:
                return result
            last_note = result.note or last_note
    return Found(lead.company, note=last_note)


def _try_site(lead: Lead, tokens: list[str], hit: Hit, root: str, fetch: Fetch) -> Found:
    domain = registrable(host_of(root))
    home = fetch(root)
    if not home:
        return Found(lead.company, note=f"{domain}: could not be read")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", home, re.IGNORECASE | re.DOTALL)
    page_title = title_match.group(1) if title_match else ""
    visible = re.sub(r"<[^>]+>", " ", home)
    # The identity test: every distinctive word of the name is on the site, and
    # something marks it as South African.
    name_on_site = _name_in(f"{page_title} {hit.title} {visible[:6000]}", tokens)
    south_african = domain.endswith(".za") or any(m in visible.lower() for m in _SA_MARKERS)
    if not (name_on_site and south_african):
        return Found(lead.company, note=f"{domain}: could not confirm it is the same company")

    pages = {root: home}
    emails = extract_emails(home, domain)
    for path in _CONTACT_PATHS:
        if emails:
            break
        page = fetch(root + path)
        if page:
            pages[root + path] = page
            emails = extract_emails(page, domain)
    if not emails:
        return Found(lead.company, website=root, note=f"{domain}: confirmed, but no address printed on it")
    source = next(u for u, t in pages.items() if emails[0] in t.lower())
    title_has_name = _name_in(f"{page_title} {hit.title}", tokens)
    # High needs the company's own distinctive word in the domain itself.
    # A name made of ordinary words ("Accreditation and Training Services")
    # can match some other firm's page, so it stays Medium for a human to check.
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
    name_in_domain = any(t in label for t in tokens)
    confidence = "High" if (domain.endswith(".za") and title_has_name and name_in_domain) else "Medium"
    return Found(lead.company, website=root, email=emails[0], confidence=confidence, source=source)


# ── Running over a lead list ────────────────────────────────────────────────

@dataclass
class EnrichReport:
    searched: int = 0
    found: int = 0
    needs_review: int = 0
    skipped_have_email: int = 0
    skipped_spent: int = 0
    skipped_done: int = 0
    unresolved: dict[str, int] = field(default_factory=dict)


def review_path_for(out_path: Path) -> Path:
    return out_path.with_name(out_path.stem + ".review" + out_path.suffix)


def audit_found(out_path: Path) -> tuple[int, int]:
    """Re-sort an existing results file: rows whose domain carries the company's
    own name stay; the rest move to the review file. Offline, no searches.
    Returns (kept, moved)."""
    with open(out_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    keep = [r for r in rows if name_in_domain(r["Company"], r.get("Website", ""))]
    move = [r for r in rows if r not in keep]
    review = review_path_for(out_path)
    already = {r["Company"] for r in _read_rows(review)}
    with open(review, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PIPELINE_FIELDS)
        if f.tell() == 0:
            w.writeheader()
        w.writerows([{k: r.get(k, "") for k in PIPELINE_FIELDS} for r in move
                     if r["Company"] not in already])
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PIPELINE_FIELDS)
        w.writeheader()
        w.writerows([{k: r.get(k, "") for k in PIPELINE_FIELDS} for r in keep])
    return len(keep), len(move)


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_done(path: Path) -> set[str]:
    """Companies a previous run already looked at (so a re-run resumes)."""
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {company_key(r.get("Company", "")) for r in csv.DictReader(f)}


def enrich_leads(leads: list[Lead], spent_emails: set[str], out_path: Path,
                 search: Search, fetch: Fetch, limit: int = 50) -> EnrichReport:
    """Write one pipeline-format row per company where a contact was confirmed.

    Rows are appended as they are found, so an interrupted run loses nothing.
    Companies searched without a result are recorded in ``<out>.misses`` so a
    re-run does not repeat the same searches.
    """
    report = EnrichReport()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    misses_path = out_path.with_suffix(out_path.suffix + ".misses")
    done = read_done(out_path) | read_done(review_path_for(out_path)) | _read_misses(misses_path)
    new_file = not out_path.exists()
    review_path = review_path_for(out_path)
    with open(out_path, "a", newline="", encoding="utf-8") as f, \
            open(review_path, "a", newline="", encoding="utf-8") as rf:
        writer = csv.DictWriter(f, fieldnames=PIPELINE_FIELDS)
        review_writer = csv.DictWriter(rf, fieldnames=PIPELINE_FIELDS)
        if new_file:
            writer.writeheader()
        if rf.tell() == 0:
            review_writer.writeheader()
        for lead in leads:
            if report.searched >= limit:
                break
            if lead.email:
                report.skipped_have_email += 1
                continue
            if is_spent(lead, spent_emails):
                report.skipped_spent += 1
                continue
            if lead.key in done:
                report.skipped_done += 1
                continue
            report.searched += 1
            result = find_contact(lead, search, fetch)
            if result.email:
                row = {"Company": lead.company, "Email": result.email, "Website": result.website,
                       "Contact confidence": result.confidence, "Status": "Researched",
                       "Source": result.source}
                if name_in_domain(lead.company, result.website):
                    report.found += 1
                    writer.writerow(row)
                    f.flush()
                else:
                    report.needs_review += 1
                    review_writer.writerow(row)
                    rf.flush()
            else:
                report.unresolved[result.note] = report.unresolved.get(result.note, 0) + 1
                _append_miss(misses_path, lead.key)
    return report


def _read_misses(path: Path) -> set[str]:
    return set(path.read_text(encoding="utf-8").split("\n")) - {""} if path.exists() else set()


def _append_miss(path: Path, key: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(key + "\n")


# ── Real search and fetch ───────────────────────────────────────────────────

def serper_search(api_key: str, timeout: float = 15.0) -> Search:
    """Google results through Serper.dev, South Africa only. The only search
    backend used here: scraping a search page returns rate-limit pages that
    look like results."""
    import httpx

    def _search(query: str) -> list[Hit]:
        try:
            r = httpx.post("https://google.serper.dev/search", timeout=timeout,
                           headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                           json={"q": query, "gl": "za", "num": 10})
            r.raise_for_status()
        except httpx.HTTPError:
            return []
        return [Hit(title=o.get("title", ""), url=o.get("link", ""), snippet=o.get("snippet", ""))
                for o in r.json().get("organic", []) if o.get("link")]

    return _search


def http_fetch(timeout: float = 12.0) -> Fetch:
    import httpx

    def _fetch(url: str) -> str | None:
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; PulseBidsResearch)"})
        except httpx.HTTPError:
            return None
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or "html" not in ctype:
            return None
        return r.text[:400_000]

    return _fetch
