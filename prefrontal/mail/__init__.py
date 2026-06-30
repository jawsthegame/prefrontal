"""Mail ingestion — aggregate, triage, and surface email as open loops.

Prefrontal's "mail monitoring" capability. The pipeline is deliberately split so
each piece is testable in isolation and the network/model parts stay optional:

- :mod:`prefrontal.mail.models` — a normalized :class:`~prefrontal.mail.models.MailItem`
  and ``normalize_message``, which applies a per-account retention **policy**
  (``full`` keeps the body; ``signals`` keeps only subject + sender).
- :mod:`prefrontal.mail.triage` — classify a message (urgency, needs-action,
  category, who's waiting) with a local Ollama model, falling back to a keyword
  heuristic when the model is unavailable (mirrors the profile summarizer).
- :mod:`prefrontal.mail.ingest` — orchestration: dedup, triage, then persist a
  ``mail`` episode plus a todo for anything that needs action, so mail folds
  into the same memory/briefing loop as everything else.
- :mod:`prefrontal.mail.imap` — an optional, dependency-free stdlib IMAP fetcher
  for a no-n8n path (Gmail works with an app password).

Ingestion is normally driven by ``POST /webhooks/mail/sync`` (n8n's Gmail node
fetches and posts, so OAuth lives in n8n, not here) or by ``prefrontal mail``.
Either way, triage runs on the local model — message content never leaves the
host.
"""

from prefrontal.mail.ingest import IngestSummary, ingest_messages
from prefrontal.mail.models import MailItem, normalize_message
from prefrontal.mail.triage import MailTriage, priority_for_urgency, triage_message

__all__ = [
    "MailItem",
    "normalize_message",
    "MailTriage",
    "triage_message",
    "priority_for_urgency",
    "IngestSummary",
    "ingest_messages",
]
