"""ADR 0012 spike — measure the four behaviours the record says it has not tested.

ADR 0012 chooses `calendar.freebusy` + `calendar.app.created` on the grounds that
both are non-sensitive, so there is no Google verification to survive. It names
two behaviours as untested and load-bearing, and this script measures them
against a real account rather than reasoning about them.

    Q1  Does an event on an app-created secondary calendar send a normal
        attendee invitation? ADR 0004 requires mentees to receive one while
        completing no OAuth flow. If not, the mentee gets nothing.

    Q2  Do that calendar's busy intervals appear in the mentor's free/busy?
        Measured twice, because "the mentor's free/busy" is ambiguous and the
        two readings have different answers:
          Q2a  querying the PRIMARY calendar id  -- what everyone else sees
          Q2b  querying the SECONDARY calendar id -- what we can see, knowing it

    Q3  Can we make the calendar contribute to availability with only
        `calendar.app.created`? Reading or writing its sharing needs
        `calendar.acls`, which we are deliberately not requesting.

Nothing here is committed to the repository and nothing is decided by running
it. It prints what happened; the decision follows.

Run it with the ephemeral dependencies rather than adding any to the project:

    uv run --with google-auth-oauthlib --with google-api-python-client \
        python calendar_spike.py --attendee someone-else@example.com

Cleanup is offered at the end and can be re-run alone with --cleanup-only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

# Exactly what ADR 0012 point 1 names. Nothing else — the whole question is
# whether this pair is sufficient, so a wider grant here would prove nothing.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/calendar.freebusy",
]

CALENDAR_NAME = "EduFurther (ADR 0012 spike)"

#: Beside the script rather than beside the caller. These were CWD-relative, so
#: running from the repo root minted a token the `.gitignore` entry — which names
#: `scripts/` — did not cover.
HERE = Path(__file__).parent
TOKEN_PATH = HERE / "calendar_spike_token.json"
STATE_PATH = HERE / "calendar_spike_state.json"


#: Searched in order. `script-data-dev/` is the better home and is already
#: ignored wholesale, where `scripts/` needs a rule naming this file by pattern.
SECRET_DIRS = (HERE.parent / "script-data-dev", HERE)


def default_client_secrets() -> Path:
    """The download, wherever the console and Windows between them put it.

    Windows appends `.json` to a file whose extension it is already hiding, so
    the console's `client_secret_<id>.json` lands as `...json.json`. Globbing
    beats asking the caller to notice that.
    """
    for directory in SECRET_DIRS:
        found = sorted(directory.glob("client_secret*.json*"))
        if found:
            return found[0]
    return SECRET_DIRS[0] / "client_secret.json"


OK, NO, HUH = "  YES", "   NO", "    ?"


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def service(client_secrets: Path) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def cleanup(api: Any) -> None:
    if not STATE_PATH.exists():
        print("nothing recorded to clean up")
        return
    calendar_id = json.loads(STATE_PATH.read_text(encoding="utf-8"))["calendar_id"]
    try:
        api.calendars().delete(calendarId=calendar_id).execute()
        print(f"deleted {calendar_id}")
    except Exception as exc:
        print(f"could not delete {calendar_id}: {exc}")
    STATE_PATH.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-secrets",
        type=Path,
        default=None,
        help="the OAuth client JSON downloaded from the Cloud console "
        "(default: whatever client_secret*.json sits beside this script)",
    )
    parser.add_argument(
        "--attendee",
        action="append",
        default=[],
        help="an address YOU control, and NOT the one you authenticate as — "
        "Google never emails somebody their own event. Repeat the flag, or pass "
        "a comma-separated list, to invite several: the realistic shape is a "
        "platform account creating the event with mentor and mentee as guests",
    )
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()

    secrets = args.client_secrets or default_client_secrets()
    if not secrets.exists():
        print(f"no client secrets at {secrets} — see the guide, step 3")
        return 1

    api = service(secrets)

    if args.cleanup_only:
        cleanup(api)
        return 0

    attendees = [
        address.strip()
        for value in args.attendee
        for address in value.split(",")
        if address.strip()
    ]
    if not attendees:
        print("--attendee is required (use an address you can read, not the one you sign in as)")
        return 1
    guests = [{"email": address} for address in attendees]

    findings: dict[str, str] = {}
    #: Bound in Q1 and read again in Q4, which names it in the join instructions.
    creator_email = ""

    # ---------------------------------------------------------------- setup --
    rule("Creating the secondary calendar")
    created = api.calendars().insert(body={"summary": CALENDAR_NAME}).execute()
    calendar_id = created["id"]
    STATE_PATH.write_text(json.dumps({"calendar_id": calendar_id}), encoding="utf-8")
    print(f"calendar id: {calendar_id}")
    print("(this alone proves calendar.app.created can create calendars)")

    # **Five minutes out, deliberately.** This was three days out, which made
    # every question answerable except the one that needs a human to walk into
    # the room: whether an invited guest can join with the creator absent. A
    # meeting nobody can attend during the session cannot test that.
    start = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).replace(second=0, microsecond=0)
    end = start + dt.timedelta(minutes=45)

    # ------------------------------------------------------------------- Q1 --
    rule("Q1  Does an attendee on an app-created calendar get an invitation?")
    body = {
        "summary": "EduFurther spike — mentor and mentee",
        "description": "ADR 0012 spike. Safe to delete.",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "attendees": guests,
        # `transparency: opaque` is the default and is what makes an event count
        # as busy at all. Stated rather than assumed, because Q2 depends on it.
        "transparency": "opaque",
        # **Every event carries a room.** `MeetingProvider.GOOGLE_MEET` promises
        # a per-session link, so an event without one is not the production
        # shape — and the invitation the guest receives is the thing that has to
        # contain it.
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    try:
        # `conferenceDataVersion=1` is not optional: without it the API accepts
        # the write and silently drops `conferenceData`, which is
        # indistinguishable from a permissions refusal in the response.
        event = (
            api.events()
            .insert(calendarId=calendar_id, body=body, sendUpdates="all", conferenceDataVersion=1)
            .execute()
        )
        print(f"event created: {event.get('htmlLink')}")
        returned = event.get("attendees")
        if returned:
            print(f"attendees accepted by the API: {json.dumps(returned, indent=2)}")
            findings["Q1 API accepted attendees"] = OK
        else:
            print("!! the API dropped the attendees field — it accepted the write and")
            print("   silently discarded the guest, which is the worst of both answers")
            findings["Q1 API accepted attendees"] = NO
    except Exception as exc:
        print(f"!! events.insert refused: {exc}")
        findings["Q1 API accepted attendees"] = NO
        findings["Q1 invitation delivered"] = NO
        event = None

    if event and findings.get("Q1 API accepted attendees") == OK:
        # **Who Google thinks is sending.** The first runs answered "accepted"
        # and no invitation arrived, and the two ordinary explanations are
        # distinguishable only here: an event whose organizer is the app-created
        # calendar sends as that calendar, and an attendee who *is* the
        # authenticated account is never emailed their own invitation.
        fetched = api.events().get(calendarId=calendar_id, eventId=event["id"]).execute()
        print()
        print(f"  organizer: {json.dumps(fetched.get('organizer'))}")
        print(f"  creator:   {json.dumps(fetched.get('creator'))}")
        print(f"  status:    {fetched.get('status')}")
        creator_email = str((fetched.get("creator") or {}).get("email", ""))
        if creator_email.lower() in {address.lower() for address in attendees}:
            print()
            print("  !! THE ATTENDEE IS THE AUTHENTICATED ACCOUNT.")
            print("     Google never emails somebody their own event. Re-run with")
            print("     --attendee set to an address that is NOT the one you consented as.")
            findings["Q1 invitation delivered"] = NO

        # A second, explicit send. `insert` with `sendUpdates=all` should have
        # mailed already; if only this one lands, the notification is tied to
        # modification rather than creation and the booking flow needs to know.
        print()
        print("  re-sending by patching the event (sendUpdates=all)...")
        api.events().patch(
            calendarId=calendar_id,
            eventId=event["id"],
            body={"description": "ADR 0012 spike. Safe to delete. (resend)"},
            sendUpdates="all",
        ).execute()
        print("  patch accepted")

        print()
        for address in attendees:
            print(f"  >>> CHECK THE INBOX OF {address} — and its Spam folder.")
        print("      An invitation email is the answer to Q1. Nothing else is —")
        print("      the API returning 200 does not mean anything was sent.")
        findings.setdefault("Q1 invitation delivered", HUH)

    # ------------------------------------------------------------------- Q4 --
    rule("Q4  The room, and the question only a human can answer")
    # Read off the event above rather than creating a second one. A separate
    # Meet-only event proved the link could be minted and left the guests holding
    # an invitation to a different room; the production shape is one event that
    # is both the invitation and the room.
    if event:
        link = event.get("hangoutLink")
        conference = event.get("conferenceData", {})
        status = (conference.get("createRequest") or {}).get("status", {})
        print(f"  hangoutLink:    {link}")
        print(f"  conference id:  {conference.get('conferenceId')}")
        print(f"  create status:  {status.get('statusCode')}")
        findings["Q4 Meet link created"] = OK if link else NO
        if not link:
            print("  !! accepted the write and produced no link")
        else:
            local = start.astimezone()
            print()
            print(f"  starts {local:%H:%M} local — five minutes from now.")
            print()
            print("  >>> THE TEST: open that link as an invited guest while the")
            print(f"      creating account ({creator_email or 'the one you signed in as'}) is")
            print("      SIGNED OUT. Joining with no knock is the answer.")
            print()
            print("      Being asked to wait would mean the calendar-invite bypass")
            print("      does not apply on consumer accounts — and a platform-owned")
            print("      event would need a host in every session, which sinks it.")
            print("      Sign in as the invited account: the bypass is keyed to the")
            print("      address on the invitation, not to holding the link.")
        findings["Q4 guests join with no host"] = HUH
    else:
        findings["Q4 Meet link created"] = NO

    # ------------------------------------------------------------------ Q2a --
    rule("Q2a Does it show in the PRIMARY calendar's free/busy? (what others see)")
    window = {
        "timeMin": (start - dt.timedelta(hours=1)).isoformat(),
        "timeMax": (end + dt.timedelta(hours=1)).isoformat(),
    }
    primary = api.freebusy().query(body={**window, "items": [{"id": "primary"}]}).execute()
    primary_busy = primary["calendars"]["primary"]["busy"]
    print(json.dumps(primary, indent=2)[:600])
    findings["Q2a busy on primary"] = OK if primary_busy else NO
    if not primary_busy:
        print("\n  -> The session is INVISIBLE to anyone querying the mentor's main")
        print("     calendar. A colleague inviting them at this time sees them free.")

    # ------------------------------------------------------------------ Q2b --
    rule("Q2b Does it show when the SECONDARY calendar is named explicitly?")
    secondary = api.freebusy().query(body={**window, "items": [{"id": calendar_id}]}).execute()
    entry = secondary["calendars"].get(calendar_id, {})
    print(json.dumps(secondary, indent=2)[:600])
    if entry.get("errors"):
        print("\n  -> freebusy refused this calendar; calendar.freebusy may not reach it")
        findings["Q2b busy on the new calendar"] = NO
    else:
        findings["Q2b busy on the new calendar"] = OK if entry.get("busy") else NO

    # ------------------------------------------------------------------- Q3 --
    rule("Q3  What can we NOT do with only these two scopes?")
    for label, call in (
        ("read the calendar's sharing (acl.list)", lambda: api.acl().list(calendarId=calendar_id)),
        (
            "make it visible in the user's list (calendarList.insert)",
            lambda: api.calendarList().insert(body={"id": calendar_id, "selected": True}),
        ),
        (
            "read the PRIMARY calendar's events",
            lambda: api.events().list(calendarId="primary", maxResults=1),
        ),
    ):
        try:
            call().execute()
            print(f"  permitted : {label}")
            findings[f"Q3 {label}"] = OK
        except Exception as exc:
            code = getattr(getattr(exc, "resp", None), "status", "?")
            print(f"  refused   : {label}  (HTTP {code})")
            findings[f"Q3 {label}"] = NO

    # ---------------------------------------------------------------- report --
    rule("What this run measured")
    for question, verdict in findings.items():
        print(f"{verdict}   {question}")
    print(
        f"\n  ? = only a human can answer. Check {', '.join(attendees)} and say "
        "whether\n      an invitation arrived, and who it appears to be from."
    )
    print(f"\nClean up with:  python {Path(__file__).name} --cleanup-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
