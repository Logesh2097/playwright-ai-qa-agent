#!/usr/bin/env python3
"""Run approved QA Agent tests from a CI pipeline and fail the job unless they verifiably pass.

One file, standard library only (Python 3.9+). Copy it into a pipeline or fetch
it from your QA Agent installation's repository.

    QA_AGENT_URL=https://qa.example.com QA_AGENT_API_KEY=qak_... \\
    python qa_agent_ci.py --project <id> --environment <id> --suite <id> --junit results.xml

The API key is read from the environment only, never from the command line,
so it does not appear in process lists or shell history.

Exit codes
    0  every test ran and passed first time, as written
    1  at least one test failed
    2  nothing failed, but the run is not a verified pass (an error, tests that did not
       run, a pass only on retry, or a result that needs review)
    3  the execution did not finish (cancelled, timed out, engine unavailable)
    4  the tests could not be started or followed (configuration, network, time limit)

Only exit code 0 is success. `--allow-flaky` makes a pass-on-retry count as success;
nothing else is ever relaxed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

EXIT_PASSED, EXIT_FAILED, EXIT_UNVERIFIED, EXIT_INCOMPLETE, EXIT_TOOL = 0, 1, 2, 3, 4
USER_AGENT = "qa-agent-ci/1"


class ToolError(Exception):
    """The run could not be started or followed. The message never contains the key."""


def log(message: str) -> None:
    print(f"[qa-agent] {message}", flush=True)  # noqa: T201 - a command-line tool


class Api:
    def __init__(self, base_url: str, key: str, *, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/") + "/api/v1"
        self._key = key
        self._timeout = timeout

    def request(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self._base + path, data=data, method=method)  # noqa: S310
        request.add_header("Authorization", f"Bearer {self._key}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise ToolError(f"The QA Agent server could not be reached: {reason}") from None

    def json(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> Any:
        status, raw = self.request(method, path, body, headers)
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        if status >= 400:
            detail = parsed.get("detail") or parsed.get("title") or "no details"
            code = parsed.get("code", "error")
            raise ToolError(f"The server answered {status} ({code}): {detail}")
        return parsed


def git_from_environment(env: dict[str, str]) -> dict[str, Any]:
    """What the run is for, from the variables GitHub Actions sets. Empty elsewhere."""
    git: dict[str, Any] = {}
    if env.get("GITHUB_REPOSITORY"):
        git["repository"] = env["GITHUB_REPOSITORY"]
    sha = env.get("QA_AGENT_SHA") or env.get("GITHUB_SHA") or ""
    if re.fullmatch(r"[0-9a-f]{7,64}", sha):
        git["sha"] = sha
    if env.get("GITHUB_REF"):
        git["ref"] = env["GITHUB_REF"]
        match = re.fullmatch(r"refs/pull/(\d+)/(?:merge|head)", env["GITHUB_REF"])
        if match:
            git["pull_request"] = int(match.group(1))
    if env.get("GITHUB_SERVER_URL", "").startswith("https://") and env.get("GITHUB_RUN_ID"):
        git["run_url"] = (
            f"{env['GITHUB_SERVER_URL']}/{env.get('GITHUB_REPOSITORY', '')}/actions/runs/{env['GITHUB_RUN_ID']}"
        )
    return git


def default_idempotency_key(env: dict[str, str], selection: str) -> str | None:
    """The same for a re-sent request of one pipeline run; different for a re-run."""
    if env.get("GITHUB_RUN_ID"):
        return f"gha-{env['GITHUB_RUN_ID']}-{env.get('GITHUB_RUN_ATTEMPT', '1')}-{selection}"[:200]
    return None


def exit_code_for(result: dict[str, Any], *, allow_flaky: bool) -> int:
    """From the server's verified outcome. Unknown answers are never treated as success."""
    if result.get("passed") is True and result.get("outcome") == "passed":
        return EXIT_PASSED
    if allow_flaky and result.get("verdict") == "flaky" and result.get("outcome") == "unverified":
        return EXIT_PASSED
    code = result.get("exit_code")
    if code in (EXIT_FAILED, EXIT_UNVERIFIED, EXIT_INCOMPLETE):
        return int(code)
    return EXIT_UNVERIFIED


def summary_markdown(result: dict[str, Any], code: int) -> str:
    totals = result.get("totals") or {}
    lines = [
        f"### QA Agent: {result.get('outcome', 'unknown')}",
        "",
        str(result.get("summary", "")),
        "",
        "| Total | Passed | Failed | Error | Skipped | Flaky | Needs review |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        "| "
        + " | ".join(
            str(totals.get(k, 0))
            for k in ("total", "passed", "failed", "error", "skipped", "flaky", "needs_review")
        )
        + " |",
        "",
    ]
    for failure in (result.get("failures") or [])[:20]:
        text = f"{failure.get('case_key', '')} {failure.get('title', '')}".replace(
            "|", "/"
        ).replace("`", "'")
        lines.append(
            f"- `{text[:160]}` — {failure.get('status')} ({failure.get('failure_category') or 'n/a'})"
        )
    lines += [
        "",
        f"[Open the report]({result.get('report_url', '')}) (requires signing in)",
        "",
        f"Exit code {code}.",
    ]
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace, env: dict[str, str]) -> int:
    url, key = args.url or env.get("QA_AGENT_URL", ""), env.get("QA_AGENT_API_KEY", "")
    if not url:
        raise ToolError("Set QA_AGENT_URL or pass --url.")
    if not key:
        raise ToolError("Set QA_AGENT_API_KEY in the environment (use a CI secret).")
    if not url.startswith("https://") and not args.allow_insecure:
        raise ToolError(
            "The server address must use https. (--allow-insecure exists for local testing.)"
        )
    if bool(args.suite) == bool(args.case):
        raise ToolError("Give either --suite or one or more --case.")
    api = Api(url, key)

    body: dict[str, Any] = {"project_id": args.project, "environment_id": args.environment}
    if args.suite:
        body["suite_id"] = args.suite
    else:
        body["case_ids"] = args.case
    git = git_from_environment(env)
    if git:
        body["git"] = git
    headers = {}
    idempotency = args.idempotency_key or default_idempotency_key(env, args.suite or "cases")
    if idempotency:
        headers["Idempotency-Key"] = idempotency

    result = api.json("POST", "/ci/executions", body, headers)
    execution_id = result["id"]
    log(f"Execution {execution_id} started. Report: {result.get('report_url', '')}")

    deadline = time.monotonic() + args.timeout
    failures_in_a_row = 0
    while not result.get("finished"):
        if time.monotonic() >= deadline:
            raise ToolError(
                f"The execution did not finish within {args.timeout} seconds. It is still running on the server."
            )
        time.sleep(args.poll)
        try:
            result = api.json("GET", f"/ci/executions/{execution_id}")
            failures_in_a_row = 0
        except ToolError as error:
            failures_in_a_row += 1
            if failures_in_a_row >= 5:
                raise
            log(f"Could not read the status ({error}); trying again.")

    if args.junit:
        status, xml = api.request("GET", f"/ci/executions/{execution_id}/junit")
        if status == 200:
            with open(args.junit, "wb") as handle:
                handle.write(xml)
            log(f"JUnit results written to {args.junit}")
        else:
            log(f"The JUnit results could not be fetched (status {status}).")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)

    code = exit_code_for(result, allow_flaky=args.allow_flaky)
    totals = result.get("totals") or {}
    log(f"{result.get('outcome')}: {result.get('summary')}")
    log(
        "total {total}, passed {passed}, failed {failed}, error {error}, skipped {skipped}, "
        "flaky {flaky}, needs review {needs_review}".format(
            **{
                k: totals.get(k, 0)
                for k in ("total", "passed", "failed", "error", "skipped", "flaky", "needs_review")
            }
        )
    )
    for failure in (result.get("failures") or [])[:20]:
        log(f"  {failure.get('status')}: {failure.get('case_key')} {failure.get('title')}")
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(summary_markdown(result, code))
        except OSError:
            pass
    return code


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run approved QA Agent tests and exit by their verified result."
    )
    p.add_argument("--url", help="Address of the QA Agent installation (or QA_AGENT_URL).")
    p.add_argument("--project", required=True, help="Project ID.")
    p.add_argument("--environment", required=True, help="Environment ID to test against.")
    p.add_argument("--suite", help="ID of an approved suite.")
    p.add_argument(
        "--case", action="append", default=[], help="ID of an approved case. May be repeated."
    )
    p.add_argument(
        "--timeout", type=int, default=1800, help="Seconds to wait for the result (default 1800)."
    )
    p.add_argument(
        "--poll", type=float, default=5.0, help="Seconds between status checks (default 5)."
    )
    p.add_argument("--junit", help="Write JUnit XML to this file.")
    p.add_argument("--json", help="Write the result as JSON to this file.")
    p.add_argument("--idempotency-key", help="Defaults to the GitHub Actions run and attempt.")
    p.add_argument("--allow-flaky", action="store_true", help="Count a pass-on-retry as success.")
    p.add_argument(
        "--allow-insecure", action="store_true", help="Allow http:// (local testing only)."
    )
    return p


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args, dict(os.environ) if env is None else env)
    except ToolError as error:
        log(f"error: {error}")
        return EXIT_TOOL


if __name__ == "__main__":
    sys.exit(main())
