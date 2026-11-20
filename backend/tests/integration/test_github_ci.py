"""GitHub and CI through the API. The database and Redis are real; GitHub and the
engine are stand-ins."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import pytest
from sqlalchemy import update

from app.db.context import set_current_org
from app.integrations.github import webhooks
from app.models import (
    ApiKey,
    AuditLog,
    GithubDelivery,
    GithubRepository,
    Integration,
    TestExecution,
)
from tests.conftest import Account, Api
from tests.integration.test_executions import LOOK_ONLY, Rig

pytestmark = pytest.mark.integration

TOKEN = "github_pat_11AAAAAAA0_secretsecretsecretsecretsecret"
REPO, REPO_ID = "acme/shop", 4242
SHA1, SHA2 = "a" * 40, "b" * 40


@dataclass
class FakeGitHub:
    """Answers the REST calls the platform makes, and remembers what it was asked to write."""

    files: list[str] = field(default_factory=lambda: ["src/billing/invoice.py"])
    head_sha: str = SHA1
    pr_repo_id: int = REPO_ID
    # path prefix -> status to answer with instead
    failing: dict[str, int] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    statuses: list[dict[str, Any]] = field(default_factory=list)
    comments: dict[int, str] = field(default_factory=dict)
    comment_calls: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        for prefix, status in self.failing.items():
            if path.startswith(prefix):
                return httpx.Response(status, json={"message": "boom"})
        repository = {"id": REPO_ID, "full_name": REPO, "default_branch": "main", "private": True,
                      "archived": False, "html_url": f"https://github.com/{REPO}", "description": "Shop",
                      "permissions": {"pull": True, "push": False}}  # fmt: skip
        if path == "/user":
            return httpx.Response(200, json={"login": "acme-bot"})
        if path == "/user/repos":
            return httpx.Response(
                200, json=[repository] if request.url.params["page"] == "1" else []
            )
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json=repository)
        if path == f"/repos/{REPO}/branches":
            return httpx.Response(
                200, json=[{"name": "main", "commit": {"sha": SHA1}, "protected": True}]
            )
        if path == f"/repos/{REPO}/pulls/7":
            return httpx.Response(200, json={
                "number": 7, "title": "Change rounding", "state": "open", "draft": False,
                "user": {"login": "ada"}, "html_url": f"https://github.com/{REPO}/pull/7", "changed_files": len(self.files),
                "head": {"sha": self.head_sha, "ref": "feature/rounding", "repo": {"id": REPO_ID}},
                "base": {"ref": "main", "repo": {"id": self.pr_repo_id}},
            })  # fmt: skip
        if path == f"/repos/{REPO}/pulls/7/files":
            page = (
                [{"filename": name} for name in self.files]
                if request.url.params["page"] == "1"
                else []
            )
            return httpx.Response(200, json=page)
        if path.startswith(f"/repos/{REPO}/commits/"):
            sha = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"sha": sha, "html_url": "https://github.com/c", "author": {"login": "ada"},
                                             "commit": {"message": "Fix rounding", "committer": {"date": "2026-11-19T10:00:00Z"}}})  # fmt: skip
        if path.startswith(f"/repos/{REPO}/statuses/") and method == "POST":
            self.statuses.append({"sha": path.rsplit("/", 1)[1], **json.loads(request.content)})
            return httpx.Response(201, json={})
        if path == f"/repos/{REPO}/issues/7/comments" and method == "POST":
            comment_id = 9000 + len(self.comments)
            self.comments[comment_id] = json.loads(request.content)["body"]
            self.comment_calls.append("create")
            return httpx.Response(201, json={"id": comment_id})
        if path.startswith(f"/repos/{REPO}/issues/comments/") and method == "PATCH":
            comment_id = int(path.rsplit("/", 1)[1])
            if comment_id not in self.comments:
                return httpx.Response(404, json={"message": "Not Found"})
            self.comments[comment_id] = json.loads(request.content)["body"]
            self.comment_calls.append("update")
            return httpx.Response(200, json={"id": comment_id})
        return httpx.Response(404, json={"message": "Not Found"})


def git_of(execution: TestExecution) -> dict[str, Any]:
    return execution.git_context or {}


class Github:
    """A project with one linked repository and two approved suites."""

    def __init__(self, api: Api, rig: Rig, fake: FakeGitHub) -> None:
        self.api, self.rig, self.fake = api, rig, fake
        self.org_url = f"/api/v1/organizations/{rig.owner.org_id}"
        self.link: dict[str, Any] = {}
        self.connection: dict[str, Any] = {}
        self.secret = ""
        self.suites: dict[str, dict[str, Any]] = {}
        self.cases: dict[str, dict[str, Any]] = {}

    @classmethod
    async def create(cls, api: Api, **environment: Any) -> "Github":
        fake = FakeGitHub()
        api.app.state.github_transport = fake.transport()
        rig = await Rig(api, public_base_url="https://qa.example").setup(**environment)
        self = cls(api, rig, fake)
        connection = await rig.owner.post(
            f"{self.org_url}/github/connections", json={"name": "Bot", "token": TOKEN}
        )
        assert connection.status_code == 201, connection.text
        self.connection = connection.json()
        linked = await rig.owner.post(
            f"{rig.base}/github/repositories",
            json={"connection_id": self.connection["id"], "full_name": REPO},
        )
        assert linked.status_code == 201, linked.text
        self.link, self.secret = linked.json(), linked.json()["webhook_secret"]
        for name, amount in (("Billing", "500"), ("Checkout", "510")):
            case = await rig.case(amount)
            suite = (
                await rig.owner.post(
                    f"{rig.base}/test-suites",
                    json={
                        "name": name,
                        "application_id": rig.ws.application_id,
                        "case_ids": [case["id"]],
                    },
                )
            ).json()
            assert (
                await rig.owner.post(f"{rig.base}/test-suites/{suite['id']}/approve")
            ).status_code == 200
            self.suites[name], self.cases[name] = suite, case
        return self

    @property
    def url(self) -> str:
        return f"{self.rig.base}/github/repositories/{self.link['id']}"

    async def configure(self, account: Account | None = None, **changes: Any) -> Any:
        return await (account or self.rig.owner).patch(self.url, json=changes)

    async def enable(self, **more: Any) -> None:
        settings = {
            "on_pull_request": True, "branches": ["main"], "environment_id": self.rig.ws.environment_id,
            "suite_ids": [s["id"] for s in self.suites.values()], **more,
        }  # fmt: skip
        response = await self.configure(**settings)
        assert response.status_code == 200, response.text
        self.link = response.json()

    def pr_body(
        self, action: str = "opened", *, sha: str = SHA1, base: str = "main", **pull: Any
    ) -> bytes:
        details = {
            "number": 7, "state": "open", "draft": False, "title": "Change rounding", "user": {"login": "ada"},
            "head": {"sha": sha, "ref": "feature/rounding", "repo": {"id": REPO_ID}},
            "base": {"ref": base, "repo": {"id": REPO_ID}}, **pull,
        }  # fmt: skip
        return json.dumps(
            {"action": action, "repository": {"id": REPO_ID}, "pull_request": details}
        ).encode()

    async def deliver(
        self, body: bytes, *, event: str = "pull_request", delivery: str | None = None,
        secret: str | None = None, signature: str | None = None, path: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:  # fmt: skip
        sent = {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
            "X-Hub-Signature-256": signature or webhooks.sign(secret or self.secret, body),
            "Content-Type": "application/json",
            **(headers or {}),
        }
        return await self.api.client().post(
            path or self.link["webhook_url"], content=body, headers=sent
        )

    async def executions(self) -> list[TestExecution]:
        rows = await self.rig.rows(TestExecution)
        return sorted(rows, key=lambda row: row.created_at)

    async def deliveries(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = (await self.rig.owner.get(f"{self.url}/deliveries")).json()
        return found


# --- connecting and linking ---------------------------------------------------------------


async def test_connecting_checks_the_credentials_with_github_and_never_shows_them_again(
    api: Api,
) -> None:
    hub = await Github.create(api)

    assert (hub.connection["account"], hub.connection["auth"], hub.connection["status"]) == (
        "acme-bot",
        "token",
        "active",
    )
    listed = await hub.rig.owner.get(f"{hub.org_url}/github/connections")
    assert (
        TOKEN not in listed.text and "token" not in listed.json()[0] and "secret" not in listed.text
    )
    row = (await hub.rig.rows(Integration))[0]
    assert row.secret_ciphertext and TOKEN.encode() not in row.secret_ciphertext
    assert TOKEN not in json.dumps(row.config)
    logged = [r for r in await hub.rig.rows(AuditLog) if r.action.startswith("github.")]
    assert [r.action for r in logged] == ["github.connect", "github.repository.link"]
    assert TOKEN not in json.dumps([r.changes for r in logged]) and hub.secret not in json.dumps(
        [r.changes for r in logged]
    )

    available = (
        await hub.rig.owner.get(
            f"{hub.org_url}/github/connections/{hub.connection['id']}/repositories"
        )
    ).json()
    assert [(r["full_name"], r["id"], r["permissions"]["pull"]) for r in available] == [
        (REPO, REPO_ID, True)
    ]

    for body, code in (
        ({"name": "Bad", "token": "github_pat_wrongwrongwrongwrongwrong"}, 409),
        (
            {
                "name": "Both",
                "token": TOKEN,
                "app_id": "1",
                "private_key": "k" * 300,
                "installation_id": 1,
            },
            422,
        ),
        ({"name": "Neither"}, 422),
        ({"name": "Half", "app_id": "1"}, 422),
    ):
        refused = await hub.rig.owner.post(f"{hub.org_url}/github/connections", json=body)
        assert refused.status_code == code, (body["name"], refused.text)
        assert "wrongwrong" not in refused.text
    hub.fake.failing["/user"] = 503
    down = await hub.rig.owner.post(
        f"{hub.org_url}/github/connections", json={"name": "Later", "token": TOKEN}
    )
    assert (down.status_code, down.json()["code"]) == (503, "github_unavailable")
    assert len(await hub.rig.rows(Integration)) == 1


async def test_only_admins_manage_connections_and_links_and_other_organizations_cannot_use_them(
    api: Api,
) -> None:
    hub = await Github.create(api)
    developer = await api.invite_and_join(hub.rig.owner, "dev@acme.io", "developer")
    await hub.rig.owner.post(
        f"{hub.rig.base}/members", json={"user_id": developer.user_id, "role": "member"}
    )
    outsider = await api.register("owner@other.io", organization="Other")
    theirs = await outsider.create_project()
    link = {"connection_id": hub.connection["id"], "full_name": REPO}

    assert (await developer.get(f"{hub.org_url}/github/connections")).status_code == 403
    assert (
        await developer.post(
            f"{hub.org_url}/github/connections", json={"name": "x", "token": TOKEN}
        )
    ).status_code == 403
    assert (
        await developer.post(f"{hub.rig.base}/github/repositories", json=link)
    ).status_code == 403
    assert (await hub.configure(developer, on_pull_request=True)).status_code == 403
    assert (await developer.post(f"{hub.url}/webhook-secret")).status_code == 403
    assert (await developer.delete(hub.url)).status_code == 403
    # Members can see what is linked, but never the secret.
    seen = await developer.get(f"{hub.rig.base}/github/repositories")
    assert (
        seen.status_code == 200
        and "webhook_secret" not in seen.json()[0]
        and hub.secret not in seen.text
    )

    assert (await outsider.get(f"{hub.org_url}/github/connections")).status_code == 404
    assert (await outsider.get(hub.url)).status_code == 404
    # Another organization cannot link a repository through this organization's credentials.
    crossed = await outsider.post(
        f"{outsider.projects_url}/{theirs['id']}/github/repositories", json=link
    )
    assert crossed.status_code == 404
    assert (
        await outsider.delete(f"{hub.org_url}/github/connections/{hub.connection['id']}")
    ).status_code == 404


async def test_only_a_repository_the_credentials_can_see_can_be_linked_once_and_nothing_is_on_by_default(
    api: Api,
) -> None:
    hub = await Github.create(api)
    link = hub.link

    assert (link["full_name"], link["github_repo_id"], link["default_branch"], link["private"]) == (
        REPO,
        REPO_ID,
        "main",
        True,
    )
    assert (
        link["on_pull_request"],
        link["on_push"],
        link["status_enabled"],
        link["comment_enabled"],
    ) == (False,) * 4
    assert (
        link["webhook_url"].startswith(f"/api/v1/webhooks/github/{hub.rig.owner.org_id}/")
        and len(hub.secret) >= 32
    )
    assert link["webhook_events"] == ["pull_request", "push"]
    row = (await hub.rig.rows(GithubRepository))[0]
    assert hub.secret.encode() not in row.secret_ciphertext

    base = f"{hub.rig.base}/github/repositories"
    again = await hub.rig.owner.post(
        base, json={"connection_id": hub.connection["id"], "full_name": REPO}
    )
    assert (again.status_code, again.json()["code"]) == (409, "github_already_linked")
    unseen = await hub.rig.owner.post(
        base, json={"connection_id": hub.connection["id"], "full_name": "acme/secret-repo"}
    )
    assert (unseen.status_code, unseen.json()["code"]) == (404, "github_not_found")
    for name in (
        "no-slash",
        "../../etc/passwd",
        "acme/shop/pulls/1",
        "acme/shop?x=1",
        "https://github.com/acme/shop",
    ):
        bad = await hub.rig.owner.post(
            base, json={"connection_id": hub.connection["id"], "full_name": name}
        )
        assert bad.status_code == 422, name
    unknown = await hub.rig.owner.post(
        base, json={"connection_id": str(uuid.uuid4()), "full_name": REPO}
    )
    assert unknown.status_code == 404
    # Nothing but the allowed calls reached GitHub: no call was made for the malformed names.
    assert {r.url.path for r in hub.fake.requests} <= {
        "/user",
        "/user/repos",
        f"/repos/{REPO}",
        "/repos/acme/secret-repo",
    }


async def test_triggers_need_an_environment_suites_and_branches_of_this_project(api: Api) -> None:
    hub = await Github.create(api)
    suites = [s["id"] for s in hub.suites.values()]
    other = await hub.rig.owner.create_project("Other project")
    other_suite = (
        await hub.rig.owner.post(
            f"{hub.rig.owner.projects_url}/{other['id']}/test-suites",
            json={
                "name": "Elsewhere",
                "application_id": (await hub.rig.owner.create_application(other["id"]))["id"],
            },
        )
    ).json()

    assert (await hub.configure(on_pull_request=True)).status_code == 422  # nothing chosen yet
    assert (
        await hub.configure(on_pull_request=True, suite_ids=suites, branches=["main"])
    ).status_code == 422
    assert (await hub.configure(suite_ids=[other_suite["id"]])).status_code == 404
    assert (await hub.configure(environment_id=str(uuid.uuid4()))).status_code == 404
    assert (
        await hub.configure(
            suite_ids=suites, path_rules=[{"pattern": "src/**", "suite_ids": [other_suite["id"]]}]
        )
    ).status_code == 422
    for bad in ({"branches": ["has space"]}, {"selection_mode": "smart"}, {"path_rules": [{"pattern": "x", "suite_ids": []}]},
                {"unknown_setting": True}):  # fmt: skip
        assert (await hub.configure(**bad)).status_code == 422, bad

    await hub.enable(
        selection_mode="affected",
        path_rules=[{"pattern": "src/billing/**", "suite_ids": [suites[0]]}],
    )
    assert (
        hub.link["on_pull_request"] is True
        and hub.link["path_rules"][0]["pattern"] == "src/billing/**"
    )
    assert (
        await hub.configure(branches=[])
    ).status_code == 422  # would leave a trigger without a branch


async def test_repository_branch_pull_request_and_commit_information_comes_from_the_linked_repository(
    api: Api,
) -> None:
    hub = await Github.create(api)
    suites = [s["id"] for s in hub.suites.values()]
    await hub.enable(
        selection_mode="affected",
        path_rules=[{"pattern": "src/billing/**", "suite_ids": [suites[0]]}],
    )

    metadata = (await hub.rig.owner.get(f"{hub.url}/metadata")).json()
    assert metadata["repository"]["full_name"] == REPO and metadata["branches"] == [
        {"name": "main", "sha": SHA1, "protected": True}
    ]
    pull = (await hub.rig.owner.get(f"{hub.url}/pull-requests/7")).json()
    assert (
        pull["number"],
        pull["head_sha"],
        pull["base_ref"],
        pull["author"],
        pull["from_fork"],
    ) == (7, SHA1, "main", "ada", False)
    assert (
        pull["changed_files"] == ["src/billing/invoice.py"]
        and pull["changed_files_complete"] is True
    )
    assert (pull["selection"]["scope"], pull["selection"]["suite_ids"]) == ("affected", [suites[0]])
    commit = (await hub.rig.owner.get(f"{hub.url}/commits/{SHA1}")).json()
    assert (commit["sha"], commit["message"], commit["author"]) == (SHA1, "Fix rounding", "ada")

    suggestion = await hub.rig.owner.post(
        f"{hub.url}/impact", json={"files": ["src/billing/a.py", "infra/deploy.yml"]}
    )
    assert (suggestion.json()["scope"], suggestion.json()["suite_ids"], suggestion.json()["unmatched_files"]) == (
        "full", suites, ["infra/deploy.yml"],
    )  # fmt: skip
    assert (await hub.rig.owner.get(f"{hub.url}/commits/..%2F..%2Fuser")).status_code in (404, 422)
    assert (await hub.rig.owner.get(f"{hub.url}/pull-requests/0")).status_code == 422
    hub.fake.failing[f"/repos/{REPO}"] = 500
    assert (await hub.rig.owner.get(f"{hub.url}/metadata")).status_code == 503


# --- webhook verification -----------------------------------------------------------------


async def test_a_delivery_without_a_valid_signature_is_refused_and_leaves_no_trace(
    api: Api,
) -> None:
    hub = await Github.create(api)
    await hub.enable()
    body = hub.pr_body()
    good = webhooks.sign(hub.secret, body)

    attempts = [
        await hub.deliver(body, signature="sha256=" + "0" * 64),
        await hub.deliver(body, secret="another-secret-entirely-000000000000"),
        await hub.deliver(body, signature=good.replace("sha256=", "sha1=")),
        await hub.deliver(body, signature="not-a-signature"),
        await hub.deliver(hub.pr_body(sha=SHA2), signature=good),  # signed for another body
        await hub.deliver(body + b" ", signature=good),
        # A valid signature for this repository does not open another address.
        await hub.deliver(body, path=hub.link["webhook_url"][:-4] + "AAAA"),
        await hub.deliver(
            body,
            path=f"/api/v1/webhooks/github/{uuid.uuid4()}/{hub.link['webhook_url'].rsplit('/', 1)[1]}",
        ),
    ]
    unsigned = await api.client().post(
        hub.link["webhook_url"],
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": str(uuid.uuid4())},
    )

    assert [r.status_code for r in attempts] == [401] * 8 and unsigned.status_code == 401
    assert {r.json()["code"] for r in attempts} == {"invalid_signature"}
    assert await hub.rig.rows(GithubDelivery) == [] and api.queue.pending == []
    assert await hub.executions() == []


async def test_a_verified_delivery_still_needs_proper_headers_and_a_reasonable_size(
    api: Api,
) -> None:
    hub = await Github.create(api)
    await hub.enable()
    body = hub.pr_body()

    assert (await hub.deliver(body, delivery="x")).status_code == 422
    assert (await hub.deliver(body, event="Pull Request")).status_code == 422
    api.app.state.settings = api.app.state.settings.model_copy(
        update={"github_webhook_max_bytes": 2048}
    )
    big = json.dumps({"action": "opened", "padding": "x" * 5000}).encode()
    too_big = await hub.deliver(big)
    assert (too_big.status_code, too_big.json()["code"]) == (422, "payload_too_large")
    assert (
        await hub.deliver(b'{"zen":"Design for failure.","repository":{"id":4242}}', event="ping")
    ).json() == {"status": "pong", "reason": None}
    assert await hub.rig.rows(GithubDelivery) == []


async def test_rotating_the_secret_ends_the_old_one(api: Api) -> None:
    hub = await Github.create(api)
    await hub.enable()
    old = hub.secret
    rotated = await hub.rig.owner.post(f"{hub.url}/webhook-secret")
    new = rotated.json()["webhook_secret"]

    assert rotated.status_code == 200 and new != old
    assert (await hub.deliver(hub.pr_body(), secret=old)).status_code == 401
    assert (await hub.deliver(hub.pr_body(), secret=new)).status_code == 202


# --- pull request events ------------------------------------------------------------------


async def test_an_opened_pull_request_runs_the_configured_suites_in_the_configured_environment(
    api: Api,
) -> None:
    hub = await Github.create(api)
    await hub.enable(status_enabled=True)

    received = await hub.deliver(
        hub.pr_body("opened"), delivery="11111111-aaaa-bbbb-cccc-000000000001"
    )
    assert (received.status_code, received.json()) == (202, {"status": "accepted", "reason": None})
    assert [name for name, _ in api.queue.pending] == ["process_github_delivery"]
    await api.run_jobs()

    executions = await hub.executions()
    assert len(executions) == 2
    for execution, suite in zip(executions, hub.suites.values(), strict=True):
        assert (execution.trigger, execution.triggered_by, execution.status, execution.verdict) == (
            "github_webhook", None, "completed", "passed",
        )  # fmt: skip
        assert (str(execution.suite_id), str(execution.environment_id)) == (
            suite["id"],
            hub.rig.ws.environment_id,
        )
        context = git_of(execution)
        assert (context["source"], context["repository"], context["repository_id"]) == (
            "github",
            REPO,
            REPO_ID,
        )
        assert (context["pull_request"], context["head_sha"], context["base_ref"]) == (
            7,
            SHA1,
            "main",
        )
        assert context["selection"]["scope"] == "full" and TOKEN not in json.dumps(context)
    # The engine was given the approved suites' cases, nothing from the event.
    assert [plan.cases[0].key for plan in hub.rig.engine.plans] == [
        c["key"] for c in hub.cases.values()
    ]

    delivery = (await hub.deliveries())[0]
    assert (delivery["event"], delivery["action"], delivery["outcome"]) == (
        "pull_request",
        "opened",
        "processed",
    )
    assert delivery["execution_ids"] == [str(e.id) for e in executions]
    assert delivery["subject"]["head_sha"] == SHA1 and "payload" not in delivery
    started = [r for r in await hub.rig.rows(AuditLog) if r.action == "execution.start"]
    assert {(r.actor_type, r.actor_id, r.changes["trigger"]) for r in started} == {
        ("system", None, "github_webhook")
    }

    # Pending first, then the verified result, per suite, on the pull request's commit.
    states = [(s["context"], s["state"]) for s in hub.fake.statuses]
    assert states == [
        ("QA Agent / Billing", "pending"), ("QA Agent / Checkout", "pending"),
        ("QA Agent / Billing", "success"), ("QA Agent / Checkout", "success"),
    ]  # fmt: skip
    assert {s["sha"] for s in hub.fake.statuses} == {SHA1}
    assert (
        hub.fake.statuses[-1]["target_url"]
        == f"https://qa.example/projects/{hub.rig.ws.project_id}/executions/{executions[1].id}"
    )
    assert hub.fake.comments == {}  # comments are a separate switch


@pytest.mark.parametrize(
    ("change", "body", "reason"),
    [
        ({}, {"action": "closed"}, "does not start tests"),
        ({}, {"action": "labeled"}, "does not start tests"),
        ({}, {"base": "develop"}, "not one of the configured branches"),
        ({}, {"draft": True}, "Draft pull requests"),
        ({}, {"state": "closed"}, "not open"),
        ({"on_pull_request": False}, {}, "not switched on"),
        ({"enabled": False}, {}, "switched off"),
    ],
)
async def test_events_the_link_does_not_allow_are_recorded_and_start_nothing(
    api: Api, change: dict[str, Any], body: dict[str, Any], reason: str
) -> None:
    hub = await Github.create(api)
    await hub.enable(status_enabled=True, comment_enabled=True)
    if change:
        assert (await hub.configure(**change)).status_code == 200
    action, base = body.pop("action", "opened"), body.pop("base", "main")

    received = await hub.deliver(hub.pr_body(action, base=base, **body))

    assert received.status_code == 200 and received.json()["status"] == "ignored"
    assert reason in received.json()["reason"]
    assert api.queue.pending == [] and await hub.executions() == []
    assert (await hub.deliveries())[0]["outcome"] == "ignored"
    assert hub.fake.statuses == [] and hub.fake.comments == {}


async def test_an_event_for_another_repository_or_another_event_type_starts_nothing(
    api: Api,
) -> None:
    hub = await Github.create(api)
    await hub.enable()
    foreign = json.loads(hub.pr_body())
    foreign["repository"]["id"] = 9999

    wrong_repository = await hub.deliver(json.dumps(foreign).encode())
    issue = await hub.deliver(b'{"action":"opened","repository":{"id":4242}}', event="issues")
    push = await hub.deliver(
        json.dumps(
            {"ref": "refs/heads/main", "before": SHA2, "after": SHA1, "repository": {"id": REPO_ID}}
        ).encode(),
        event="push",
    )

    assert "different repository" in wrong_repository.json()["reason"]
    assert "is not used" in issue.json()["reason"]
    assert (
        "push is not switched on" in push.json()["reason"]
    )  # pull requests are on, pushes are not
    assert await hub.executions() == []


async def test_new_commits_run_again_and_a_push_to_a_configured_branch_runs_when_switched_on(
    api: Api,
) -> None:
    hub = await Github.create(api)
    await hub.enable(on_push=True)

    await hub.deliver(hub.pr_body("opened", sha=SHA1))
    await api.run_jobs()
    hub.fake.head_sha = SHA2
    await hub.deliver(hub.pr_body("synchronize", sha=SHA2))
    await api.run_jobs()
    pushed = {
        "ref": "refs/heads/main",
        "before": SHA2,
        "after": "c" * 40,
        "repository": {"id": REPO_ID},
        "pusher": {"name": "ada"},
    }
    hub.fake.failing[f"/repos/{REPO}/compare/"] = 404
    assert (await hub.deliver(json.dumps(pushed).encode(), event="push")).status_code == 202
    await api.run_jobs()
    feature = {**pushed, "ref": "refs/heads/feature/x", "after": "d" * 40}
    assert (await hub.deliver(json.dumps(feature).encode(), event="push")).json()[
        "status"
    ] == "ignored"

    executions = await hub.executions()
    assert [git_of(e)["head_sha"] for e in executions] == [
        SHA1,
        SHA1,
        SHA2,
        SHA2,
        "c" * 40,
        "c" * 40,
    ]
    assert (
        "pull_request" not in git_of(executions[-1])
        and git_of(executions[-1])["head_ref"] == "main"
    )


# --- duplicates ---------------------------------------------------------------------------


async def test_a_redelivered_event_and_a_second_event_for_the_same_commit_start_nothing_twice(
    api: Api,
) -> None:
    hub = await Github.create(api)
    await hub.enable(status_enabled=True)
    delivery = "22222222-aaaa-bbbb-cccc-000000000002"

    first = await hub.deliver(hub.pr_body("opened"), delivery=delivery)
    again = await hub.deliver(hub.pr_body("opened"), delivery=delivery)
    assert (first.status_code, again.status_code, again.json()["status"]) == (202, 200, "duplicate")
    assert len(api.queue.pending) == 1
    await api.run_jobs()
    assert len(await hub.executions()) == 2 and len(await hub.rig.rows(GithubDelivery)) == 1

    # A different event for the same commit (reopened): accepted, but the executions already exist.
    reopened = await hub.deliver(hub.pr_body("reopened"))
    assert reopened.status_code == 202
    await api.run_jobs()
    executions = await hub.executions()
    assert len(executions) == 2 and len(hub.rig.engine.plans) == 2
    latest = (await hub.deliveries())[0]
    assert latest["action"] == "reopened" and latest["execution_ids"] == [
        str(e.id) for e in executions
    ]

    # The job itself delivered twice does nothing the second time.
    row = (await hub.rig.rows(GithubDelivery))[0]
    api.queue.pending.append(
        (
            "process_github_delivery",
            {"organization_id": hub.rig.owner.org_id, "delivery_id": str(row.id)},
        )
    )
    assert await api.run_jobs() == ["nothing_to_do"]
    assert len(await hub.executions()) == 2


async def test_an_event_overtaken_by_a_newer_commit_is_not_tested(api: Api) -> None:
    hub = await Github.create(api)
    await hub.enable()
    hub.fake.head_sha = SHA2  # by the time the job runs, the pull request has moved on

    await hub.deliver(hub.pr_body("opened", sha=SHA1))
    assert await api.run_jobs() == ["superseded"]

    assert await hub.executions() == []
    assert "newer commit" in (await hub.deliveries())[0]["reason"]


# --- test selection -----------------------------------------------------------------------


async def test_only_the_concerned_suite_runs_when_every_changed_file_is_covered_and_all_run_otherwise(
    api: Api,
) -> None:
    hub = await Github.create(api)
    billing, checkout = (s["id"] for s in hub.suites.values())
    await hub.enable(
        selection_mode="affected",
        path_rules=[
            {"pattern": "src/billing/**", "suite_ids": [billing]},
            {"pattern": "src/checkout/**", "suite_ids": [checkout]},
        ],
    )

    hub.fake.files = ["src/billing/invoice.py", "src/billing/tax.py"]
    await hub.deliver(hub.pr_body(sha=SHA1))
    await api.run_jobs()
    first = await hub.executions()
    assert [str(e.suite_id) for e in first] == [billing]
    assert git_of(first[0])["selection"]["scope"] == "affected"
    assert git_of(first[0])["selection"]["decided_by"] == "path rules"

    # One file no rule covers: everything runs.
    hub.fake.files, hub.fake.head_sha = ["src/billing/invoice.py", "Dockerfile"], SHA2
    await hub.deliver(hub.pr_body("synchronize", sha=SHA2))
    await api.run_jobs()
    second = (await hub.executions())[1:]
    assert sorted(str(e.suite_id) for e in second) == sorted([billing, checkout])
    assert git_of(second[0])["selection"]["scope"] == "full"
    assert git_of(second[0])["selection"]["unmatched_files"] == ["Dockerfile"]


async def test_when_github_cannot_say_what_changed_everything_runs_anyway(api: Api) -> None:
    hub = await Github.create(api)
    billing, checkout = (s["id"] for s in hub.suites.values())
    await hub.enable(
        selection_mode="affected",
        path_rules=[{"pattern": "src/billing/**", "suite_ids": [billing]}],
    )
    hub.fake.failing[f"/repos/{REPO}/pulls/7"] = 502

    await hub.deliver(hub.pr_body())
    assert await api.run_jobs() == ["started:2", "completed:passed", "completed:passed"]

    executions = await hub.executions()
    assert sorted(str(e.suite_id) for e in executions) == sorted([billing, checkout])
    assert "could not be read" in git_of(executions[0])["selection"]["reason"]
    assert (
        "could not be asked about the change (unavailable)" in (await hub.deliveries())[0]["reason"]
    )


async def test_github_saying_the_pull_request_is_in_another_repository_stops_everything(
    api: Api,
) -> None:
    hub = await Github.create(api)
    await hub.enable(status_enabled=True)
    hub.fake.pr_repo_id = 31337

    await hub.deliver(hub.pr_body())
    assert await api.run_jobs() == ["failed"]

    assert await hub.executions() == [] and hub.fake.statuses == []
    assert "belongs to another repository" in (await hub.deliveries())[0]["reason"]


# --- reporting back -----------------------------------------------------------------------


async def test_nothing_is_written_to_github_unless_it_is_switched_on(api: Api) -> None:
    hub = await Github.create(api)
    await hub.enable()
    hub.rig.engine.outcomes = {hub.cases["Billing"]["key"]: "failed"}

    await hub.deliver(hub.pr_body())
    await api.run_jobs()

    assert [e.verdict for e in await hub.executions()] == ["failed", "passed"]
    written = [r for r in hub.fake.requests if r.method != "GET"]
    assert written == [] and hub.fake.statuses == [] and hub.fake.comments == {}
    assert "published" not in git_of((await hub.executions())[0])


async def test_the_pull_request_gets_one_comment_that_is_updated_and_statuses_follow_the_verified_result(
    api: Api,
) -> None:
    hub = await Github.create(api)
    await hub.enable(status_enabled=True, comment_enabled=True)
    hub.rig.engine.outcomes = {
        hub.cases["Billing"]["key"]: "failed",
        hub.cases["Checkout"]["key"]: "flaky",
    }

    await hub.deliver(hub.pr_body())
    await api.run_jobs()

    final = {
        s["context"]: (s["state"], s["description"])
        for s in hub.fake.statuses
        if s["state"] != "pending"
    }
    assert final["QA Agent / Billing"] == ("failure", "1 of 1 test(s) failed.")
    # A pass on retry is not reported to GitHub as success.
    assert (
        final["QA Agent / Checkout"][0] == "failure"
        and "only after a retry" in final["QA Agent / Checkout"][1]
    )
    assert hub.fake.comment_calls == ["create", "update"] and len(hub.fake.comments) == 1
    body = next(iter(hub.fake.comments.values()))
    assert "QA Agent: Tests failed" in body and f"`{SHA1[:12]}`" in body
    assert (
        "| Billing | ❌ failed | 1 | 0 | 1 | 0 | 0 | 0 |" in body
        and "| Checkout | ⚠️ unverified | 1 | 0 | 0 | 0 | 0 | 1 |" in body
    )
    billing_key = hub.cases["Billing"]["key"].replace("-", "\\-")
    assert f"**{billing_key}**" in body and "Category (by rules): test assertion failure" in body
    assert "- Expected: `{'equals': '50'}`" in body and "- Found: `{'text': '51'}`" in body
    assert "Suggested next steps:" in body and "### Not counted as passed" in body
    executions = await hub.executions()
    assert (
        f"https://qa.example/projects/{hub.rig.ws.project_id}/executions/{executions[0].id}" in body
    )
    assert "evidence-downloads" not in body and "/content" not in body and TOKEN not in body
    assert git_of(executions[1])["published"] == {"comment": "ok", "status": "ok"}
    assert git_of(executions[1])["comment_id"] == 9000

    # A new commit: the same comment is rewritten for it.
    hub.fake.head_sha, hub.rig.engine.outcomes = SHA2, {}
    await hub.deliver(hub.pr_body("synchronize", sha=SHA2))
    await api.run_jobs()
    assert len(hub.fake.comments) == 1 and hub.fake.comment_calls[-1] == "update"
    updated = hub.fake.comments[9000]
    assert (
        "QA Agent: All tests passed" in updated
        and f"`{SHA2[:12]}`" in updated
        and "Failed tests" not in updated
    )
    assert [s["state"] for s in hub.fake.statuses if s["sha"] == SHA2][-2:] == [
        "success",
        "success",
    ]


async def test_github_failing_while_results_are_written_back_changes_no_result(api: Api) -> None:
    hub = await Github.create(api)
    await hub.enable(status_enabled=True, comment_enabled=True)
    hub.rig.engine.outcomes = {hub.cases["Billing"]["key"]: "failed"}
    hub.fake.failing[f"/repos/{REPO}/statuses/"] = 403
    hub.fake.failing[f"/repos/{REPO}/issues/"] = 500

    await hub.deliver(hub.pr_body())
    results = await api.run_jobs()

    assert results == ["started:2", "completed:failed", "completed:passed"]
    executions = await hub.executions()
    assert [(e.status, e.verdict) for e in executions] == [
        ("completed", "failed"),
        ("completed", "passed"),
    ]
    assert git_of(executions[0])["published"] == {
        "comment": "unavailable",
        "status": "forbidden",
    }
    assert "comment_id" not in git_of(executions[0])
    report = await hub.rig.owner.get(f"{hub.rig.base}/executions/{executions[0].id}/report")
    assert report.json()["totals"]["failed"] == 1


async def test_a_suite_that_can_no_longer_run_is_reported_and_the_others_still_run(
    api: Api,
) -> None:
    hub = await Github.create(api, requires_approval=True)
    await hub.enable()
    # On an approval-required environment nothing automated may change data: both suites do.
    await hub.deliver(hub.pr_body(sha=SHA1))
    assert await api.run_jobs() == ["started:0"]
    delivery = (await hub.deliveries())[0]
    assert delivery["outcome"] == "failed" and "cannot give that approval" in delivery["reason"]
    assert await hub.executions() == []

    looking = await hub.rig.case("530", LOOK_ONLY)
    suite = (
        await hub.rig.owner.post(
            f"{hub.rig.base}/test-suites",
            json={
                "name": "Read only",
                "application_id": hub.rig.ws.application_id,
                "case_ids": [looking["id"]],
            },
        )
    ).json()
    await hub.rig.owner.post(f"{hub.rig.base}/test-suites/{suite['id']}/approve")
    await hub.enable(suite_ids=[hub.suites["Billing"]["id"], suite["id"]])
    hub.fake.head_sha = SHA2
    await hub.deliver(hub.pr_body("synchronize", sha=SHA2))
    await api.run_jobs()

    executions = await hub.executions()
    assert [str(e.suite_id) for e in executions] == [suite["id"]] and executions[0].config[
        "state_changes_approved"
    ] is False
    assert "Billing:" in (await hub.deliveries())[0]["reason"]


async def test_unlinking_stops_deliveries_and_removing_the_connection_removes_its_links(
    api: Api,
) -> None:
    hub = await Github.create(api)
    await hub.enable()
    assert (
        await hub.rig.owner.delete(f"{hub.org_url}/github/connections/{hub.connection['id']}")
    ).status_code == 204

    assert await hub.rig.rows(GithubRepository) == [] and await hub.rig.rows(Integration) == []
    assert (await hub.deliver(hub.pr_body())).status_code == 401
    assert (await hub.rig.owner.get(hub.url)).status_code == 404


# --- CI: API keys -------------------------------------------------------------------------


class Ci:
    def __init__(self, api: Api, rig: Rig) -> None:
        self.api, self.rig = api, rig
        self.keys_url = f"/api/v1/organizations/{rig.owner.org_id}/api-keys"

    async def key(
        self, scopes: Any = ("executions:write", "executions:read"), **more: Any
    ) -> dict[str, Any]:
        response = await self.rig.owner.post(
            self.keys_url, json={"name": "pipeline", "scopes": list(scopes), **more}
        )
        assert response.status_code == 201, response.text
        created: dict[str, Any] = response.json()
        return created

    async def call(self, method: str, path: str, key: str | None, **kwargs: Any) -> httpx.Response:
        headers = {
            **kwargs.pop("headers", {}),
            **({"Authorization": f"Bearer {key}"} if key else {}),
        }
        return await self.api.client().request(
            method, f"/api/v1/ci{path}", headers=headers, **kwargs
        )

    def body(self, *cases: dict[str, Any], **more: Any) -> dict[str, Any]:
        return {"project_id": self.rig.ws.project_id, "environment_id": self.rig.ws.environment_id,
                "case_ids": [c["id"] for c in cases], **more}  # fmt: skip


async def test_an_api_key_is_shown_once_stored_as_a_hash_and_managed_by_admins_only(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    ci = Ci(api, rig)
    developer = await api.invite_and_join(rig.owner, "dev@acme.io", "developer")
    outsider = await api.register("owner@other.io", organization="Other")

    created = await ci.key()

    key = created["key"]
    assert (
        key.startswith(f"qak_{uuid.UUID(rig.owner.org_id).hex}_")
        and created["key_prefix"] == key[:43]
    )
    assert created["scopes"] == ["executions:read", "executions:write"] and created["expires_at"]
    row = (await rig.rows(ApiKey))[0]
    assert (
        key not in (row.key_hash, row.key_prefix)
        and len(row.key_hash) == 64
        and row.key_prefix in key
    )
    listed = await rig.owner.get(ci.keys_url)
    assert "key" not in listed.json()[0] and key not in listed.text
    assert key not in json.dumps([r.changes for r in await rig.rows(AuditLog)])

    for account in (developer,):
        assert (await account.get(ci.keys_url)).status_code == 403
        assert (
            await account.post(ci.keys_url, json={"name": "x", "scopes": ["executions:read"]})
        ).status_code == 403
        assert (await account.delete(f"{ci.keys_url}/{created['id']}")).status_code == 403
    assert (await outsider.get(ci.keys_url)).status_code == 404
    assert (await outsider.delete(f"{ci.keys_url}/{created['id']}")).status_code == 404
    for bad in ({"name": "x", "scopes": []}, {"name": "x", "scopes": ["admin"]}, {"name": "x", "scopes": ["executions:read"], "expires_in_days": 5000},
                {"name": "x", "scopes": ["executions:read"], "project_id": str(uuid.uuid4())}):  # fmt: skip
        assert (await rig.owner.post(ci.keys_url, json=bad)).status_code in (404, 422), bad


async def test_requests_without_a_working_key_are_refused_the_same_way(api: Api) -> None:
    rig = await Rig(api).setup()
    ci = Ci(api, rig)
    case = await rig.case()
    good, revoked, expired = (await ci.key())["key"], await ci.key(), await ci.key()
    await rig.owner.delete(f"{ci.keys_url}/{revoked['id']}")
    async with api.app.state.session_factory() as session, session.begin():
        await set_current_org(session, uuid.UUID(rig.owner.org_id))
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == uuid.UUID(expired["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    other_org = "qak_" + uuid.uuid4().hex + "_" + good.rsplit("_", 1)[1]

    for key in (
        None,
        "",
        "nonsense",
        revoked["key"],
        expired["key"],
        good[:-3] + "AAA",
        other_org,
        rig.owner.token,
        good.upper(),
    ):
        refused = await ci.call("POST", "/executions", key, json=ci.body(case))
        assert (refused.status_code, refused.json()["code"]) in (
            (401, "invalid_api_key"),
            (401, "unauthenticated"),
        ), key
        assert (await ci.call("GET", f"/executions/{uuid.uuid4()}", key)).status_code == 401
    # And a key is not a session: it opens nothing but the CI interface.
    as_session = await api.client().get(
        f"{rig.base}/executions", headers={"Authorization": f"Bearer {good}"}
    )
    assert as_session.status_code == 401
    assert await rig.rows(TestExecution) == []


async def test_a_key_does_only_what_its_scopes_and_project_allow(api: Api) -> None:
    rig = await Rig(api).setup()
    ci = Ci(api, rig)
    case = await rig.case()
    other = await rig.owner.create_project("Other project")
    read_only = (await ci.key(["executions:read"]))["key"]
    write_only = (await ci.key(["executions:write"]))["key"]
    elsewhere = (await ci.key(project_id=other["id"]))["key"]
    here = (await ci.key(project_id=rig.ws.project_id))["key"]

    refused = await ci.call("POST", "/executions", read_only, json=ci.body(case))
    assert (refused.status_code, refused.json()["code"]) == (403, "insufficient_scope")
    assert (await ci.call("POST", "/executions", elsewhere, json=ci.body(case))).status_code == 404
    started = await ci.call("POST", "/executions", write_only, json=ci.body(case))
    assert started.status_code == 202
    execution_id = started.json()["id"]
    assert (await ci.call("GET", f"/executions/{execution_id}", write_only)).status_code == 403
    assert (await ci.call("GET", f"/executions/{execution_id}", elsewhere)).status_code == 404
    assert (await ci.call("GET", f"/executions/{execution_id}/junit", elsewhere)).status_code == 404
    assert (await ci.call("GET", f"/executions/{execution_id}", here)).status_code == 200
    # A key of another organization sees nothing of this one.
    outsider = await api.register("owner@other.io", organization="Other")
    foreign = (
        await outsider.post(
            f"/api/v1/organizations/{outsider.org_id}/api-keys",
            json={"name": "x", "scopes": ["executions:read", "executions:write"]},
        )
    ).json()["key"]
    assert (await ci.call("GET", f"/executions/{execution_id}", foreign)).status_code == 404
    assert (await ci.call("POST", "/executions", foreign, json=ci.body(case))).status_code == 404


# --- CI: running and reading --------------------------------------------------------------


async def test_a_pipeline_starts_approved_tests_and_reads_the_verified_outcome(api: Api) -> None:
    rig = await Rig(api, public_base_url="https://qa.example").setup()
    ci = Ci(api, rig)
    good, bad = await rig.case("500"), await rig.case("510")
    rig.engine.outcomes = {bad["key"]: "failed"}
    key = await ci.key()
    git = {"repository": "acme/shop", "sha": SHA1, "ref": "refs/pull/7/merge", "pull_request": 7,
           "run_url": "https://github.com/acme/shop/actions/runs/1"}  # fmt: skip

    started = await ci.call("POST", "/executions", key["key"], json=ci.body(good, bad, git=git))

    assert started.status_code == 202, started.text
    running = started.json()
    assert (running["finished"], running["outcome"], running["passed"], running["exit_code"]) == (
        False,
        "running",
        False,
        None,
    )
    assert (
        running["report_url"]
        == f"https://qa.example/projects/{rig.ws.project_id}/executions/{running['id']}"
    )
    await api.run_jobs()
    done = (await ci.call("GET", f"/executions/{running['id']}", key["key"])).json()
    assert (done["finished"], done["outcome"], done["passed"], done["exit_code"], done["verdict"]) == (
        True, "failed", False, 1, "failed",
    )  # fmt: skip
    assert done["summary"] == "1 of 2 test(s) failed." and done["totals"]["passed"] == 1
    assert done["failures"] == [{"case_key": bad["key"], "title": "Loyalty points for a bill of 510", "status": "failed",
                                 "failure_category": "test_assertion_failure", "summary": f"{bad['key']} did not pass"}]  # fmt: skip

    row = (await rig.rows(TestExecution))[0]
    assert (row.trigger, row.triggered_by, str(row.api_key_id)) == ("ci", None, key["id"])
    assert git_of(row) == {"source": "ci", **git}
    audit = next(r for r in await rig.rows(AuditLog) if r.action == "execution.start")
    assert (audit.actor_type, str(audit.actor_id), audit.changes["trigger"]) == (
        "api_key",
        key["id"],
        "ci",
    )
    assert (await rig.rows(ApiKey))[0].last_used_at is not None

    junit = await ci.call("GET", f"/executions/{running['id']}/junit", key["key"])
    assert junit.status_code == 200 and junit.headers["content-type"].startswith("application/xml")
    suite = ET.fromstring(junit.content)  # noqa: S314 - our own output
    assert (suite.attrib["tests"], suite.attrib["failures"], suite.attrib["errors"]) == (
        "2",
        "1",
        "0",
    )
    assert suite.findall("testcase")[1].find("failure") is not None
    report = (await ci.call("GET", f"/executions/{running['id']}/report", key["key"])).json()
    assert report["totals"]["failed"] == 1 and report["execution"]["id"] == running["id"]
    # A person sees the same execution in the web application.
    assert (await rig.get(running["id"]))["verdict"] == "failed"


@pytest.mark.parametrize(
    ("engine_outcome", "outcome", "exit_code"),
    [
        ("passed", "passed", 0),
        ("failed", "failed", 1),
        ("error", "unverified", 2),
        ("flaky", "unverified", 2),
        ("healed", "unverified", 2),
    ],
)
async def test_the_ci_outcome_follows_the_verdict_and_only_a_verified_pass_is_success(
    api: Api, engine_outcome: str, outcome: str, exit_code: int
) -> None:
    rig = await Rig(api).setup()
    ci = Ci(api, rig)
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: engine_outcome}
    key = (await ci.key())["key"]

    started = await ci.call(
        "POST", "/executions", key, json=ci.body(case, config={"healing": {"mode": "recover"}})
    )
    await api.run_jobs()
    done = (await ci.call("GET", f"/executions/{started.json()['id']}", key)).json()

    assert (done["outcome"], done["exit_code"], done["passed"]) == (
        outcome,
        exit_code,
        outcome == "passed",
    )


async def test_a_repeated_request_with_the_same_idempotency_key_gets_the_same_execution(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    ci = Ci(api, rig)
    case = await rig.case()
    key = (await ci.key())["key"]
    headers = {"Idempotency-Key": "gha-12345-1-suite"}

    first = await ci.call("POST", "/executions", key, json=ci.body(case), headers=headers)
    second = await ci.call("POST", "/executions", key, json=ci.body(case), headers=headers)
    other = await ci.call(
        "POST",
        "/executions",
        key,
        json=ci.body(case),
        headers={"Idempotency-Key": "gha-12345-2-suite"},
    )

    assert (first.status_code, second.status_code, other.status_code) == (202, 200, 202)
    assert first.json()["id"] == second.json()["id"] != other.json()["id"]
    assert len(await rig.rows(TestExecution)) == 2 and len(api.queue.pending) == 2
    for bad in ("short", "has space in it", "x" * 300, "new\nline-000000"):
        refused = await ci.call(
            "POST", "/executions", key, json=ci.body(case), headers={"Idempotency-Key": bad}
        )
        assert refused.status_code in (400, 422), bad


async def test_ci_can_only_run_approved_work_and_cannot_approve_changes_to_a_protected_environment(
    api: Api,
) -> None:
    rig = await Rig(api).setup(requires_approval=True)
    ci = Ci(api, rig)
    changing, looking = await rig.case("500"), await rig.case("510", LOOK_ONLY)
    draft = await rig.case("520", approve=False)
    key = (await ci.key())["key"]

    refused = await ci.call("POST", "/executions", key, json=ci.body(changing))
    assert (refused.status_code, refused.json()["code"]) == (403, "approval_required")
    unapproved = await ci.call("POST", "/executions", key, json=ci.body(draft))
    assert (unapproved.status_code, unapproved.json()["code"]) == (409, "case_not_approved")
    assert (await ci.call("POST", "/executions", key, json=ci.body())).status_code == 422
    assert (
        await ci.call(
            "POST",
            "/executions",
            key,
            json={**ci.body(looking), "environment_id": str(uuid.uuid4())},
        )
    ).status_code == 404
    assert (
        await ci.call(
            "POST", "/executions", key, json={**ci.body(looking), "git": {"sha": "not a sha"}}
        )
    ).status_code == 422
    assert (
        await ci.call(
            "POST",
            "/executions",
            key,
            json={**ci.body(looking), "git": {"run_url": "javascript:alert(1)"}},
        )
    ).status_code == 422

    allowed = await ci.call("POST", "/executions", key, json=ci.body(looking))
    assert allowed.status_code == 202
    await api.run_jobs()
    assert set(rig.engine.plans[0].allowed_actions) == {"navigate", "read", "api_read"}
    assert (await rig.rows(TestExecution))[0].config["state_changes_approved"] is False


async def test_a_key_that_asks_too_often_is_slowed_down(api: Api) -> None:
    rig = await Rig(api, ci_rate_limit=3).setup()
    ci = Ci(api, rig)
    key = (await ci.key())["key"]

    answers = [
        (await ci.call("GET", f"/executions/{uuid.uuid4()}", key)).status_code for _ in range(5)
    ]

    assert answers == [404, 404, 404, 429, 429]
    limited = await ci.call("GET", f"/executions/{uuid.uuid4()}", key)
    assert int(limited.headers["retry-after"]) >= 1
