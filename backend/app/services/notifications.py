"""Notify reviewers when a run parks at the approval gate or escalates (#60).

The human-in-the-loop gate is a *stop*: the graph will sit there indefinitely
until a named reviewer clears it. Without an outbound signal the only way to
learn a run is waiting is to keep reloading the runs index, which is exactly the
polling this module removes.

Two channels, both opt-in via config and both off by default (`NOTIFY_ENABLED`):

- **webhook** — one JSON POST. The payload carries a Slack-shaped `text` *and* a
  structured `event` object, so the same URL works for a Slack/Teams incoming
  webhook (which renders `text` and ignores the rest) and for a generic consumer
  that wants fields rather than a sentence.
- **email** — one SMTP message via stdlib `smtplib`, run in a thread (it is
  blocking, and the caller is on the event loop). No new dependency, and no
  hosted-mail-API key to manage for what is one message per parked run.

**PHI hygiene is the load-bearing property here.** A notification leaves the
trust boundary — into Slack, into a mail relay, into whatever inbox — so it is
built from an explicit allowlist of fields (`_payload`) rather than from the
graph state, and *never* from `matched_patients`, `parsed_criteria`, or the event
log. What ships is operational metadata: which run, what phase, the protocol
filename, how many criteria were extracted, and a link. A reader learns that a
run needs attention and has to open the app — authenticated — to see anything
about a patient. `notify_gate`'s docstring restates this for anyone adding a
field later.

Delivery is best-effort and **never raises**: this is called from inside the SSE
generator that drives a screening, where an exception would abort the run's
stream and mark a perfectly good screening failed. Every failure is logged and
counted (`notifications_total`) instead.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from app.logging_config import get_logger
from app.services.metrics import notifications_total

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.config import Settings

    # One channel's delivery function, as `_dispatch` drives them.
    Sender = Callable[[Settings, dict[str, Any]], Awaitable[None]]

log = get_logger("notifications")

# The phases where a run stops and waits for a person, and how to describe each.
# Both are quiet — no error, no failure — which is precisely why they need
# pushing: a failed run at least shows up as a failure, while these two just sit
# there looking fine.
#
# `escalated` is the stored status of the graph's `human_escalation` node (see
# services.screening._status_from_snapshot, which denormalizes `current_step`).
#
# The headline and the action live beside the status rather than in per-channel
# templates, so a status can't exist without a description and the webhook and the
# email can't drift into describing the same event differently.
_STOPS = {
    "awaiting_approval": (
        "awaiting approval",
        "A reviewer must approve the extracted criteria before any patient data is matched.",
    ),
    "escalated": (
        "escalated for human review",
        "The Critic could not converge on a compliant extraction — the criteria need "
        "correcting by hand.",
    ),
}

# Derived, so adding a stop status is one edit and never leaves it undescribed.
NOTIFY_STATUSES = frozenset(_STOPS)


def _run_url(settings: Settings, thread_id: str) -> str | None:
    """Deep link to the run's detail page, or None when no base URL is configured.

    Must mirror the frontend's `runHref` (frontend/src/lib/runs.ts) exactly: the
    app is a static export, so the id is a *query parameter* on the one exported
    `/runs/view/` page — the `/runs/<thread_id>` path segment this reads like is a
    hard 404 on a static host. The trailing slash before `?` is load-bearing for
    the same reason it is there (`trailingSlash: true` exports
    `runs/view/index.html`).

    Omitted rather than guessed when `NOTIFY_BASE_URL` is unset: a link to the
    wrong host is worse than no link.
    """
    if not settings.notify_base_url:
        return None
    base = settings.notify_base_url.rstrip("/")
    return f"{base}/runs/view/?id={quote(thread_id, safe='')}"


def _payload(
    settings: Settings,
    *,
    thread_id: str,
    status: str,
    source_filename: str | None,
    criteria_count: int | None,
) -> dict[str, Any]:
    """The PHI-free notification body — an explicit allowlist, not a state dump.

    Every field here is operational: an identifier, a phase, the uploaded
    protocol's filename (a document name, never a patient's), and a count. Adding
    anything derived from `matched_patients` or the criteria themselves would push
    protocol/patient detail into Slack and mail relays, which is the one thing
    this feature must not do.
    """
    return {
        "thread_id": thread_id,
        "status": status,
        "source_filename": source_filename,
        "criteria_count": criteria_count,
        "url": _run_url(settings, thread_id),
    }


def _summary(event: dict[str, Any]) -> str:
    """One line naming the run and its phase — the webhook's `text` and the email
    subject both derive from this, so the two channels never disagree.

    Straight quotes and no other punctuation dressing: this string becomes an
    email `Subject` header, and keeping it ASCII keeps it out of RFC 2047
    encoded-word form (unreadable in raw logs and older clients).
    """
    headline, _action = _STOPS[event["status"]]
    name = event["source_filename"] or event["thread_id"]
    return f'TrialGate: "{name}" is {headline}'


def _webhook_text(event: dict[str, Any]) -> str:
    """The rendered message for a chat webhook: what happened, what to do, where.

    The link belongs in `text` rather than only in `event` — a Slack message a
    reviewer can't click through from just sends them to hunt for the run.
    """
    _headline, action = _STOPS[event["status"]]
    parts = [_summary(event), action]
    if event["url"]:
        parts.append(event["url"])
    return "\n".join(parts)


def _email_body(event: dict[str, Any]) -> str:
    _headline, action = _STOPS[event["status"]]
    lines = [
        _summary(event),
        "",
        action,
        "",
        f"Run:      {event['thread_id']}",
        f"Protocol: {event['source_filename'] or 'unknown'}",
    ]
    if event["criteria_count"] is not None:
        lines.append(f"Criteria: {event['criteria_count']} extracted")
    if event["url"]:
        lines += ["", f"Open the run: {event['url']}"]
    lines += [
        "",
        "This notification carries no patient data — sign in to TrialGate to review the run.",
    ]
    return "\n".join(lines)


async def _send_webhook(settings: Settings, event: dict[str, Any]) -> None:
    """POST the event to the configured webhook.

    `text` is what Slack (and Teams, and Discord's Slack-compatible endpoint)
    renders; `event` is the same information as fields, for a consumer that wants
    to route on status rather than parse a sentence. Unknown keys are ignored by
    Slack, so one payload serves both.
    """
    assert settings.notify_webhook_url is not None  # guarded by notify_webhook_configured
    body = {"text": _webhook_text(event), "event": event}
    async with httpx.AsyncClient(timeout=settings.notify_timeout_seconds) as client:
        response = await client.post(settings.notify_webhook_url, json=body)
        response.raise_for_status()


def _build_email(settings: Settings, event: dict[str, Any]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = _summary(event)
    assert settings.notify_email_from is not None  # guarded by notify_email_configured
    message["From"] = settings.notify_email_from
    message["To"] = ", ".join(settings.notify_email_recipient_list)
    message.set_content(_email_body(event))
    return message


def _send_email_blocking(settings: Settings, message: EmailMessage) -> None:
    """The blocking SMTP conversation, isolated so the async side can thread it.

    STARTTLS is applied before authenticating so credentials never cross the wire
    in the clear; `NOTIFY_SMTP_STARTTLS=false` exists only for a plaintext relay on
    a trusted network, and skips auth's TLS requirement with it.
    """
    assert settings.notify_smtp_host is not None  # guarded by notify_email_configured
    with smtplib.SMTP(
        settings.notify_smtp_host,
        settings.notify_smtp_port,
        timeout=settings.notify_timeout_seconds,
    ) as smtp:
        if settings.notify_smtp_starttls:
            smtp.starttls()
        if settings.notify_smtp_username and settings.notify_smtp_password:
            smtp.login(settings.notify_smtp_username, settings.notify_smtp_password)
        smtp.send_message(message)


async def _send_email(settings: Settings, event: dict[str, Any]) -> None:
    message = _build_email(settings, event)
    # smtplib is synchronous: off the event loop, or a slow relay stalls every
    # other in-flight request on this worker, not just this run.
    await asyncio.to_thread(_send_email_blocking, settings, message)


async def _dispatch(settings: Settings, channel: str, send: Sender, event: dict[str, Any]) -> None:
    """Run one channel's send under a timeout, converting every outcome to a log
    line and a metric.

    The blind except is the point: a notification failure — a 500 from Slack, a
    refused SMTP connection, a DNS miss — must not surface to the caller, which is
    mid-stream on a screening that itself succeeded. The detail goes to the server
    log; the reviewer's run is unaffected.
    """
    try:
        await asyncio.wait_for(send(settings, event), settings.notify_timeout_seconds)
    except TimeoutError:
        notifications_total.labels(channel=channel, outcome="failed").inc()
        log.warning("notify.timeout", channel=channel, status=event["status"])
    except Exception as exc:  # noqa: BLE001 — best-effort side channel, never fatal
        notifications_total.labels(channel=channel, outcome="failed").inc()
        log.warning(
            "notify.failed",
            channel=channel,
            status=event["status"],
            error=type(exc).__name__,
            detail=str(exc),
        )
    else:
        notifications_total.labels(channel=channel, outcome="sent").inc()
        log.info("notify.sent", channel=channel, status=event["status"])


async def _notify(
    settings: Settings,
    *,
    thread_id: str,
    status: str,
    source_filename: str | None,
    criteria_count: int | None,
) -> None:
    """Build the event once and hand it to every configured channel.

    Channels are dispatched concurrently, so two of them cost one timeout rather
    than two — and a hung webhook can't eat the email's budget.
    """
    event = _payload(
        settings,
        thread_id=thread_id,
        status=status,
        source_filename=source_filename,
        criteria_count=criteria_count,
    )
    sends = []
    if settings.notify_webhook_configured:
        sends.append(_dispatch(settings, "webhook", _send_webhook, event))
    if settings.notify_email_configured:
        sends.append(_dispatch(settings, "email", _send_email, event))
    if sends:
        await asyncio.gather(*sends)


async def notify_gate(
    settings: Settings,
    *,
    thread_id: str,
    status: str,
    source_filename: str | None = None,
    criteria_count: int | None = None,
) -> None:
    """Notify every configured channel that a run stopped and needs a person.

    A no-op unless notifications are enabled *and* `status` is one a human has to
    act on (`NOTIFY_STATUSES`) — a run that finished or failed on its own is not
    waiting for anybody, and paging on those would train reviewers to ignore this.

    Callers pass metadata only, and this must stay true: the payload is assembled
    by `_payload` from the arguments here, so a future caller cannot widen what
    leaves the process by handing over more state. Nothing patient-identifying,
    and nothing from the criteria themselves, belongs in this signature.

    **Never raises.** It is called from inside a screening's SSE generator (see
    services.screening._notify_if_parked), where an exception would abort the
    stream of a run that actually succeeded and mark it failed. `_dispatch` already
    absorbs each channel's own failures — the backstop here is for a bug anywhere
    else on this path (payload assembly, a template, the dispatch plumbing), so the
    guarantee holds structurally rather than by the internals staying careful.
    """
    if not settings.notify_enabled or status not in NOTIFY_STATUSES:
        return
    try:
        await _notify(
            settings,
            thread_id=thread_id,
            status=status,
            source_filename=source_filename,
            criteria_count=criteria_count,
        )
    except Exception:  # noqa: BLE001 — the never-raises backstop described above
        log.error("notify.crashed", status=status, exc_info=True)
