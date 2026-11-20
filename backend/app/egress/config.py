"""Settings of the egress proxy. It needs no database, Redis or application secrets."""

import ipaddress
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class EgressSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    log_level: str = "INFO"
    egress_admin_token: SecretStr = Field(min_length=24)
    egress_proxy_host: str = "0.0.0.0"  # noqa: S104 - listens on the internal runner network
    egress_proxy_port: int = Field(default=3128, ge=1, le=65535)
    egress_max_sessions: int = Field(default=200, ge=1)
    egress_connect_timeout_s: float = Field(default=10.0, gt=0)
    egress_idle_timeout_s: float = Field(default=300.0, gt=0)
    # Networks no session may ever reach, whatever its environment allows: the
    # platform's own internal networks. Loopback is always refused as well.
    egress_deny_cidrs: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("egress_deny_cidrs", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("egress_deny_cidrs")
    @classmethod
    def _valid_networks(cls, value: list[str]) -> list[str]:
        for item in value:
            ipaddress.ip_network(item, strict=False)
        return value

    @property
    def deny_networks(self) -> tuple[Network, ...]:
        return tuple(ipaddress.ip_network(item, strict=False) for item in self.egress_deny_cidrs)
