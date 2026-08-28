from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

import psutil
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class NetworkConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class InterfaceAddress:
    name: str
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    network: ipaddress.IPv4Network | ipaddress.IPv6Network
    is_up: bool = True


@dataclass(frozen=True)
class AdminNetwork:
    interface: str
    bind_address: str
    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]


VPN_NAME = re.compile(r"^(?:awg|amn|amnezia|tun|tap|wg)[A-Za-z0-9_.-]*$", re.IGNORECASE)
PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_private_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in network for network in PRIVATE_NETWORKS)


def _is_private_network(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    return any(
        network.version == allowed.version and network.subnet_of(allowed)
        for allowed in PRIVATE_NETWORKS
    )


def system_interfaces() -> list[InterfaceAddress]:
    stats = psutil.net_if_stats()
    result: list[InterfaceAddress] = []
    for name, addresses in psutil.net_if_addrs().items():
        is_up = stats.get(name).isup if name in stats else False
        for item in addresses:
            if item.family not in {2, 10} or not item.address or not item.netmask:
                continue
            address_text = item.address.split("%", 1)[0]
            try:
                interface = ipaddress.ip_interface(f"{address_text}/{item.netmask}")
            except ValueError:
                continue
            result.append(
                InterfaceAddress(
                    name=name,
                    address=interface.ip,
                    network=interface.network,
                    is_up=is_up,
                )
            )
    return result


def resolve_admin_network(
    *,
    interface_name: str | None = None,
    bind_address: str | None = None,
    allowed_cidrs: str | None = None,
    interfaces: list[InterfaceAddress] | None = None,
) -> AdminNetwork:
    available = interfaces if interfaces is not None else system_interfaces()
    if bind_address or allowed_cidrs or interface_name:
        if not (bind_address and allowed_cidrs):
            raise NetworkConfigurationError(
                "explicit Amnezia override requires bind address and allowed CIDR"
            )
        try:
            address = ipaddress.ip_address(bind_address)
            networks = tuple(
                ipaddress.ip_network(item.strip(), strict=False)
                for item in allowed_cidrs.split(",")
                if item.strip()
            )
        except ValueError as error:
            raise NetworkConfigurationError("Amnezia address or CIDR is invalid") from error
        if not networks or not _is_private_address(address):
            raise NetworkConfigurationError("Amnezia bind address must be private and specific")
        if any(not _is_private_network(network) for network in networks):
            raise NetworkConfigurationError("all Amnezia CIDRs must be private")
        if not any(address in network for network in networks):
            raise NetworkConfigurationError("Amnezia bind address is outside allowed CIDRs")
        matching = [
            item
            for item in available
            if item.is_up
            and item.address == address
            and (not interface_name or item.name == interface_name)
        ]
        if len(matching) != 1:
            raise NetworkConfigurationError("configured Amnezia interface/address is unavailable")
        selected_name = interface_name or matching[0].name
        return AdminNetwork(selected_name, str(address), networks)

    candidates = [
        item
        for item in available
        if item.is_up and VPN_NAME.match(item.name) and _is_private_address(item.address)
    ]
    unique = {(item.name, str(item.address), str(item.network)): item for item in candidates}
    if len(unique) != 1:
        raise NetworkConfigurationError(
            f"expected exactly one private Amnezia interface, found {len(unique)}"
        )
    selected = next(iter(unique.values()))
    return AdminNetwork(selected.name, str(selected.address), (selected.network,))


class VPNAccessMiddleware:
    def __init__(self, app: ASGIApp, allowed_networks: tuple[Any, ...]) -> None:
        self.app = app
        self.allowed_networks = allowed_networks

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        try:
            peer = ipaddress.ip_address(client[0] if client else "")
        except ValueError:
            peer = None
        if peer is None or not any(peer in network for network in self.allowed_networks):
            response = PlainTextResponse("Forbidden", status_code=403)
            await response(scope, receive, send)
            return
        # Forwarding headers are deliberately not parsed or trusted.
        scope["vpn_peer"] = str(peer)
        await self.app(scope, receive, send)
