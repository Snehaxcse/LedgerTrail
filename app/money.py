"""
Single source of truth for converting integer paise (the database's native
currency unit -- see the float-to-paise migration) into decimal rupees for
JSON API responses.

This is applied at exactly ONE boundary: the point where a Pydantic response
model (or a dict that becomes AI-prompt text, which ends up in a response too)
is built. Everything before that point -- database columns, matching.py,
bridge.py, and the is_reconciled/variance comparisons inside app/main.py's own
_batch_summary() -- keeps working in paise and must never call this.
Converting early would just reintroduce the float-precision problem this
migration exists to eliminate, one step earlier.

exceptions.py and anomaly_detection.py are the same way for their own
classification logic (which must stay paise-native), with ONE narrow,
deliberate exception each: _log_exception_created / _log_anomaly_created call
this when building an AuditEvent's after_state JSON, because that JSON is
returned verbatim by GET /audit-trail and rendered directly in the UI -- a
response boundary like any other, just not a typed Pydantic model. Found live
during a pre-merge regression pass: without this, the audit trail displayed
the raw paise integer as if it were already rupees (e.g. "Rs.79,934.00" for
an exception actually worth Rs.799.34). See those two functions' comments.
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
