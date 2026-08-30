"""
Single source of truth for converting integer paise (the database's native
currency unit -- see the float-to-paise migration) into decimal rupees for
JSON API responses.

This is applied at exactly ONE boundary: the point where a Pydantic response
model (or a dict that becomes AI-prompt text, which ends up in a response too)
is built. Everything before that point -- database columns, matching.py,
bridge.py, exceptions.py, anomaly_detection.py, and the is_reconciled/variance
comparisons inside app/main.py's own _batch_summary() -- keeps working in
paise and must never call this. Converting early would just reintroduce the
float-precision problem this migration exists to eliminate, one step earlier.
"""
from decimal import Decimal
from typing import Optional


def paise_to_rupees(paise: Optional[int]) -> Optional[float]:
    """Converts integer paise to a rupee float via exact Decimal division
    (e.g. 11629435 -> 116294.35), not float division/multiplication, which
    could reintroduce the exact binary-float imprecision this migration exists
    to eliminate. float() is applied only at the very last step, for JSON
    serialization -- every arithmetic step before it is exact Decimal math."""
    if paise is None:
        return None
    return float(Decimal(paise) / 100)
