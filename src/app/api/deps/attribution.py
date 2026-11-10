"""Where the caller came from (PRD 30 PR 2).

`CurrentUserDep` is the only identity involved: nothing here takes a user from
the path or the body, and the store names `user_id` in its statement.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.api.deps.core import CurrentUserDep, SessionDep
from app.api.schemas.attribution import AcquisitionWrite
from app.infra.db.acquisition_store import Acquisition, record_acquisition

__all__ = ["RecordedAcquisitionDep"]


async def recorded_acquisition(
    body: AcquisitionWrite, user: CurrentUserDep, session: SessionDep
) -> Acquisition:
    """Record the caller's acquisition the first time; later calls read it. Commits."""
    acquisition = await record_acquisition(
        session,
        user["id"],
        source=body.source,
        channel=body.channel,
        referral_code=body.referral_code,
    )
    await session.commit()
    return acquisition


RecordedAcquisitionDep = Annotated[Acquisition, Depends(recorded_acquisition)]
