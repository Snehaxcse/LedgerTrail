"""
Held-out reconciliation evaluation dataset. Deliberately separate from
scripts/generate_synthetic_data.py's primary demo dataset -- different date
range (Dec 2026, vs the primary's Aug-Nov 2026), different order-ref prefix
("HO-" vs "OD-"), never loaded by app/startup.py's normal pipeline, never
touched by ingest.py. Its only consumer is app/holdout_evaluation.py.

Every case here is fully hardcoded (no random draws) -- these are precise,
individually-designed test cases whose expected classification is derived by
hand-tracing the REAL matching.py/bridge.py/exceptions.py logic, not guessed.
Values are specified directly in integer paise (not rupees converted at parse
time), consistent with the float-to-paise migration: there is no CSV/ingest.py
step in this dataset's path at all, so there's no rupee-decimal stage to skip.

Reuses the primary generator's fee-tier schedule (FEE_TIERS, GST_RATE) --
pure, deterministic, no randomness -- so the held-out data reflects the same
real-world fee structure the primary dataset does, not a bespoke one invented
to make cases easy.

Case types:
  clean_exact_match (x5)          -- no injected error, should reconcile automatically
  date_shifted_match              -- fuzzy match within DATE_WINDOW_DAYS -> TIMING_DIFFERENCE
  fee_mismatch                    -- wrong fee tier applied -> FEE_TIER_MISMATCH
  missing_refund                  -- settlement shows a refund the order doesn't ->
                                      MISSING_REFUND_RECORD
  refund_mismatch_reverse         -- order shows a refund the settlement doesn't reflect
                                      (the opposite direction from missing_refund) ->
                                      REFUND_NOT_IN_SETTLEMENT. This case was originally
                                      planted to demonstrate a real, disclosed engine blind
                                      spot (this direction went undetected before the reverse
                                      check existed); kept as the same case now that it's
                                      fixed, so its history stays visible in git rather than
                                      being replaced by a fresh-looking case with no memory
                                      of the bug it caught.
  bank_amount_mismatch_large      -- bank credits an amount far outside AMOUNT_TOLERANCE ->
                                      UNMATCHED_BATCH (correct regardless of tolerance size)
  bank_amount_mismatch_small      -- bank credits an amount within the restored
                                      AMOUNT_TOLERANCE (100 paise) but not exactly equal ->
                                      UNEXPLAINED_VARIANCE, actually exercised now that
                                      matching.AMOUNT_TOLERANCE is nonzero again
  duplicate_looking               -- same order_ref twice, identical amounts -> DUPLICATE_ENTRY
  ambiguous_candidate_match (x2)  -- two batches sharing one valid bank-transaction candidate ->
                                      AmbiguousMatchError, handled explicitly, not a crash
"""
import datetime

from scripts.generate_synthetic_data import FEE_TIERS, GST_RATE, fee_rate_for_amount

GROUND_TRUTH_UNDETECTED = "UNDETECTED_KNOWN_LIMITATION"


def _round(x):
    return round(x)


def _rate_for_gross_paise(gross_paise):
    """fee_rate_for_amount()'s tier thresholds (1000, 5000) are rupee-scale --
    every gross value in this file is paise, so every tier lookup must divide
    by 100 first. A first draft of this file called fee_rate_for_amount()
    directly on paise values and silently picked the wrong tier for any gross
    under Rs.5,00,000 paise-equivalent; caught by hand-checking case 05's
    expected delta against its own hand computation, not by luck."""
    return fee_rate_for_amount(gross_paise / 100)


def _fee_tax(gross_paise, rate):
    fee = _round(gross_paise * rate)
    tax = _round(fee * GST_RATE)
    return fee, tax


def _wrong_rate(correct_rate):
    rates = [r for _, r in FEE_TIERS]
    idx = rates.index(correct_rate)
    return rates[idx - 1] if idx > 0 else rates[idx + 1]


def _make_bank_ref(i):
    return f"UTR9{i:011d}"


def _make_narration(ref):
    return f"NEFT CR: HDFC BANK {ref} RAZORPAY SETTLEMENT"


def build_holdout_dataset():
    """Returns a dict: {batches, entries, bank_txns, orders, ground_truth,
    ambiguous_batch_labels}. Every *_paise field is an integer, ready for
    direct DB insertion -- no CSV, no rupee-decimal intermediate stage."""
    batches = []      # {label, settlement_date, total_gross, total_refunds, total_fees, total_tax, total_net}
    entries = []       # {batch_label, order_ref, gross_amount, fee, tax, refund, net_amount}
    bank_txns = []     # {label, date, amount, reference, description}
    orders = []        # {order_ref, amount, status, refund_amount, fee_amount}
    ground_truth = []  # {batch_label, case_type, expected_classification, expected_delta_paise, note}

    base_date = datetime.date(2026, 12, 1)

    def add_simple_batch(label, day_offset, gross, rate=None, bank_day_offset=None,
                          fee_override=None, tax_override=None, refund_entry=0,
                          refund_order=None, bank_amount_override=None,
                          order_fee_override=None):
        """One-entry batch: settlement entry, matching order record, one bank txn
        (unless bank_amount_override says otherwise). Handles the common case;
        the fee-mismatch/refund-mismatch/duplicate/ambiguous cases build by hand
        below since each needs a genuinely different shape."""
        settlement_date = base_date + datetime.timedelta(days=day_offset)
        rate = rate if rate is not None else _rate_for_gross_paise(gross)
        fee, tax = _fee_tax(gross, rate)
        if fee_override is not None:
            fee = fee_override
        if tax_override is not None:
            tax = tax_override
        net = gross - refund_entry - fee - tax

        order_ref = f"HO-{label}-0001"
        batches.append({
            "label": label, "settlement_date": settlement_date,
            "total_gross": gross, "total_refunds": refund_entry,
            "total_fees": fee, "total_tax": tax, "total_net": net,
        })
        entries.append({
            "batch_label": label, "order_ref": order_ref, "gross_amount": gross,
            "fee": fee, "tax": tax, "refund": refund_entry, "net_amount": net,
        })
        orders.append({
            "order_ref": order_ref, "amount": gross, "status": "completed",
            "refund_amount": refund_order if refund_order is not None else refund_entry,
            "fee_amount": order_fee_override if order_fee_override is not None else fee,
        })
        bank_date = settlement_date + datetime.timedelta(days=bank_day_offset or 0)
        bank_amount = bank_amount_override if bank_amount_override is not None else net
        ref = _make_bank_ref(len(bank_txns) + 1)
        bank_txns.append({
            "label": label, "date": bank_date, "amount": bank_amount,
            "reference": ref, "description": _make_narration(ref),
        })
        return net, fee, tax

    # --- Clean, exact match: no exception expected (5 of these for a meaningful
    # true-negative count) ---
    add_simple_batch("01", 0, 2_000_000)
    ground_truth.append({"batch_label": "01", "case_type": "clean_exact_match",
                          "expected_classification": None, "expected_delta_paise": None})

    add_simple_batch("02", 1, 3_000_000)
    ground_truth.append({"batch_label": "02", "case_type": "clean_exact_match",
                          "expected_classification": None, "expected_delta_paise": None})

    add_simple_batch("03", 2, 1_500_000)
    ground_truth.append({"batch_label": "03", "case_type": "clean_exact_match",
                          "expected_classification": None, "expected_delta_paise": None})

    add_simple_batch("11", 14, 2_500_000)
    ground_truth.append({"batch_label": "11", "case_type": "clean_exact_match",
                          "expected_classification": None, "expected_delta_paise": None})

    add_simple_batch("12", 15, 1_300_000)
    ground_truth.append({"batch_label": "12", "case_type": "clean_exact_match",
                          "expected_classification": None, "expected_delta_paise": None})

    # --- Date-shifted match: bank credit 2 days after settlement_date, still
    # within matching.DATE_WINDOW_DAYS (3) -> fuzzy match -> TIMING_DIFFERENCE ---
    add_simple_batch("04", 3, 2_200_000, bank_day_offset=2)
    ground_truth.append({"batch_label": "04", "case_type": "date_shifted_match",
                          "expected_classification": "TIMING_DIFFERENCE", "expected_delta_paise": 0})

    # --- Fee mismatch: settlement charges the WRONG tier's rate; order record
    # keeps the correct expected fee. Gross chosen in the middle tier (>=1000,
    # <5000 rupees -> rate 0.023) so "one tier off" (0.020) is unambiguous. ---
    gross5 = 300_000  # Rs.3,000 -> correct tier rate 0.023
    correct_rate5 = _rate_for_gross_paise(gross5)
    wrong_rate5 = _wrong_rate(correct_rate5)
    correct_fee5, correct_tax5 = _fee_tax(gross5, correct_rate5)
    wrong_fee5, wrong_tax5 = _fee_tax(gross5, wrong_rate5)
    add_simple_batch("05", 6, gross5, fee_override=wrong_fee5, tax_override=wrong_tax5,
                      order_fee_override=correct_fee5)
    ground_truth.append({"batch_label": "05", "case_type": "fee_mismatch",
                          "expected_classification": "FEE_TIER_MISMATCH",
                          "expected_delta_paise": abs(wrong_fee5 - correct_fee5)})

    # --- Missing refund: settlement entry shows a refund the order record has
    # no knowledge of (forward direction -- the direction exceptions.py actually
    # checks: entry.refund - order_refund > TOLERANCE). ---
    refund6 = 200_000
    add_simple_batch("06", 7, 1_000_000, refund_entry=refund6, refund_order=0)
    ground_truth.append({"batch_label": "06", "case_type": "missing_refund",
                          "expected_classification": "MISSING_REFUND_RECORD",
                          "expected_delta_paise": refund6})

    # --- Refund mismatch, REVERSE direction: the order record shows a refund
    # the settlement entry does NOT reflect (entry.refund=0, order.refund_amount>0).
    # Originally planted to demonstrate a real, disclosed engine blind spot
    # (exceptions.py's refund check used to only fire in the other direction);
    # now that the reverse check exists (see exceptions.py's
    # REFUND_NOT_IN_SETTLEMENT), this same case is expected to be correctly
    # detected -- kept as the same case rather than duplicated, since a second,
    # near-identical case would test nothing the first doesn't already cover. ---
    refund7 = 150_000
    add_simple_batch("07", 8, 1_200_000, refund_entry=0, refund_order=refund7)
    ground_truth.append({"batch_label": "07", "case_type": "refund_mismatch_reverse",
                          "expected_classification": "REFUND_NOT_IN_SETTLEMENT",
                          "expected_delta_paise": refund7})

    # --- Bank amount mismatch, LARGE (Rs.1,000): settlement is internally
    # consistent but the bank credited a wildly different amount -- far beyond
    # matching.AMOUNT_TOLERANCE (Rs.1.00) in either its original or its
    # exact-equality form, so this batch never matches any bank transaction at
    # all -> UNMATCHED_BATCH, not a matched-with-variance UNEXPLAINED_VARIANCE.
    # A real bank-side discrepancy this large means the settlement genuinely
    # has no corresponding bank credit, so UNMATCHED_BATCH is the correct,
    # intended outcome here regardless of tolerance -- see case "13" below for
    # a mismatch small enough to actually exercise UNEXPLAINED_VARIANCE. ---
    net8, _, _ = add_simple_batch("08", 9, 1_800_000, bank_amount_override=1_700_000)
    ground_truth.append({"batch_label": "08", "case_type": "bank_amount_mismatch_large",
                          "expected_classification": "UNMATCHED_BATCH",
                          "expected_delta_paise": abs(net8)})

    # --- Bank amount mismatch, SMALL (50 paise): within matching.AMOUNT_TOLERANCE
    # (100 paise, restored to its original pre-migration value) so this batch
    # DOES match -- but bridge.py/exceptions.py's own tolerances are still exact
    # zero, so the nonzero match_diff isn't silently absorbed. Same-day match
    # (match_type stays "exact" -- that field tracks DATE exactness, not amount),
    # no refund/fee issue, so nothing else claims this discrepancy first ->
    # UNEXPLAINED_VARIANCE, exercised for real rather than just in a comment. ---
    gross13 = 1_900_000
    rate13 = _rate_for_gross_paise(gross13)
    fee13, tax13 = _fee_tax(gross13, rate13)
    net13 = gross13 - fee13 - tax13
    variance13 = 50
    add_simple_batch("13", 16, gross13, rate=rate13, bank_amount_override=net13 - variance13)
    ground_truth.append({"batch_label": "13", "case_type": "bank_amount_mismatch_small",
                          "expected_classification": "UNEXPLAINED_VARIANCE",
                          "expected_delta_paise": variance13,
                          "note": ("50-paise bank/settlement variance, within the restored "
                                    "AMOUNT_TOLERANCE (100 paise) so the batch still matches -- "
                                    "exceptions.py's own TOLERANCE stays exact-zero, so this small "
                                    "variance is still flagged, not silently absorbed.")})

    # --- Duplicate-looking records: same order_ref appears twice with identical
    # amounts; batch totals reflect ONE real payout (matches the primary
    # dataset's own convention), so bridge_net (sum of both entries) diverges
    # from the declared total_net -> DUPLICATE_ENTRY. ---
    gross9, rate9 = 1_600_000, _rate_for_gross_paise(1_600_000)
    fee9, tax9 = _fee_tax(gross9, rate9)
    net9 = gross9 - fee9 - tax9
    settlement_date9 = base_date + datetime.timedelta(days=10)
    order_ref9 = "HO-09-0001"
    batches.append({"label": "09", "settlement_date": settlement_date9, "total_gross": gross9,
                     "total_refunds": 0, "total_fees": fee9, "total_tax": tax9, "total_net": net9})
    entries.append({"batch_label": "09", "order_ref": order_ref9, "gross_amount": gross9,
                     "fee": fee9, "tax": tax9, "refund": 0, "net_amount": net9})
    entries.append({"batch_label": "09", "order_ref": order_ref9, "gross_amount": gross9,
                     "fee": fee9, "tax": tax9, "refund": 0, "net_amount": net9})
    orders.append({"order_ref": order_ref9, "amount": gross9, "status": "completed",
                    "refund_amount": 0, "fee_amount": fee9})
    ref9 = _make_bank_ref(len(bank_txns) + 1)
    bank_txns.append({"label": "09", "date": settlement_date9, "amount": net9,
                       "reference": ref9, "description": _make_narration(ref9)})
    ground_truth.append({"batch_label": "09", "case_type": "duplicate_looking",
                          "expected_classification": "DUPLICATE_ENTRY", "expected_delta_paise": 0})

    # --- Ambiguous candidate match: two batches with IDENTICAL total_net, dates
    # close enough together that ONE bank transaction (dated between them) is a
    # valid amount+date candidate for BOTH -> matching.AmbiguousMatchError. ---
    shared_net = 1_067_550
    gross10, rate10 = 1_100_000, _rate_for_gross_paise(1_100_000)
    fee10, tax10 = _fee_tax(gross10, rate10)
    assert gross10 - fee10 - tax10 == shared_net, "ambiguous-pair amounts must match exactly"

    date_10a = base_date + datetime.timedelta(days=11)
    date_10b = base_date + datetime.timedelta(days=13)
    shared_bank_date = base_date + datetime.timedelta(days=12)

    for label, settlement_date in (("10a", date_10a), ("10b", date_10b)):
        order_ref = f"HO-{label}-0001"
        batches.append({"label": label, "settlement_date": settlement_date, "total_gross": gross10,
                         "total_refunds": 0, "total_fees": fee10, "total_tax": tax10, "total_net": shared_net})
        entries.append({"batch_label": label, "order_ref": order_ref, "gross_amount": gross10,
                         "fee": fee10, "tax": tax10, "refund": 0, "net_amount": shared_net})
        orders.append({"order_ref": order_ref, "amount": gross10, "status": "completed",
                        "refund_amount": 0, "fee_amount": fee10})

    shared_ref = _make_bank_ref(len(bank_txns) + 1)
    bank_txns.append({"label": "10-shared", "date": shared_bank_date, "amount": shared_net,
                       "reference": shared_ref, "description": _make_narration(shared_ref)})

    ground_truth.append({"batch_label": "10a", "case_type": "ambiguous_candidate_match",
                          "expected_classification": "AMBIGUOUS", "expected_delta_paise": None})
    ground_truth.append({"batch_label": "10b", "case_type": "ambiguous_candidate_match",
                          "expected_classification": "AMBIGUOUS", "expected_delta_paise": None})

    return {
        "batches": batches,
        "entries": entries,
        "bank_txns": bank_txns,
        "orders": orders,
        "ground_truth": ground_truth,
        "ambiguous_batch_labels": {"10a", "10b"},
    }
