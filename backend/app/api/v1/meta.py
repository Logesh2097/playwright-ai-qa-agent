"""Service metadata."""

from fastapi import APIRouter

from app.api.deps import SettingsDep
from app.core.security.permissions import ROLE_PERMISSIONS, Role
from app.schemas.system import MetaResponse

router = APIRouter(tags=["meta"])


@router.get("/meta", summary="Service name, version and environment")
async def get_meta(settings: SettingsDep) -> MetaResponse:
    return MetaResponse(
        name=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        api_version="v1",
    )


@router.get("/meta/roles", summary="The roles and what each may do")
async def get_roles() -> list[dict[str, object]]:
    """The same table the server enforces, so a page never keeps its own copy of it."""
    return [
        {"role": role.value, "permissions": sorted(p.value for p in ROLE_PERMISSIONS[role])}
        for role in Role
    ]
