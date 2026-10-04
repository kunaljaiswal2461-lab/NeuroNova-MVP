"""Destination and TLS policy for public PostgreSQL onboarding connections."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Awaitable, Callable


class ConnectionPolicyError(Exception):
    """Connection settings are outside the supported onboarding boundary."""


Resolver = Callable[[str, int], Awaitable[list[str]]]
_HOST_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


async def _system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host,
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return sorted({info[4][0] for info in infos})


def _normalise_dns_hostname(host: str, *, allow_single_label_for_tests: bool = False) -> str:
    value = host.strip().rstrip(".")
    if not value or len(value) > 253 or any(char in value for char in "/\\@?:#[]%"):
        raise ConnectionPolicyError()
    # Initial support is DNS-only. This improves certificate hostname
    # verification and avoids accepting URL/hostaddr-style inputs.
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ConnectionPolicyError()
    try:
        ascii_host = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise ConnectionPolicyError() from None
    labels = ascii_host.split(".")
    if (
        (len(labels) < 2 and not allow_single_label_for_tests)
        or any(not _HOST_LABEL.fullmatch(label) for label in labels)
    ):
        raise ConnectionPolicyError()
    return ascii_host.lower()


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
            not address.is_global,
        )
    )


@dataclass(frozen=True)
class PostgresConnectionPolicy:
    """Policy injected into the adapter; production never switches by APP_ENV.

    ``asyncpg`` resolves the supplied hostname again while opening its socket,
    so this preflight DNS check cannot itself eliminate DNS rebinding. A
    production egress firewall/proxy remains required before enabling the
    feature; see the source-onboarding documentation.
    """

    resolver: Resolver = _system_resolver
    allow_non_public_for_disposable_tests: bool = False
    allow_single_label_for_disposable_tests: bool = False
    required_port: int = 5432

    @classmethod
    def for_disposable_test(cls, resolver: Resolver) -> "PostgresConnectionPolicy":
        """Explicit test injection; never selected from application settings."""
        return cls(
            resolver=resolver,
            allow_non_public_for_disposable_tests=True,
            allow_single_label_for_disposable_tests=True,
        )

    async def validate(self, *, host: str, port: int) -> str:
        if port != self.required_port:
            raise ConnectionPolicyError()
        normalized_host = _normalise_dns_hostname(
            host,
            allow_single_label_for_tests=self.allow_single_label_for_disposable_tests,
        )
        try:
            addresses = await self.resolver(normalized_host, port)
        except (OSError, socket.gaierror):
            raise ConnectionPolicyError() from None
        if not addresses:
            raise ConnectionPolicyError()
        if not self.allow_non_public_for_disposable_tests and not all(
            _is_public_address(address) for address in addresses
        ):
            raise ConnectionPolicyError()
        return normalized_host

    @staticmethod
    def build_ssl_context(custom_ca_pem: str | None) -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        if custom_ca_pem:
            try:
                context.load_verify_locations(cadata=custom_ca_pem)
            except (ssl.SSLError, ValueError):
                raise ConnectionPolicyError() from None
        return context
