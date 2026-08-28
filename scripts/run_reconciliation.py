"""
Runs matching (app/matching.py) followed by the bridge calculation (app/bridge.py)
across every SettlementBatch, then prints a summary table.

Pure orchestration -- no computation happens in this file itself, and no AI/LLM
involvement anywhere in the chain.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app import matching, bridge

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def fmt_amount(x):
    return f"{x:,.2f}" if x is not None else "-"


def main():
    db = SessionLocal()
    try:
        match_results = matching.run_matching(db)
        bridge_results = bridge.compute_bridge(db)

        match_by_batch = {r.batch_id: r for r in match_results}

        columns = [
            ("batch_id", 9),
            ("matched", 9),
            ("confidence", 12),
            ("bridge_net", 15),
            ("total_net", 15),
            ("bank_amount", 15),
            ("variance", 13),
            ("reconciled", 12),
        ]

        header = "".join(name.rjust(width) for name, width in columns)
        print(header)
        print("-" * len(header))

        for br in bridge_results:
            mr = match_by_batch[br.batch_id]
            row = [
                str(br.batch_id),
                "yes" if mr.matched else "no",
                f"{mr.confidence_score:.2f}" if mr.confidence_score is not None else "-",
                fmt_amount(br.bridge_net),
                fmt_amount(br.total_net),
                fmt_amount(br.matched_bank_amount),
                fmt_amount(br.variance),
                "yes" if br.is_reconciled else "no",
            ]
            print("".join(cell.rjust(width) for cell, (_, width) in zip(row, columns)))

        print()
        unmatched = [r for r in match_results if not r.matched]
        unreconciled = [r for r in bridge_results if not r.is_reconciled]
        print(f"Summary: {len(match_results)} batches, {len(unmatched)} unmatched, {len(unreconciled)} not reconciled.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
