"""#384 spike: does a Meet link patched onto an existing invite still let guests in?

#384 creates every session's calendar event with no conference, carrying only
the session page, and patches the Meet in at the last reminder. That is safe
only if what ADR 0012's spike measured for a Meet created *with* the event also
holds for one added *afterwards*:

    Q1  Is the patched conference created, and how long does `pending` last?
    Q2  Does the guest's calendar show the link after a `sendUpdates=none`
        patch, and did any email arrive for it?
    Q3  Does the invited guest join WITHOUT knocking, the creating account
        absent? If they must knock, nobody can admit them and #384 is unsafe.

Q1 is printed. Q2 and Q3 need a human: open the guest's calendar, then join the
link signed in as the guest, with the creating account out of the call.

    uv run --with google-auth-oauthlib --with google-api-python-client \\
        python scripts/meet_patch_spike.py --attendee someone-else@example.com

Same OAuth client and token as `calendar_spike.py`. Re-run with --cleanup-only
to delete the spike calendar.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid

from calendar_spike import HERE, default_client_secrets, service

sys.stdout.reconfigure(encoding="utf-8")

STATE_PATH = HERE / "meet_patch_spike_state.json"


def status_of(event: dict) -> str | None:
    """The conference's creation status: `pending`, `success` or `failure`."""
    return (
        event.get("conferenceData", {}).get("createRequest", {}).get("status", {}).get("statusCode")
    )


def recorded() -> list[str]:
    """Every spike calendar not yet deleted. A list, so a second run before a
    cleanup does not lose the first one's id (the old shape held one)."""
    if not STATE_PATH.exists():
        return []
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return list(state.get("calendar_ids") or [state["calendar_id"]])


def cleanup(api: object) -> None:
    for calendar_id in recorded():
        api.calendars().delete(calendarId=calendar_id).execute()  # type: ignore[attr-defined]
        print(f"deleted {calendar_id}")
    STATE_PATH.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--attendee",
        action="append",
        default=[],
        help="an address you control, not the one you sign in as; repeat to invite several",
    )
    parser.add_argument("--minutes", type=int, default=10, help="how far out the event starts")
    parser.add_argument(
        "--meet-at-creation",
        action="store_true",
        help="the control: make the Meet with the event, as today, and patch nothing",
    )
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()
    api = service(default_client_secrets())
    if args.cleanup_only:
        cleanup(api)
        return 0
    if not args.attendee:
        parser.error("--attendee is required")

    calendar = api.calendars().insert(body={"summary": "EduFurther (#384 spike)"}).execute()
    STATE_PATH.write_text(
        json.dumps({"calendar_ids": [*recorded(), calendar["id"]]}), encoding="utf-8"
    )
    start = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(minutes=args.minutes)
    body = {
        "summary": "EduFurther #384 spike",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + dt.timedelta(minutes=30)).isoformat()},
        "attendees": [{"email": email} for email in args.attendee],
        "guestsCanSeeOtherGuests": False,
        "guestsCanInviteOthers": False,
        "description": "Join here: https://example.invalid/sessions/spike",
    }
    if args.meet_at_creation:
        # The control: the Meet made with the event, as `main` does today. A
        # knock here too means the guests, not the late patch, are the cause.
        body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        event = (
            api.events()
            .insert(
                calendarId=calendar["id"], sendUpdates="all", conferenceDataVersion=1, body=body
            )
            .execute()
        )
        print(f"CONTROL: created {event['id']} WITH its Meet: {event.get('hangoutLink')!r}")
        print("join it as each guest, this account absent: straight in, or asked to wait?")
        return 0
    event = api.events().insert(calendarId=calendar["id"], sendUpdates="all", body=body).execute()
    print(f"created {event['id']} with no conference; hangoutLink={event.get('hangoutLink')!r}")
    print("check the guest's inbox and calendar now: the invite should carry no Meet link")
    # A pause rather than a prompt, so it runs from a non-interactive shell: long
    # enough for the invite, which carries no link, to be sent before the patch.
    print("patching the Meet in 20s...")
    time.sleep(20)

    # One id for both patches: the build retries with the session's own id, so
    # what matters is that a repeat returns the same Meet, not a second one.
    request_id = str(uuid.uuid4())

    def patch() -> dict:
        return (
            api.events()
            .patch(
                calendarId=calendar["id"],
                eventId=event["id"],
                conferenceDataVersion=1,
                sendUpdates="none",
                body={
                    "conferenceData": {
                        "createRequest": {
                            "requestId": request_id,
                            "conferenceSolutionKey": {"type": "hangoutsMeet"},
                        }
                    }
                },
            )
            .execute()
        )

    began = time.monotonic()
    patched = patch()
    status = status_of(patched)
    link = patched.get("hangoutLink")
    print(f"patch answered in {time.monotonic() - began:.1f}s: status={status!r} link={link!r}")
    while status == "pending" and time.monotonic() - began < 60:
        time.sleep(1)
        got = api.events().get(calendarId=calendar["id"], eventId=event["id"]).execute()
        status, link = status_of(got), got.get("hangoutLink")
        print(f"  {time.monotonic() - began:.1f}s: status={status!r} link={link!r}")

    # Measured both ways on 2026-10-09: once the same Meet, once `403 Rate
    # Limit Exceeded` a second after the first. Either is a result, so print it.
    try:
        repeat = patch().get("hangoutLink")
        print(f"Q1b same requestId again: link={repeat!r} (same Meet: {repeat == link})")
    except Exception as exc:
        print(f"Q1b same requestId again: refused ({exc})")

    print()
    print("Q1 above. Now, by hand:")
    print("  Q2  refresh the guest's calendar: is the Meet link there?")
    print("      and did any email arrive for the patch?")
    print("  Q3  join the link signed in as the GUEST, this account absent:")
    print("      straight in, or asked to wait?")
    print("then: uv run ... python scripts/meet_patch_spike.py --cleanup-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
