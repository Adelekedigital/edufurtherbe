"""What the machine callers — QStash, Daily and the job runner — are told back."""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["PresenceRecordedRead", "ReminderQueuedRead", "RuntimeJobRead"]


class ReminderQueuedRead(BaseModel):
    """A response reminder callback was handled."""

    queued: bool = Field(
        description="False when the request no longer needs a nudge; still a success."
    )


class BioDraftRead(BaseModel):
    """A bio draft callback was handled."""

    drafted: bool = Field(
        description=(
            "True when a draft was saved as the bio. False when none was owed, "
            "none came back, or the person wrote their own first; still a success."
        )
    )


class PresenceRecordedRead(BaseModel):
    """A Daily webhook delivery was handled."""

    recorded: bool = Field(description="Whether a party's presence row matched.")


class RuntimeJobRead(BaseModel):
    """The terminal result of one recurring job run."""

    job: str = Field(description="The job's name, as in the path.")
    job_id: str | None = Field(description="The schedule id the delivery carried.")
    status: str = Field(description="`completed`, or `no-op` when every count is zero.")
    counts: dict[str, int] = Field(description="What the job did, per kind of row.")
