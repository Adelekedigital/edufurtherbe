"""Daily spike — measure the six behaviours the room port is about to depend on.

The Google side taught this lesson twice, and neither trap was in the
documentation's foreground: ``conferenceDataVersion=1`` silently dropped the
conference, and free/busy read back empty right after a write. So nothing here
is asserted from Daily's documentation; it is measured against a real account.

    Q1  Does ``privacy: private`` actually refuse a visitor holding the URL and
        no token? The whole withheld-link design rests on it.

    Q2  Does a token's ``nbf`` refuse an early join? This is the time gate that
        replaces "please do not join before the session".

    Q3  Does the meeting record give **per-participant** join and leave times?
        Presence is not attendance; co-presence needs both ends per person.

    Q4  Does the identity we mint onto the token reach that record? Without it
        the times are real and unattributable, which is no better than nothing.

    Q5  **How long after the call does the record appear?** The one that decides
        the architecture. The settlement sweep runs on a schedule; if the record
        lands minutes late, a sweep firing on time reads an empty meeting and
        settles a session that happened as ``no_show``. This is the same shape
        as Google free/busy's indexing lag, which the calendar spike found by
        measuring and would have missed by reading.

    Q6  Does ``exp`` close the room, and what does somebody already inside see?

Nothing here is committed and nothing is decided by running it. It prints what
happened; the decision follows.

    uv run python scripts/daily_spike.py

**It needs a human.** Rooms produce no participant records without somebody
joining one, so the script creates the room, hands you two URLs, and waits. Join
as both parties in two browsers, leave at different times, then let it measure.

The API key is read from ``script-data-dev/daily_api_key.txt`` — a file rather
than an environment variable, following the calendar spike, so that
non-negotiable #6 holds everywhere and the directory's existing ignore rule
covers it.

Cleanup is offered at the end and can be re-run alone with ``--cleanup-only``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.stdout.reconfigure(encoding="utf-8")

API = "https://api.daily.co/v1"

HERE = Path(__file__).parent
#: Searched in order, exactly as the calendar spike searches for its client
#: secret: `script-data-dev/` is ignored wholesale, where `scripts/` would need
#: a rule naming this file.
SECRET_DIRS = (HERE.parent / "script-data-dev", HERE)
STATE_PATH = HERE / "daily_spike_state.json"

#: How far ahead the room opens, so Q2 has something to refuse. Short enough
#: that the spike is not a coffee break.
GATE = dt.timedelta(minutes=2)

#: How long to keep asking for the meeting record before giving up on Q5. Ten
#: minutes is generous; the answer that matters is *how long*, and "longer than
#: ten minutes" is already an answer that changes the design.
PATIENCE = dt.timedelta(minutes=10)
POLL_EVERY = 15


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def api_key() -> str:
    for directory in SECRET_DIRS:
        candidate = directory / "daily_api_key.txt"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "No API key. Put your Daily key in script-data-dev/daily_api_key.txt "
        "(one line, no quotes). Dashboard -> Developers -> API key."
    )


def client() -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {api_key()}"},
        timeout=30.0,
    )


def show(label: str, response: httpx.Response) -> Any:
    """Print the raw answer, not a summary of it.

    The calendar spike's two surprises were both visible in a response nobody
    printed. A spike that reports its own interpretation can only tell you what
    its author already believed.
    """
    print(f"\n--- {label}: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        print(response.text[:2000])
        return None
    print(json.dumps(body, indent=2)[:4000])
    return body


def create(api: httpx.Client) -> dict[str, Any]:
    """A private room that opens in two minutes and closes in one hour."""
    opens = dt.datetime.now(dt.UTC) + GATE
    closes = opens + dt.timedelta(hours=1)
    name = f"efspike-{uuid.uuid4().hex[:10]}"

    rule("Creating a private room")
    room = show(
        "POST /rooms",
        api.post(
            "/rooms",
            json={
                "name": name,
                "privacy": "private",
                "properties": {
                    "nbf": int(opens.timestamp()),
                    "exp": int(closes.timestamp()),
                    # So a refused join is visibly refused rather than a blank
                    # page somebody has to interpret.
                    "enable_prejoin_ui": True,
                },
            },
        ),
    )
    if room is None or "url" not in room:
        raise SystemExit("Room creation failed — see the response above.")

    tokens: dict[str, str] = {}
    for role, owner in (("mentor", True), ("mentee", False)):
        # **The identity is the point of Q4.** `user_id` is what we would set to
        # our own `users.id`, and if it does not survive into the meeting record
        # then per-participant attendance is unattributable.
        minted = show(
            f"POST /meeting-tokens ({role})",
            api.post(
                "/meeting-tokens",
                json={
                    "properties": {
                        "room_name": name,
                        "user_name": f"Spike {role}",
                        "user_id": f"spike-{role}-{uuid.uuid4().hex[:8]}",
                        "is_owner": owner,
                        "nbf": int(opens.timestamp()),
                        "exp": int(closes.timestamp()),
                    }
                },
            ),
        )
        if minted is None or "token" not in minted:
            raise SystemExit(f"Token minting failed for {role} — see above.")
        tokens[role] = str(minted["token"])

    state = {
        "room": name,
        "url": str(room["url"]),
        "opens": opens.isoformat(),
        "closes": closes.isoformat(),
        "tokens": tokens,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def instructions(state: dict[str, Any]) -> None:
    opens = dt.datetime.fromisoformat(str(state["opens"]))
    rule("Now do this, in a browser")
    print(
        f"""
The room opens at {opens.isoformat()} (about two minutes from creation).

  Q1  BEFORE anything else, open the bare URL with NO token:
        {state["url"]}
      Expected if `privacy: private` works: refused, not admitted.

  Q2  Still before {opens.isoformat()}, open the MENTOR url below.
      Expected if `nbf` is enforced: refused for being early.

  Q3/Q4  After it opens, join in TWO browsers (or one plus a private window):

        mentor  {state["url"]}?t={state["tokens"]["mentor"]}
        mentee  {state["url"]}?t={state["tokens"]["mentee"]}

      Then LEAVE AT DIFFERENT TIMES — mentee first, a minute apart is plenty.
      Overlapping and then not overlapping is the behaviour the whole
      co-presence question is about, so make the two intervals differ.

  Q6  Optional: stay in past `exp` ({state["closes"]}) and note what happens.

Note the wall-clock time you left each one. The record's numbers are only
checkable against something you observed.
"""
    )


def measure(api: httpx.Client, state: dict[str, Any]) -> None:
    """Poll for the meeting record, and time how long it takes to appear.

    **The waiting is the measurement.** Q5 is not "can we read the record" but
    "how long after the call", and a script that read once and reported nothing
    would answer neither.
    """
    rule("Q5 — how long until the meeting record appears")
    started = time.monotonic()
    deadline = started + PATIENCE.total_seconds()
    record: Any = None

    while time.monotonic() < deadline:
        waited = int(time.monotonic() - started)
        response = api.get("/meetings", params={"room": str(state["room"])})
        if response.status_code != 200:
            show(f"GET /meetings after {waited}s", response)
            break
        body = response.json()
        sessions = body.get("data") or []
        if sessions:
            print(f"\n  record appeared after ~{waited}s")
            record = body
            break
        print(f"  {waited:>4}s — no meeting record yet")
        time.sleep(POLL_EVERY)

    if record is None:
        print(
            f"\n  NOTHING after {int(PATIENCE.total_seconds())}s."
            "\n  That is an answer, and a load-bearing one: a settlement sweep"
            "\n  cannot read attendance from a record that is not there yet."
        )
        return

    print(json.dumps(record, indent=2)[:6000])
    rule("Q3 / Q4 — read these off the record above")
    print(
        """
  Q3  Does each participant carry BOTH a join time and a leave time (or a
      duration you can add to the join)? One end is presence; two is an
      interval, and only intervals can overlap.

  Q4  Does the `user_id` you minted onto each token appear against the right
      participant? If it is absent or rewritten, attendance is unattributable
      and per-person figures cannot be built on this.

  And check the times against what you observed. A record that exists but
  disagrees with the wall clock is worse than none.
"""
    )


def cleanup(api: httpx.Client) -> None:
    if not STATE_PATH.exists():
        print("Nothing to clean up.")
        return
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    rule("Cleanup")
    show(f"DELETE /rooms/{state['room']}", api.delete(f"/rooms/{state['room']}"))
    STATE_PATH.unlink()
    print("State file removed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cleanup-only", action="store_true", help="Delete the spike room and stop."
    )
    parser.add_argument(
        "--measure-only",
        action="store_true",
        help="Skip creation and read the record for the room in the state file.",
    )
    args = parser.parse_args()

    with client() as api:
        if args.cleanup_only:
            cleanup(api)
            return 0

        if args.measure_only:
            if not STATE_PATH.exists():
                raise SystemExit("No state file — run without --measure-only first.")
            measure(api, json.loads(STATE_PATH.read_text(encoding="utf-8")))
            return 0

        state = create(api)
        instructions(state)
        input("\nPress ENTER once both of you have joined and left... ")
        measure(api, state)

        print(
            "\nRoom left in place so you can re-read the record with"
            "\n  uv run python scripts/daily_spike.py --measure-only"
            "\nDelete it with"
            "\n  uv run python scripts/daily_spike.py --cleanup-only"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
