"""Version 1 of the public API. Feature routers are included here as phases land."""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    browser,
    ci,
    evidence,
    executions,
    github,
    instructions,
    meta,
    organizations,
    projects,
    quality,
    reports,
    test_cases,
    workflows,
)

api_v1_router = APIRouter()
api_v1_router.include_router(meta.router)
api_v1_router.include_router(auth.router)
api_v1_router.include_router(organizations.router)
api_v1_router.include_router(projects.router)
api_v1_router.include_router(instructions.router)
api_v1_router.include_router(test_cases.router)
api_v1_router.include_router(browser.router)
api_v1_router.include_router(executions.router)
api_v1_router.include_router(ci.router)
api_v1_router.include_router(evidence.router)
api_v1_router.include_router(github.router)
api_v1_router.include_router(quality.router)
api_v1_router.include_router(reports.router)
api_v1_router.include_router(reports.org_router)
api_v1_router.include_router(workflows.router)
