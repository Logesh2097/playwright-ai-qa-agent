"""Builds the session manager from settings. None when browsers are not configured."""

from app.browser.manager import BrowserSessionManager
from app.browser.services import EgressClient, LaunchOptions, RunnerClient, Viewport
from app.core.config import Settings
from app.engine.client import EngineClient


def build_egress_client(settings: Settings) -> EgressClient | None:
    if not (
        settings.egress_admin_url and settings.egress_admin_token and settings.egress_proxy_url
    ):
        return None
    return EgressClient(
        settings.egress_admin_url,
        settings.egress_admin_token.get_secret_value(),
        settings.egress_proxy_url,
    )


def build_session_manager(settings: Settings) -> BrowserSessionManager | None:
    egress = build_egress_client(settings)
    if egress is None or not (settings.runner_url and settings.runner_token):
        return None
    return BrowserSessionManager(
        RunnerClient(settings.runner_url, settings.runner_token.get_secret_value()),
        egress,
        client_version=settings.app_version,
    )


def build_engine_client(settings: Settings) -> EngineClient | None:
    if not (settings.engine_url and settings.engine_token):
        return None
    return EngineClient(settings.engine_url, settings.engine_token.get_secret_value())


def launch_options(settings: Settings, viewport: dict[str, object] | None) -> LaunchOptions:
    """Installation defaults, with the environment's viewport when it has a usable one."""
    size = Viewport()
    if viewport:
        width, height = viewport.get("width"), viewport.get("height")
        if (
            isinstance(width, int)
            and isinstance(height, int)
            and 320 <= width <= 3840
            and 240 <= height <= 2160
        ):
            size = Viewport(width, height)
    return LaunchOptions(
        browser=settings.browser_name,
        viewport=size,
        action_timeout_ms=settings.browser_action_timeout_ms,
        navigation_timeout_ms=settings.browser_navigation_timeout_ms,
        ttl_s=settings.browser_session_ttl_s,
    )
