"""Why a step did not pass.

`StepFailure`: the step ran and what it checked was not so. The application
did something other than expected: status `failed`.

`StepError`: the step could not be carried out at all (no such element, time
ran out, the address is not allowed). Nothing was verified: status `error`.

The two are never merged: an error is not evidence about the application.
"""

from typing import Any


class StepFailure(Exception):  # noqa: N818 - a failed check, not a program error
    def __init__(self, message: str, *, expected: dict[str, Any], actual: dict[str, Any]) -> None:
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.actual = actual


class StepError(Exception):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
