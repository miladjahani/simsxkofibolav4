"""Native workbook calculation test.

Requires app/data/model.xlsx (the original Excel model). The workbook is not
committed to the repository, so this test is skipped when it is absent. All
other tests (test_process.py) run without it.
"""

import os

import pytest

WORKBOOK = os.path.join("app", "data", "model.xlsx")

pytestmark = pytest.mark.skipif(
    not os.path.exists(WORKBOOK),
    reason=f"{WORKBOOK} not present (workbook is not committed to the repo)",
)


def test_native_workbook_calculate():
    from app.native_engine import NativeWorkbook

    r = NativeWorkbook(WORKBOOK).calculate(
        "2Ex1S A1", only_cells=["B7", "B8", "B9", "D5", "D6"]
    )
    print(r["values"])
    assert r["values"]
