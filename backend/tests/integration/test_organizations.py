"""Organizations, members, roles and the audit log."""

import pytest

from tests.conftest import Account, Api

pytestmark = pytest.mark.integration

ORGS = "/api/v1/organizations"


async def _members(account: Account) -> list[dict[str, object]]:
    response = await account.get(f"{ORGS}/{account.org_id}/members")
    assert response.status_code == 200, response.text
    members: list[dict[str, object]] = response.json()
    return members


async def _member_id(viewer: Account, email: str) -> str:
    return str(next(m["id"] for m in await _members(viewer) if m["email"] == email))


# --- organizations -----------------------------------------------------------------


async def test_user_can_create_and_list_several_organizations(api: Api) -> None:
    owner = await api.register("asha@acme.io", organization="Acme")
    created = await owner.post(ORGS, json={"name": "Second Org"})
    assert created.status_code == 201
    assert created.json()["role"] == "owner"

    listed = await owner.get(ORGS)
    assert [org["name"] for org in listed.json()] == ["Acme", "Second Org"]


async def test_organization_slugs_are_unique_across_tenants(api: Api) -> None:
    first = await api.register("a@acme.io", organization="Acme")
    second = await api.register("b@other.io", organization="Acme")
    slug_a = first.session["memberships"][0]["organization_slug"]
    slug_b = second.session["memberships"][0]["organization_slug"]
    assert slug_a == "acme"
    assert slug_b.startswith("acme-") and slug_b != slug_a


async def test_organization_requests_require_authentication(api: Api) -> None:
    owner = await api.register("asha@acme.io")
    client = api.client()
    assert (await client.get(ORGS)).status_code == 401
    assert (await client.get(f"{ORGS}/{owner.org_id}")).status_code == 401
    assert (await client.get(f"{ORGS}/{owner.org_id}/members")).status_code == 401


async def test_owner_can_rename_and_delete(api: Api) -> None:
    owner = await api.register("asha@acme.io")
    renamed = await owner.patch(f"{ORGS}/{owner.org_id}", json={"name": "Acme Global"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Acme Global"

    assert (await owner.delete(f"{ORGS}/{owner.org_id}")).status_code == 204
    assert (await owner.get(f"{ORGS}/{owner.org_id}")).status_code == 404
    assert (await owner.get(ORGS)).json() == []


# --- role permissions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "rename", "invite", "audit", "delete"),
    [
        ("viewer", 403, 403, 403, 403),
        ("developer", 403, 403, 403, 403),
        ("qa_engineer", 403, 403, 403, 403),
        ("admin", 200, 201, 200, 403),
    ],
)
async def test_role_permissions_are_enforced_by_the_backend(
    api: Api, role: str, rename: int, invite: int, audit: int, delete: int
) -> None:
    owner = await api.register("owner@acme.io")
    member = await api.invite_and_join(owner, f"{role}@acme.io", role)
    base = f"{ORGS}/{owner.org_id}"

    # Reading is allowed for every role.
    assert (await member.get(base)).status_code == 200
    assert (await member.get(f"{base}/members")).status_code == 200

    assert (await member.patch(base, json={"name": "Renamed"})).status_code == rename
    invited = await member.post(
        f"{base}/invitations", json={"email": "new@acme.io", "role": "viewer"}
    )
    assert invited.status_code == invite
    assert (await member.get(f"{base}/audit-logs")).status_code == audit
    assert (await member.delete(base)).status_code == delete
    if delete == 403:
        assert (await owner.get(base)).status_code == 200


async def test_forbidden_responses_use_the_error_envelope(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    viewer = await api.invite_and_join(owner, "viewer@acme.io", "viewer")
    response = await viewer.patch(f"{ORGS}/{owner.org_id}", json={"name": "Nope"})
    assert response.status_code == 403
    assert response.json()["code"] == "insufficient_role"
    assert response.headers["content-type"] == "application/problem+json"


async def test_role_change_takes_effect_on_the_next_request(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    member = await api.invite_and_join(owner, "dev@acme.io", "developer")
    base = f"{ORGS}/{owner.org_id}"
    assert (await member.get(f"{base}/audit-logs")).status_code == 403

    member_id = await _member_id(owner, "dev@acme.io")
    promoted = await owner.patch(f"{base}/members/{member_id}", json={"role": "admin"})
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"

    # Same access token as before: roles are read from the database, not the token.
    assert (await member.get(f"{base}/audit-logs")).status_code == 200


async def test_removed_member_loses_access_immediately(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    member = await api.invite_and_join(owner, "dev@acme.io", "developer")
    base = f"{ORGS}/{owner.org_id}"
    member_id = await _member_id(owner, "dev@acme.io")

    assert (await owner.delete(f"{base}/members/{member_id}")).status_code == 204

    assert (await member.get(base)).status_code == 404
    assert (await member.get(f"{base}/members")).status_code == 404
    assert (await member.get(ORGS)).json() == []


async def test_admin_cannot_escalate_or_touch_owners(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    admin = await api.invite_and_join(owner, "admin@acme.io", "admin")
    base = f"{ORGS}/{owner.org_id}"
    owner_member = await _member_id(owner, "owner@acme.io")
    admin_member = await _member_id(owner, "admin@acme.io")

    invite_owner = await admin.post(
        f"{base}/invitations", json={"email": "x@acme.io", "role": "owner"}
    )
    promote_self = await admin.patch(f"{base}/members/{admin_member}", json={"role": "owner"})
    demote_owner = await admin.patch(f"{base}/members/{owner_member}", json={"role": "viewer"})
    remove_owner = await admin.delete(f"{base}/members/{owner_member}")

    assert [r.status_code for r in (invite_owner, promote_self, demote_owner, remove_owner)] == [
        403,
        403,
        403,
        403,
    ]
    assert {m["email"]: m["role"] for m in await _members(owner)} == {
        "owner@acme.io": "owner",
        "admin@acme.io": "admin",
    }


async def test_an_organization_always_keeps_one_owner(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    base = f"{ORGS}/{owner.org_id}"
    owner_member = await _member_id(owner, "owner@acme.io")

    demote = await owner.patch(f"{base}/members/{owner_member}", json={"role": "admin"})
    leave = await owner.delete(f"{base}/members/{owner_member}")
    assert (demote.status_code, demote.json()["code"]) == (409, "last_owner")
    assert (leave.status_code, leave.json()["code"]) == (409, "last_owner")

    # With a second owner, the first may step down.
    await api.invite_and_join(owner, "second@acme.io", "owner")
    assert (
        await owner.patch(f"{base}/members/{owner_member}", json={"role": "viewer"})
    ).status_code == 200


async def test_any_member_can_leave(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    viewer = await api.invite_and_join(owner, "viewer@acme.io", "viewer")
    base = f"{ORGS}/{owner.org_id}"
    own_id = await _member_id(viewer, "viewer@acme.io")
    other_id = await _member_id(viewer, "owner@acme.io")

    assert (await viewer.delete(f"{base}/members/{other_id}")).status_code == 403
    assert (await viewer.delete(f"{base}/members/{own_id}")).status_code == 204
    assert (await viewer.get(base)).status_code == 404


# --- invitations --------------------------------------------------------------------


async def test_invitation_flow_for_a_new_user(api: Api) -> None:
    owner = await api.register("owner@acme.io", organization="Acme")
    invite = await owner.post(
        f"{ORGS}/{owner.org_id}/invitations", json={"email": "dev@acme.io", "role": "developer"}
    )
    assert invite.status_code == 201
    assert invite.json()["status"] == "invited"
    token = api.outbox.token_for("dev@acme.io")
    assert token not in invite.text, "the token travels by email only"

    joined = await api.register("dev@acme.io", organization=None, invite_token=token)
    assert joined.session["memberships"] == [
        {
            "organization_id": owner.org_id,
            "organization_name": "Acme",
            "organization_slug": "acme",
            "role": "developer",
        }
    ]
    statuses = {m["email"]: m["status"] for m in await _members(owner)}
    assert statuses == {"owner@acme.io": "active", "dev@acme.io": "active"}


async def test_existing_user_accepts_an_invitation(api: Api) -> None:
    owner = await api.register("owner@acme.io", organization="Acme")
    other = await api.register("dev@other.io", organization="Other")
    await owner.post(
        f"{ORGS}/{owner.org_id}/invitations", json={"email": "dev@other.io", "role": "qa_engineer"}
    )
    token = api.outbox.token_for("dev@other.io")

    accepted = await other.post("/api/v1/auth/invitations/accept", json={"token": token})
    assert accepted.status_code == 204
    roles = {org["name"]: org["role"] for org in (await other.get(ORGS)).json()}
    assert roles == {"Acme": "qa_engineer", "Other": "owner"}

    # Single use.
    again = await other.post("/api/v1/auth/invitations/accept", json={"token": token})
    assert again.status_code == 400


async def test_invitation_is_bound_to_the_invited_email(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    await owner.post(
        f"{ORGS}/{owner.org_id}/invitations", json={"email": "dev@acme.io", "role": "admin"}
    )
    token = api.outbox.token_for("dev@acme.io")

    intruder = await api.register("intruder@evil.io", organization="Evil")
    stolen = await intruder.post("/api/v1/auth/invitations/accept", json={"token": token})
    assert (stolen.status_code, stolen.json()["code"]) == (400, "invalid_invitation")

    register = await api.client().post(
        "/api/v1/auth/register",
        json={
            "email": "second@evil.io",
            "password": "correct horse battery staple",
            "full_name": "X",
            "invite_token": token,
        },
    )
    assert register.status_code == 400
    assert (await intruder.get(f"{ORGS}/{owner.org_id}")).status_code == 404


async def test_duplicate_invitations_and_members_are_rejected(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    url = f"{ORGS}/{owner.org_id}/invitations"
    assert (await owner.post(url, json={"email": "dev@acme.io"})).status_code == 201

    again = await owner.post(url, json={"email": "DEV@acme.io"})
    assert (again.status_code, again.json()["code"]) == (409, "already_invited")

    existing = await owner.post(url, json={"email": "owner@acme.io"})
    assert (existing.status_code, existing.json()["code"]) == (409, "already_member")


async def test_invitation_can_be_cancelled(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    base = f"{ORGS}/{owner.org_id}"
    invite = await owner.post(f"{base}/invitations", json={"email": "dev@acme.io"})
    token = api.outbox.token_for("dev@acme.io")

    assert (await owner.delete(f"{base}/members/{invite.json()['id']}")).status_code == 204

    register = await api.client().post(
        "/api/v1/auth/register",
        json={
            "email": "dev@acme.io",
            "password": "correct horse battery staple",
            "full_name": "Dev",
            "invite_token": token,
        },
    )
    assert register.status_code == 400


# --- audit log -----------------------------------------------------------------------


async def test_audit_log_records_actions_and_paginates(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    base = f"{ORGS}/{owner.org_id}"
    await owner.patch(base, json={"name": "Acme Global"})
    await owner.post(f"{base}/invitations", json={"email": "dev@acme.io", "role": "developer"})

    first = await owner.get(f"{base}/audit-logs", params={"limit": 2})
    assert first.status_code == 200
    page = first.json()
    assert [item["action"] for item in page["items"]] == ["member.invite", "organization.update"]
    assert page["next_cursor"] is not None
    assert page["items"][0]["actor_id"] == owner.user_id
    assert page["items"][0]["request_id"]

    rest = await owner.get(f"{base}/audit-logs", params={"limit": 2, "cursor": page["next_cursor"]})
    actions = [item["action"] for item in rest.json()["items"]]
    assert actions == ["user.register", "organization.create"]
    assert rest.json()["next_cursor"] is None

    filtered = await owner.get(f"{base}/audit-logs", params={"action": "member.invite"})
    assert len(filtered.json()["items"]) == 1
    assert filtered.json()["items"][0]["changes"] == {"email": "dev@acme.io", "role": "developer"}


async def test_audit_log_query_parameters_are_validated(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    base = f"{ORGS}/{owner.org_id}/audit-logs"
    assert (await owner.get(base, params={"limit": 0})).status_code == 422
    assert (await owner.get(base, params={"limit": 1000})).status_code == 422
    assert (await owner.get(base, params={"cursor": "not-a-uuid"})).status_code == 422
