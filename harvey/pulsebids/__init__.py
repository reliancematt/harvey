"""PulseBids outreach: losing-bidder leads, one email each, rendered in code.

Harvey's own flow finds businesses, researches the people there and has a
model write a three-email sequence for a whole batch. PulseBids works the
other way round. The leads already exist (companies that bid on a government
tender and did not win it), each email opens on the one tender the reader can
verify, and POPIA s69(2) allows a single approach. So this package:

  * reads the lead export and the researched contacts (``leads``),
  * renders the one email from facts, with no model call (``copy``),
  * records everything as Harvey companies, prospects and observations and
    stages the email into the Outbox for review (``importer``).

Everything Harvey already does after that (approval, pacing, daily cap,
warmup, the pre-send gate, reply and bounce handling) is unchanged.
"""

PULSEBIDS_PERSON_SOURCE = "pulsebids:person"
PULSEBIDS_INBOX_SOURCE = "pulsebids:inbox"
PULSEBIDS_CAMPAIGN_NAME = "PulseBids losing bidders"


def is_pulsebids_source(source: str | None) -> bool:
    """True for a prospect that came from the PulseBids lead list."""
    return (source or "").startswith("pulsebids:")
