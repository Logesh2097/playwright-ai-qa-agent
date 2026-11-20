"""A model's explanation of why a test did not pass. A suggestion, never a verdict.

Everything here is advice for the people who read a report. None of it is read
when a status, a category or a verdict is decided.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

LikelyCause = Literal["application_defect", "test_script", "test_data", "environment", "unknown"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SuggestedCase(_Out):
    title: str
    # What the extra case would show that the existing one does not.
    why: str


class FailureExplanation(_Out):
    # The reference given for the result in the request, e.g. "R1".
    ref: str
    likely_cause: LikelyCause
    # What happened, in one or two sentences.
    summary: str
    # A possible cause. A hypothesis for a person to check, not an established fact.
    possible_root_cause: str
    # What the failure means for users or the business if it is real.
    impact: str
    # What in the recorded facts supports the explanation.
    supporting_facts: list[str]
    debugging_steps: list[str]
    suggested_test_cases: list[SuggestedCase]
    confidence: Literal["low", "medium", "high"]


class FailureExplanations(_Out):
    explanations: list[FailureExplanation]
