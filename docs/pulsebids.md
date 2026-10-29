# PulseBids outreach mode

Harvey, adapted for one job: email companies that bid on a South African
government tender and did not win it, once each, opening on the one tender
they can verify. Sending, approval, pacing, the pre-send gate and reply
handling are Harvey's own. The lead list, the email and the rules are
PulseBids'.

## What is different from stock Harvey

| Stock Harvey | PulseBids mode (`pulsebids.enabled: true`) |
|---|---|
| Finds businesses by ICP and city | You import the lead list (`harvey pulsebids import`) |
| A model writes a 3-email sequence per batch | One email per lead, rendered in code from stored facts. No model call |
| Follow-ups on day 3 and 7 | None exist. The campaign has a single step (POPIA s69(2): one approach) |
| Reads every UNSEEN inbox message and marks it read | Reads the shared mailbox read-only, newest first, and fetches in full only mail from people Harvey wrote to |
| HTML is rejected | Still plain text only (the gate is unchanged) |
| Copy rules live in prompts | The gate rejects rand amounts, tender counts, "lost", and any "awarded to" other than "another bidder" |

## Setup

1. `python3 -m venv .venv && source .venv/bin/activate && pip install -e . openpyxl`
2. `cp harvey.pulsebids.example.yaml harvey.local.yaml`
3. `.env` (Zoho, for your sending mailbox). Check the hosts against your
   own Zoho account's data centre (.com, .eu, .in, .com.au):
   ```
   SMTP_HOST=smtp.zoho.com   SMTP_PORT=587
   SMTP_USERNAME=support@yourdomain.co.za   SMTP_PASSWORD=<app-specific password>
   IMAP_HOST=imap.zoho.com   IMAP_PORT=993
   IMAP_SENT_FOLDER=Sent        # optional: files a copy of each email in Zoho's Sent
   REOON_API_KEY=...            # or ZEROBOUNCE_API_KEY; Harvey only sends to verified addresses
   ```
   In Zoho: enable IMAP access, and if the account has two-factor
   authentication create an app-specific password (Accounts > Security).
4. `harvey` needs the Claude CLI logged in for reply classification only
   (`claude login`).

## Workflow

```bash
harvey pulsebids import --leads leads.csv \
    --contacts outreach_workbook.xlsx --verify
harvey pulsebids stage --limit 10 --preview     # renders into the Outbox; sends nothing
harvey outbox                                   # read them, then approve
harvey run                                      # sends within the cap and window, reads replies
```

Keep lead data, `.env` and `data/` out of git. `.env` and `data/` are
already ignored; do not add the CSV or workbook to the repo.

## Safety properties (each has a test in tests/test_pulsebids.py)

- A lead whose workbook status is anything but an "open" one, whose date
  contacted is set, or whose address is in the Contact Log is imported as
  `contacted` and can never be staged.
- Staging skips any address that already has a pending, approved or sent
  email, in any campaign.
- Only `verified` addresses are staged (`--allow-risky` adds catch-alls).
- The email is built from the tender reference, title and buyer only. The
  winner, the award value and the loss count are discarded at import and
  never stored.
- A first name is used only when the contact research is `High`; otherwise
  "Good day,".
- Staging never approves. Autopilot should stay off.

## Known limits

- Tender close month is not in the lead export, so "closed <month>" is
  omitted unless the CSV has a `tender_closed` column.
- Feed titles are cut at 80 characters; the renderer drops the last word and
  adds an ellipsis. RAF titles are replaced by "RAF tender <reference>".
  A title that itself contains a rand amount is held by the gate for editing.
- Finding contacts for leads with no email (179 of 188 at the time of
  writing) is not built yet. Harvey's Scout can do website and team-page
  research, but its domain guessing (.com, .io, .co) must be replaced with
  .co.za and identity confirmation before it is pointed at these leads.
- Opt-out keywords are English only.

## Zoho mailbox settings (support@ mailbox)

If your Zoho Mail address starts `mail.zoho.com`, the account is on the `.com`
data centre. Other regions use `.eu`, `.in`, `.com.au` or `.jp` hosts instead.

```
SMTP_HOST=smtp.zoho.com
SMTP_PORT=587
IMAP_HOST=imap.zoho.com
IMAP_PORT=993
SMTP_USERNAME=support@pulsebids.co.za
IMAP_USERNAME=support@pulsebids.co.za
SMTP_PASSWORD=<app password>
IMAP_PASSWORD=<app password>
```

Set IMAP_HOST explicitly: it defaults to the SMTP host, which is wrong for Zoho. Enable IMAP in
Zoho Mail (Settings, Mail Accounts, IMAP Access) and create an app password at
accounts.zoho.com (Security, App Passwords). Keep it in `.env` only.

## Finding contacts

```
harvey pulsebids find-contacts --leads data/leads.csv --contacts Outreach.xlsx --limit 50
```

Needs `SERPER_API_KEY`. For each lead with no email it searches for the
company, accepts a site only when every distinctive word of the name is on it
and it is South African, and takes only an address printed on that site's own
domain. Directories, social networks and tender aggregators are never accepted.
`High` means `.za` domain, name in the page title and an on-domain address;
anything weaker is `Medium`. Nothing is guessed. Results go to
`data/pulsebids_found_contacts.csv` (gitignored); review them, then pass that
file to `import --contacts` after the workbook. A re-run skips companies
already searched.

## Sending without an email verifier

`harvey pulsebids import ... --unchecked` stores addresses as `risky` (not
confirmed deliverable). Two opt-ins are then needed before anything can leave:
`harvey pulsebids stage --allow-risky`, and `send_to_risky: true` under
`channels.email` in `harvey.yaml` (the pre-send gate enforces it). Send a handful first
and watch the support@ inbox for bounces before staging more.
