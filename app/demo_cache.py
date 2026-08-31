"""
In-memory cache for the ephemeral hero-case demo endpoint (GET
/demo/hero-case/investigate). Not a DB column: the hero case has no
persistent exception row to key a cache against -- a fresh isolated
in-memory database is built and discarded on every /demo/hero-case call.

Lives in its own tiny module, separate from both app/main.py and
app/startup.py, purely to avoid a circular import: app/startup.py needs to
WRITE this cache (boot-time pre-warming) and app/main.py needs to READ it
(the endpoint), but app/main.py already imports app/startup.py to register
the FastAPI startup event -- so startup.py cannot import back from main.py.
Both import this neutral module instead.

Stored as a plain dict (matching app.investigation_agent.
investigation_result_to_dict's shape), not the Pydantic InvestigationOut
model, so this module doesn't need to depend on Pydantic or on app.main's
schema definitions either.

Cleared on process restart, same as any other in-memory state.
"""
from typing import Any, Dict, Optional

hero_case_investigation: Optional[Dict[str, Any]] = None
