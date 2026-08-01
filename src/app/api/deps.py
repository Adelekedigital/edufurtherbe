"""FastAPI dependency wiring.

One of the two sanctioned composition points (the other is ``main.py``), and
exempt from the layer check for that reason.
"""

from typing import Annotated

from fastapi import Depends

from app.core.config import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]
