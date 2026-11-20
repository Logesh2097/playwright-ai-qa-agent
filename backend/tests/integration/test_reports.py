"""Reports, exports, evidence access, retention and history through the API.

The database and Redis are real; the engine is a stand-in.
"""

import csv
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from app.db.context import set_current_org
from app.models import AuditLog, TestEvidence
from app.services.evidence import EvidenceStore
from app.services.evidence_access import prune_expired
from tests.conftest import Account, Api
from tests.engine_fakes import PNG
from tests.integration.test_executions import Rig

pytestmark = pytest.mark.integration


async def report_of(
    rig: Rig, execution: dict[str, Any], account: Account | None = None
) -> dict[str, Any]:
    response = await (account or rig.owner).get(f"{rig.base}/executions/{execution['id']}/report")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def viewer_of(api: Api, rig: Rig) -> Account:
    viewer = await api.invite_and_join(rig.owner, "viewer@acme.io", "viewer")
    await rig.owner.post(f"{rig.base}/members", json={"user_id": viewer.user_id, "role": "member"})
    return viewer


async def evidence_of(rig: Rig, execution: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = (
        await rig.owner.get(f"{rig.base}/executions/{execution['id']}/evidence")
    ).json()
    return found


# --- report accuracy ----------------------------------------------------------------------


async def test_the_report_agrees_with_the_stored_results_in_every_count(api: Api) -> None:
    rig = await Rig(api).setup()
    names = ["passed", "failed", "error", "flaky", "healed", "passed"]
    cases = [await rig.case(str(500 + n * 10)) for n in range(len(names))]
    unscripted = await rig.case("700", script=None)
    rig.engine.outcomes = {c["key"]: n for c, n in zip(cases, names, strict=True)}
    suite = (
        await rig.owner.post(
            f"{rig.base}/test-suites",
            json={
                "name": "Loyalty",
                "application_id": rig.ws.application_id,
                "case_ids": [c["id"] for c in [*cases, unscripted]],
            },
        )
    ).json()
    assert (
        await rig.owner.post(f"{rig.base}/test-suites/{suite['id']}/approve")
    ).status_code == 200
    started = await rig.start(suite_id=suite["id"], config={"healing": {"mode": "recover"}})
    assert started.status_code == 202, started.text
    await api.run_jobs()
    execution = await rig.get(started.json()["id"])

    report = await report_of(rig, execution)

    assert report["totals"] == {
        "total": 7, "passed": 2, "failed": 1, "skipped": 1, "blocked": 0, "error": 1, "flaky": 1,
        "needs_review": 1, "pass_rate": 0.3333,
    }  # fmt: skip
    # The same numbers the execution itself reports.
    for name in (
        "total",
        "passed",
        "failed",
        "skipped",
        "blocked",
        "error",
        "flaky",
        "needs_review",
    ):
        assert report["totals"][name] == execution["totals"][name], name
    assert [r["status"] for r in report["results"]] == [r["status"] for r in execution["results"]]
    assert (report["execution"]["id"], report["execution"]["verdict"]) == (
        execution["id"],
        "failed",
    )
    assert report["execution"]["status"] == "completed"
    assert report["execution"]["started_at"] and report["execution"]["finished_at"]
    assert (
        report["execution"]["duration_ms"] is not None and report["execution"]["duration_ms"] >= 0
    )
    assert report["suite"] == {"id": suite["id"], "name": "Loyalty"}
    assert report["project"]["id"] == rig.ws.project_id and report["project"]["name"]
    assert report["application"]["id"] == rig.ws.application_id
    assert (
        report["environment"]["id"] == rig.ws.environment_id and report["environment"]["base_url"]
    )
    assert report["instruction"] is None
    assert report["failure_categories"] == {
        "test_assertion_failure": 1, "test_automation_failure": 1, "inconclusive": 2,
    }  # fmt: skip
    assert [f["status"] for f in report["failures"]] == ["failed", "error"]
    failure = report["failures"][0]
    assert failure["summary"].endswith("did not pass") and failure["failed_step"].startswith(
        "Step 5"
    )
    assert (failure["expected"], failure["actual"]) == ({"equals": "50"}, {"text": "51"})
    assert len(failure["screenshot_ids"]) == 1
    detail = report["results"][1]
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 4 + ["failed"]
    assert detail["steps"][4]["started_at"] and detail["steps"][4]["duration_ms"] == 120
    assert (
        detail["evidence"][0]["kind"] == "screenshot"
        and detail["evidence"][0]["step_position"] == 5
    )
    assert detail["expected_result"] == "51 points are shown" and detail["version"] >= 1
    flaky = report["results"][3]
    assert sorted({s["attempt"] for s in flaky["steps"]}) == [1, 2] and flaky["attempts"] == 2
    healed = report["results"][4]
    assert healed["healed"] is True and [s for s in healed["steps"] if s["healed_from"]]


async def test_a_workflow_report_names_the_instruction_and_shows_the_ai_suggestion_as_one(
    api: Api,
) -> None:
    from tests.integration.test_workflow import Rig as WorkflowRig

    rig = await WorkflowRig(api).setup()
    rig.engine.outcomes = {"TC-0002": "failed"}
    done = await rig.finish()
    base = rig.ws.project_url

    response = await rig.owner.get(f"{base}/executions/{done['execution_ids'][0]}/report")
    assert response.status_code == 200, response.text
    report = response.json()

    assert report["instruction"]["id"] == done["instruction_id"] and report["instruction"]["text"]
    failed = next(r for r in report["results"] if r["status"] == "failed")
    suggestion = failed["ai_suggestion"]
    assert suggestion["advisory"] is True and "Not a confirmed root cause" in suggestion["note"]
    assert (
        suggestion["possible_root_cause"] and failed["failure_category"] == "test_assertion_failure"
    )
    assert any("AI-generated suggestion" in note for note in report["notes"])
    assert (report["totals"]["failed"], report["execution"]["verdict"]) == (1, "failed")


# --- exports ------------------------------------------------------------------------------


async def test_json_csv_and_pdf_exports_carry_the_same_report(api: Api) -> None:
    rig = await Rig(api).setup()
    good, bad = await rig.case("500"), await rig.case("510")
    rig.engine.outcomes = {bad["key"]: "failed"}
    execution = await rig.execute(good, bad)
    report = await report_of(rig, execution)
    url = f"{rig.base}/executions/{execution['id']}/report/export"

    as_json = await rig.owner.get(url, params={"format": "json"})
    as_csv = await rig.owner.get(url, params={"format": "csv"})
    as_pdf = await rig.owner.get(url, params={"format": "pdf"})

    for response, media, extension in ((as_json, "application/json", ".json"),
                                       (as_csv, "text/csv", ".csv"), (as_pdf, "application/pdf", ".pdf")):  # fmt: skip
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith(media)
        disposition = response.headers["content-disposition"]
        assert disposition == f'attachment; filename="test-report-{execution["id"]}{extension}"'
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "private, no-store"
    exported = as_json.json()
    assert exported["totals"] == report["totals"] and exported["results"] == report["results"]
    rows = list(csv.DictReader(io.StringIO(as_csv.content.decode("utf-8-sig"))))
    assert [(r["case_key"], r["status"]) for r in rows] == [
        (good["key"], "passed"),
        (bad["key"], "failed"),
    ]
    assert (
        rows[1]["failure_category"] == "test_assertion_failure"
        and rows[1]["expected"] == '{"equals": "50"}'
    )
    assert rows[1]["execution_id"] == execution["id"] and rows[1]["evidence_files"] == "1"
    assert as_pdf.content.startswith(b"%PDF-") and len(as_pdf.content) > 2000

    assert (await rig.owner.get(url)).headers["content-type"].startswith("application/json")
    assert (await rig.owner.get(url, params={"format": "html"})).status_code == 422
    assert (await rig.owner.get(url, params={"format": "../etc"})).status_code == 422
    exports = [row for row in await rig.rows(AuditLog) if row.action == "report.export"]
    assert sorted(row.changes["format"] for row in exports) == ["csv", "json", "json", "pdf"]


async def test_reports_and_exports_are_only_for_those_who_can_see_the_project(api: Api) -> None:
    anon = api.client()
    rig = await Rig(api).setup()
    case = await rig.case()
    execution = await rig.execute(case)
    viewer = await viewer_of(api, rig)
    colleague = await api.invite_and_join(
        rig.owner, "dev@acme.io", "developer"
    )  # not in the project
    outsider = await api.register("owner@other.io", organization="Other")
    paths = [
        f"{rig.base}/executions/{execution['id']}/report",
        f"{rig.base}/executions/{execution['id']}/report/export?format=pdf",
        f"{rig.base}/execution-history",
        f"{rig.base}/dashboard",
        f"{rig.base}/trends",
        f"{rig.base}/failures",
        f"{rig.base}/test-cases/{case['id']}/history",
    ]
    for path in paths:
        assert (await viewer.get(path)).status_code == 200, path
        assert (await colleague.get(path)).status_code == 404, path
        assert (await outsider.get(path)).status_code == 404, path
        assert (await anon.get(path)).status_code == 401, path
    assert (await rig.owner.get(f"{rig.base}/executions/{uuid.uuid4()}/report")).status_code == 404


# --- evidence: capture, validation, limits ------------------------------------------------


async def test_console_and_network_logs_are_kept_for_failures_redacted_and_optional_for_passes(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    good, bad = await rig.case("500"), await rig.case("510")
    rig.engine.outcomes = {bad["key"]: "failed"}
    rig.engine.console = [
        {"at": "2026-11-19T10:00:00+00:00", "type": "log", "text": "loaded"},
        {"at": "2026-11-19T10:00:01+00:00", "type": "error",
         "text": "401 from https://api.acme.io/me?token=abc123secret Authorization: Bearer abcdefghijklmnop"},
    ]  # fmt: skip

    execution = await rig.execute(good, bad)

    found = await evidence_of(rig, execution)
    assert sorted(e["kind"] for e in found) == ["console_log", "screenshot"]
    log = next(e for e in found if e["kind"] == "console_log")
    assert (log["result_id"], log["content_type"], log["redaction_status"]) == (
        execution["results"][1]["id"], "application/json", "applied",
    )  # fmt: skip
    content = await rig.owner.get(
        f"{rig.base}/executions/{execution['id']}/evidence/{log['id']}/content"
    )
    document = content.json()
    assert [m["type"] for m in document["messages"]] == ["log", "error"] and document[
        "attempt"
    ] == 1
    assert "abc123secret" not in content.text and "abcdefghijklmnop" not in content.text
    assert "[REDACTED]" in content.text
    # The full log is a file; only the error lines stay with the result.
    failed = execution["results"][1]
    assert "console_log" not in failed["details"] and len(failed["details"]["console_errors"]) == 1

    everything = await rig.execute(good, config={"evidence": {"logs": "always"}})
    assert [e["kind"] for e in await evidence_of(rig, everything)] == ["console_log"]
    nothing = await rig.execute(bad, config={"evidence": {"logs": "off"}})
    assert [e["kind"] for e in await evidence_of(rig, nothing)] == ["screenshot"]
    assert (
        await rig.start(case_ids=[good["id"]], config={"evidence": {"logs": "sometimes"}})
    ).status_code == 422


@pytest.mark.parametrize(
    ("content", "settings", "note"),
    [
        (
            b"<html><script>alert(document.cookie)</script></html>",
            {},
            "screenshot_not_stored:invalid_content",
        ),
        (b"", {}, "screenshot_not_stored:empty"),
        (
            PNG + b"x" * 500,
            {"evidence_max_screenshot_bytes": 100},
            "screenshot_not_stored:too_large",
        ),
        (PNG, {"evidence_max_execution_bytes": 5}, "screenshot_not_stored:execution_quota"),
    ],
)
async def test_a_file_that_is_not_a_valid_screenshot_or_does_not_fit_is_not_stored(
    api: Api, content: bytes, settings: dict[str, int], note: str
) -> None:
    rig = await Rig(api, **settings).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "failed"}
    rig.engine.artifact_bytes = content

    execution = await rig.execute(case)

    # The result stands; the file is simply not there, and the result says why.
    result = execution["results"][0]
    assert (result["status"], execution["verdict"]) == ("failed", "failed")
    assert note in result["details"]["evidence_notes"]
    assert await evidence_of(rig, execution) == []
    assert (await report_of(rig, execution))["failures"][0]["screenshot_ids"] == []


async def test_the_quota_of_an_execution_counts_all_its_files(api: Api) -> None:
    rig = await Rig(api, evidence_max_execution_bytes=len(PNG) * 2 + 5).setup()
    cases = [await rig.case(str(500 + n * 10)) for n in range(3)]
    rig.engine.outcomes = {c["key"]: "failed" for c in cases}

    execution = await rig.execute(*cases)

    assert len(await evidence_of(rig, execution)) == 2
    notes = [r["details"]["evidence_notes"] for r in execution["results"]]
    assert notes == [[], [], ["screenshot_not_stored:execution_quota"]]


# --- evidence: who may read it ------------------------------------------------------------


async def test_evidence_is_served_only_to_people_who_can_see_its_execution(api: Api) -> None:
    anon = api.client()
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "failed"}
    first, second = await rig.execute(case), await rig.execute(case)
    shot = (await evidence_of(rig, first))[0]
    viewer = await viewer_of(api, rig)
    colleague = await api.invite_and_join(rig.owner, "dev@acme.io", "developer")
    outsider = await api.register("owner@other.io", organization="Other")
    url = f"{rig.base}/executions/{first['id']}/evidence/{shot['id']}/content"

    served = await viewer.get(url)
    assert served.status_code == 200 and served.content == PNG
    assert served.headers["content-type"] == "image/png"
    assert (
        served.headers["content-disposition"]
        == f'attachment; filename="screenshot-{shot["id"]}.png"'
    )
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["cache-control"] == "private, no-store"
    assert served.headers["referrer-policy"] == "no-referrer"
    assert "sandbox" in served.headers["content-security-policy"]

    assert (await anon.get(url)).status_code == 401  # no public address
    assert (await colleague.get(url)).status_code == 404  # same organization, not in the project
    assert (await outsider.get(url)).status_code == 404
    # Not through another execution of the same project,
    wrong = f"{rig.base}/executions/{second['id']}/evidence/{shot['id']}/content"
    assert (await rig.owner.get(wrong)).status_code == 404
    # nor through the outsider's own project.
    theirs = await outsider.create_project()
    crossed = f"{outsider.projects_url}/{theirs['id']}/executions/{first['id']}/evidence/{shot['id']}/content"
    assert (await outsider.get(crossed)).status_code == 404
    assert (
        await rig.owner.get(f"{rig.base}/executions/{first['id']}/evidence/{uuid.uuid4()}/content")
    ).status_code == 404


async def test_a_download_address_works_briefly_for_its_user_and_stops_when_access_does(
    api: Api,
) -> None:
    anon = api.client()
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "failed"}
    execution = await rig.execute(case)
    shot = (await evidence_of(rig, execution))[0]
    viewer = await viewer_of(api, rig)
    outsider = await api.register("owner@other.io", organization="Other")
    issue = f"{rig.base}/executions/{execution['id']}/evidence/{shot['id']}/download-url"

    issued = await viewer.post(issue)
    assert issued.status_code == 200, issued.text
    address = issued.json()["url"]
    expires = datetime.fromisoformat(issued.json()["expires_at"])
    assert timedelta(0) < expires - datetime.now(UTC) <= timedelta(seconds=300)
    assert address.startswith("/api/v1/evidence-downloads/") and shot["id"] not in address

    # A browser follows the address without a session header.
    fetched = await anon.get(address)
    assert fetched.status_code == 200 and fetched.content == PNG
    assert fetched.headers["cache-control"] == "private, no-store"
    assert "attachment" in fetched.headers["content-disposition"]

    assert (await outsider.post(issue)).status_code == 404
    assert (await anon.post(issue)).status_code == 401
    assert (await anon.get(address[:-6] + "AAAAAA")).status_code == 401
    assert (await anon.get("/api/v1/evidence-downloads/" + viewer.token)).status_code == 401
    assert (await anon.get("/api/v1/evidence-downloads/not-a-token")).status_code == 401
    issued_rows = [row for row in await rig.rows(AuditLog) if row.action == "evidence.download_url"]
    assert len(issued_rows) == 1 and str(issued_rows[0].actor_id) == viewer.user_id

    # Removing the viewer from the organization ends the address at once.
    members = (await rig.owner.get(f"/api/v1/organizations/{rig.owner.org_id}/members")).json()
    member_id = next(m["id"] for m in members if m["user_id"] == viewer.user_id)
    removed = await rig.owner.delete(
        f"/api/v1/organizations/{rig.owner.org_id}/members/{member_id}"
    )
    assert removed.status_code == 204
    assert (await anon.get(address)).status_code == 404

    # Signing out ends the owner's own addresses too.
    mine = (await rig.owner.post(issue)).json()["url"]
    assert (await anon.get(mine)).status_code == 200
    assert (
        await rig.owner.client.post("/api/v1/auth/logout", headers=rig.owner.headers)
    ).status_code == 204
    assert (await anon.get(mine)).status_code == 401


# --- retention ----------------------------------------------------------------------------


async def test_evidence_past_its_retention_is_unreachable_and_then_deleted(api: Api) -> None:
    anon = api.client()
    rig = await Rig(api, evidence_retention_days=7).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "failed"}
    old, recent = await rig.execute(case), await rig.execute(case)
    old_shot, recent_shot = (await evidence_of(rig, old))[0], (await evidence_of(rig, recent))[0]
    kept_until = datetime.fromisoformat(old_shot["expires_at"])
    assert timedelta(days=6, hours=23) < kept_until - datetime.now(UTC) <= timedelta(days=7)
    store = EvidenceStore(api.app.state.settings.evidence_root)
    organization = uuid.UUID(rig.owner.org_id)
    row = next(r for r in await rig.rows(TestEvidence) if str(r.id) == old_shot["id"])
    assert store.read(organization, row.storage_key) == PNG
    address = (
        await rig.owner.post(
            f"{rig.base}/executions/{old['id']}/evidence/{old_shot['id']}/download-url"
        )
    ).json()["url"]

    async with api.app.state.session_factory() as session, session.begin():
        await set_current_org(session, organization)
        await session.execute(
            update(TestEvidence)
            .where(TestEvidence.id == row.id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )

    # Expired: gone for readers at once, even before the job has run.
    content = f"{rig.base}/executions/{old['id']}/evidence/{old_shot['id']}/content"
    assert (await rig.owner.get(content)).status_code == 404
    assert (await anon.get(address)).status_code == 404
    report = await report_of(rig, old)
    assert report["failures"][0]["screenshot_ids"] == []
    assert any("no longer available" in note for note in report["notes"])

    counts = await prune_expired(api.app.state.session_factory, store)

    assert counts == {"removed": 1, "already_missing": 0}
    assert store.read(organization, row.storage_key) is None
    assert [str(r.id) for r in await rig.rows(TestEvidence)] == [recent_shot["id"]]
    kept = f"{rig.base}/executions/{recent['id']}/evidence/{recent_shot['id']}/content"
    assert (await rig.owner.get(kept)).status_code == 200
    # The result and its report outlive the file.
    assert (await rig.get(old["id"]))["verdict"] == "failed"
    assert await prune_expired(api.app.state.session_factory, store) == {
        "removed": 0,
        "already_missing": 0,
    }


# --- history ------------------------------------------------------------------------------


async def test_execution_history_filters_and_pages(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.sequences = {case["key"]: ["passed", "failed", "passed", "failed", "passed"]}
    runs = [await rig.execute(case) for _ in range(5)]
    url = f"{rig.base}/execution-history"

    everything = (await rig.owner.get(url)).json()
    assert (everything["total"], everything["limit"], everything["offset"]) == (5, 50, 0)
    assert [e["id"] for e in everything["items"]] == [
        r["id"] for r in reversed(runs)
    ]  # newest first

    page = (await rig.owner.get(url, params={"limit": 2, "offset": 2})).json()
    assert page["total"] == 5 and [e["id"] for e in page["items"]] == [runs[2]["id"], runs[1]["id"]]
    failed = (await rig.owner.get(url, params={"verdict": "failed"})).json()
    assert failed["total"] == 2 and {e["verdict"] for e in failed["items"]} == {"failed"}
    here = (await rig.owner.get(url, params={"environment_id": rig.ws.environment_id})).json()
    elsewhere = (await rig.owner.get(url, params={"environment_id": str(uuid.uuid4())})).json()
    assert (here["total"], elsewhere["total"], elsewhere["items"]) == (5, 0, [])
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert (await rig.owner.get(url, params={"since": later})).json()["total"] == 0
    assert (await rig.owner.get(url, params={"until": later, "status": "completed"})).json()[
        "total"
    ] == 5
    for bad in (
        {"limit": 0},
        {"limit": 500},
        {"offset": -1},
        {"verdict": "great"},
        {"status": "x"},
    ):
        assert (await rig.owner.get(url, params=bad)).status_code == 422, bad


async def test_test_case_history_and_failed_test_history(api: Api) -> None:
    rig = await Rig(api).setup()
    wobbly, steady = await rig.case("500"), await rig.case("510")
    rig.engine.sequences = {wobbly["key"]: ["passed", "failed", "error", "passed"]}
    runs = [await rig.execute(wobbly, steady) for _ in range(4)]

    history = (await rig.owner.get(f"{rig.base}/test-cases/{wobbly['id']}/history")).json()

    assert [h["status"] for h in history] == ["passed", "error", "failed", "passed"]  # newest first
    assert [h["execution_id"] for h in history] == [r["id"] for r in reversed(runs)]
    assert {h["case_key"] for h in history} == {wobbly["key"]}
    assert [h["failure_category"] for h in history] == [
        None, "test_automation_failure", "test_assertion_failure", None,
    ]  # fmt: skip
    assert all(h["environment_id"] == rig.ws.environment_id and h["finished_at"] for h in history)
    assert (
        len(
            (
                await rig.owner.get(
                    f"{rig.base}/test-cases/{wobbly['id']}/history", params={"limit": 2}
                )
            ).json()
        )
        == 2
    )
    assert (await rig.owner.get(f"{rig.base}/test-cases/{uuid.uuid4()}/history")).status_code == 404

    failures = (await rig.owner.get(f"{rig.base}/failures")).json()
    assert [(f["case_key"], f["status"]) for f in failures] == [
        (wobbly["key"], "error"),
        (wobbly["key"], "failed"),
    ]
    assert failures[1]["failure_summary"].endswith("did not pass")
    assert (await rig.owner.get(f"{rig.base}/failures", params={"days": 0})).status_code == 422


async def test_comparing_two_executions_shows_what_regressed_and_what_was_fixed(api: Api) -> None:
    rig = await Rig(api).setup()
    a, b, c, d = [await rig.case(str(500 + n * 10)) for n in range(4)]
    rig.engine.outcomes = {b["key"]: "failed", d["key"]: "failed"}
    before = await rig.execute(a, b, c, d)
    rig.engine.outcomes = {a["key"]: "failed", d["key"]: "failed"}
    extra = await rig.case("600")
    after = await rig.execute(a, b, d, extra)
    url = f"{rig.base}/execution-comparison"

    found = (await rig.owner.get(url, params={"base": before["id"], "target": after["id"]})).json()

    assert found["changes"] == {"regressed": 1, "fixed": 1, "still_failing": 1, "changed": 0,
                                "added": 1, "removed": 1, "unchanged": 0}  # fmt: skip
    by_key = {row["case_key"]: row for row in found["cases"]}
    assert (by_key[a["key"]]["base_status"], by_key[a["key"]]["target_status"], by_key[a["key"]]["change"]) == (
        "passed", "failed", "regressed",
    )  # fmt: skip
    assert by_key[b["key"]]["change"] == "fixed" and by_key[d["key"]]["change"] == "still_failing"
    assert (by_key[c["key"]]["change"], by_key[c["key"]]["target_status"]) == ("removed", None)
    assert (by_key[extra["key"]]["change"], by_key[extra["key"]]["base_status"]) == ("added", None)
    assert found["cases"][0]["change"] == "regressed"
    assert (found["base"]["id"], found["target"]["id"], found["same_environment"]) == (
        before["id"],
        after["id"],
        True,
    )
    assert (found["base"]["results"]["pass_rate"], found["target"]["results"]["pass_rate"]) == (
        0.5,
        0.5,
    )
    assert by_key[a["key"]]["target_category"] == "test_assertion_failure"

    assert (
        await rig.owner.get(url, params={"base": before["id"], "target": before["id"]})
    ).status_code == 422
    assert (
        await rig.owner.get(url, params={"base": before["id"], "target": str(uuid.uuid4())})
    ).status_code == 404
    assert (await rig.owner.get(url, params={"base": before["id"]})).status_code == 422


async def test_trends_and_dashboard_figures_follow_from_the_stored_results(api: Api) -> None:
    rig = await Rig(api, execution_limit=100).setup()
    steady, broken, wobbly = await rig.case("500"), await rig.case("510"), await rig.case("520")
    await rig.case("530", script=None)  # approved, not automated
    await rig.case("540", approve=False)  # not approved
    rig.engine.outcomes = {broken["key"]: "failed"}
    rig.engine.sequences = {wobbly["key"]: ["passed", "transient", "passed", "transient"]}
    for _ in range(4):
        await rig.execute(steady, broken, wobbly)
    await rig.execute(steady)

    dashboard = (await rig.owner.get(f"{rig.base}/dashboard")).json()

    assert (dashboard["total_executions"], dashboard["finished_executions"]) == (5, 5)
    assert (
        dashboard["executions_by_verdict"]["failed"] == 4
        and dashboard["executions_by_verdict"]["passed"] == 1
    )
    assert (dashboard["execution_pass_rate"], dashboard["execution_failure_rate"]) == (0.2, 0.8)
    rates = dashboard["results"]
    assert (rates["ran"], rates["passed"], rates["failed"], rates["error"]) == (13, 7, 4, 2)
    assert (rates["pass_rate"], rates["failure_rate"]) == (0.5385, 0.4615)
    assert dashboard["flaky_test_count"] == 1 and dashboard["flaky_case_ids"] == [wobbly["id"]]
    assert dashboard["average_execution_duration_ms"] is not None
    failing = dashboard["frequently_failing"]
    assert [(f["case_key"], f["failures"], f["runs"]) for f in failing] == [
        (broken["key"], 4, 4), (wobbly["key"], 2, 4),
    ]  # fmt: skip
    assert failing[0]["most_common_category"] == "test_assertion_failure"
    coverage = dashboard["coverage"]
    assert (coverage["test_cases"], coverage["approved"], coverage["automated"]) == (5, 4, 3)
    assert (coverage["executed"], coverage["passing"]) == (3, 1)
    assert "not code coverage" in coverage["note"]
    assert "code_coverage" not in json.dumps(dashboard)
    assert len(dashboard["recent_executions"]) == 5 and dashboard["truncated"] is False
    assert dashboard["recent_executions"][0]["verdict"] == "passed"

    trends = (await rig.owner.get(f"{rig.base}/trends", params={"days": 7})).json()
    assert len(trends["points"]) == 7 and trends["window_days"] == 7
    today = trends["points"][-1]
    assert today["date"] == datetime.now(UTC).date().isoformat()
    assert (today["executions"], today["verdicts"]) == (5, {"passed": 1, "failed": 4})
    assert today["results"]["pass_rate"] == rates["pass_rate"]
    assert all(point["executions"] == 0 for point in trends["points"][:-1])
    for bad in ({"days": 0}, {"days": 9999}):
        assert (await rig.owner.get(f"{rig.base}/trends", params=bad)).status_code == 422
        assert (await rig.owner.get(f"{rig.base}/dashboard", params=bad)).status_code == 422
    empty = await api.register("owner@empty.io", organization="Empty")
    project = await empty.create_project()
    nothing = (await empty.get(f"{empty.projects_url}/{project['id']}/dashboard")).json()
    assert (
        nothing["total_executions"],
        nothing["execution_pass_rate"],
        nothing["results"]["pass_rate"],
    ) == (0, None, None)


# --- live progress and the organization dashboard (Phase 12) ------------------------------


def sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for block in body.split("\n\n"):
        lines = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if ": " in line and not line.startswith(":")
        )
        if "event" in lines:
            found.append((lines["event"], json.loads(lines["data"])))
    return found


async def test_live_progress_shows_only_what_the_engine_reported(api: Api) -> None:
    rig = await Rig(api).setup()
    good, bad = await rig.case("500"), await rig.case("510")
    rig.engine.outcomes = {bad["key"]: "failed"}
    started = await rig.start(case_ids=[good["id"], bad["id"]])
    url = f"{rig.base}/executions/{started.json()['id']}"

    queued = (await rig.owner.get(f"{url}/live")).json()
    assert (queued["status"], queued["finished"], queued["current"]) == ("queued", False, None)
    assert [r["status"] for r in queued["results"]] == ["pending", "pending"]
    assert queued["totals"]["pending"] == 2 and queued["totals"]["passed"] == 0

    await api.run_jobs()
    stream = await rig.owner.get(f"{url}/events")

    assert stream.status_code == 200 and stream.headers["content-type"].startswith(
        "text/event-stream"
    )
    events = sse_events(stream.text)
    kinds = [kind for kind, _ in events]
    assert kinds[-2:] == ["state", "end"] and set(kinds) == {"step", "state", "end"}
    steps = [data for kind, data in events if kind == "step"]
    assert [s["seq"] for s in steps] == list(range(1, len(steps) + 1))
    assert [s["type"] for s in steps].count("case_started") == 2
    assert [s["type"] for s in steps].count("case_finished") == 2
    reported = [s for s in steps if s["type"] == "step_finished"]
    assert (
        len(reported) == 10
        and reported[-1]["status"] == "failed"
        and reported[-1]["action"] == "assert_expression"
    )
    # Values typed, expected and found are not in the live view.
    assert all("expected" not in s and "actual" not in s and "value" not in s for s in reported)
    state = next(data for kind, data in reversed(events) if kind == "state")
    assert (state["status"], state["verdict"], state["finished"], state["current"]) == (
        "completed",
        "failed",
        True,
        None,
    )
    assert [r["status"] for r in state["results"]] == ["passed", "failed"]
    assert events[-1][1] == {"status": "completed", "verdict": "failed"}

    # Resuming after entry 5 repeats nothing before it.
    resumed = sse_events(
        (
            await rig.owner.client.get(
                f"{url}/events", headers={**rig.owner.headers, "Last-Event-ID": "5"}
            )
        ).text
    )
    assert next(d["seq"] for k, d in resumed if k == "step") == 6

    viewer = await viewer_of(api, rig)
    outsider = await api.register("owner@other.io", organization="Other")
    assert (await viewer.get(f"{url}/events")).status_code == 200
    for path in (f"{url}/events", f"{url}/live"):
        assert (await outsider.get(path)).status_code == 404
        assert (await api.client().get(path)).status_code == 401
    assert (await rig.owner.get(f"{rig.base}/executions/{uuid.uuid4()}/events")).status_code == 404


async def test_the_organization_dashboard_covers_only_the_projects_the_caller_can_see(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    good, bad = await rig.case("500"), await rig.case("510")
    rig.engine.outcomes = {bad["key"]: "failed"}
    await rig.execute(good, bad)
    await rig.execute(good)
    hidden = await rig.owner.create_project("Hidden")
    url = f"/api/v1/organizations/{rig.owner.org_id}/dashboard"

    mine = (await rig.owner.get(url)).json()
    assert (mine["total_projects"], mine["active_projects"], mine["total_executions"]) == (2, 2, 2)
    assert (mine["results"]["passed"], mine["results"]["failed"], mine["results"]["pass_rate"]) == (
        2,
        1,
        0.6667,
    )
    assert len(mine["points"]) == 30 and mine["points"][-1]["executions"] == 2
    assert [
        (f["case_key"], f["failures"], f["project_id"]) for f in mine["frequently_failing"]
    ] == [(bad["key"], 1, rig.ws.project_id)]
    assert [e["project_id"] for e in mine["recent_executions"]] == [rig.ws.project_id] * 2
    assert mine["recent_executions"][0]["verdict"] == "passed"

    # A developer who is in no project sees none of it.
    developer = await api.invite_and_join(rig.owner, "dev@acme.io", "developer")
    theirs = (await developer.get(url)).json()
    assert (theirs["total_projects"], theirs["total_executions"], theirs["recent_executions"]) == (
        0,
        0,
        [],
    )
    assert theirs["results"]["pass_rate"] is None
    await rig.owner.post(
        f"{rig.owner.projects_url}/{hidden['id']}/members",
        json={"user_id": developer.user_id, "role": "member"},
    )
    assert (await developer.get(url)).json()["total_projects"] == 1
    outsider = await api.register("owner@other.io", organization="Other")
    assert (await outsider.get(url)).status_code == 404
    assert (await rig.owner.get(url, params={"days": 0})).status_code == 422
