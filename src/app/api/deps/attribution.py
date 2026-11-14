"""Where the caller came from, and their analytics-cookie answer (PRD 30 PR 2).

`CurrentUserDep` is the only identity involved: nothing here takes a user from
the path or the body, and each store names `user_id` in its statements.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends

from app.api.deps.core import CurrentUserDep, SessionDep
from app.api.schemas.attribution import AcquisitionWrite, AnalyticsConsentWrite
from app.infra.db.acquisition_store import Acquisition, record_acquisition
from app.infra.db.analytics_consent_store import record_consent

__all__ = ["RecordedAcquisitionDep", "RecordedConsentDep"]


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


async def recorded_consent(
    body: AnalyticsConsentWrite, user: CurrentUserDep, session: SessionDep
) -> Any:
    """Record the caller's answer, replacing an earlier one. Commits."""
    row = await record_consent(
        session, user["id"], granted=body.granted, version=body.version, method=body.method
    )
    await session.commit()
    return row


RecordedAcquisitionDep = Annotated[Acquisition, Depends(recorded_acquisition)]
RecordedConsentDep = Annotated[Any, Depends(recorded_consent)]
