"""Evaluation of the platform's judgement, on labelled data.

    make evaluate

Two kinds of component are evaluated:

- **rule-based** ones (failure classification, locator recovery, sensitive-step
  detection, test selection, tool selection). These need no model and are
  measured on every run;
- **model-based** ones (requirement understanding, scenario coverage, structured
  output validity, expected-result accuracy). These are measured only when a
  model is configured; otherwise the report says "not measured". No figure is
  ever filled in for something that was not run.

The labels in `datasets.py` say what a careful person would expect, written
from the requirements and not from the code. Results are whatever the code
gives; cases the rules get wrong stay in the dataset and in the report.
"""
