"""Test reports: one model of a report, built from stored results, and its renderings.

    builder.py    rows from the database -> `ExecutionReport`
    model.py      what a report contains
    exports.py    `ExecutionReport` -> JSON, CSV, PDF
    analytics.py  history, comparison, trends and dashboard figures

Every figure in a report is counted from the results table when the report is
asked for. Nothing is copied from an earlier report, and no model output is
read to produce a number or a status.
"""
