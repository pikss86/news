import ipaddress

import pytest

from app.admin.network import (
    InterfaceAddress,
    NetworkConfigurationError,
    resolve_admin_network,
)
from app.admin.security import AdminPasswordStore, LoginRateLimiter, SessionManager


def interface(name: str, address: str, prefix: int = 24) -> InterfaceAddress:
    ip = ipaddress.ip_address(address)
    return InterfaceAddress(name, ip, ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def test_conservative_amnezia_discovery() -> None:
    selected = resolve_admin_network(interfaces=[interface("awg0", "10.8.0.1")])
    assert selected.bind_address == "10.8.0.1"
    with pytest.raises(NetworkConfigurationError):
        resolve_admin_network(interfaces=[])
    with pytest.raises(NetworkConfigurationError):
        resolve_admin_network(
            interfaces=[interface("awg0", "10.8.0.1"), interface("wg1", "10.9.0.1")]
        )


@pytest.mark.parametrize(
    ("address", "cidr"),
    [("0.0.0.0", "10.8.0.0/24"), ("8.8.8.8", "8.8.8.0/24"), ("224.0.0.1", "224.0.0.0/24")],
)
def test_explicit_network_rejects_unsafe_addresses(address, cidr) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NetworkConfigurationError):
        resolve_admin_network(bind_address=address, allowed_cidrs=cidr, interfaces=[])


def test_explicit_network_requires_matching_live_interface() -> None:
    with pytest.raises(NetworkConfigurationError):
        resolve_admin_network(
            interface_name="awg0",
            bind_address="10.8.0.1",
            allowed_cidrs="10.9.0.0/24",
            interfaces=[interface("awg0", "10.8.0.1")],
        )


def test_password_hash_sessions_csrf_and_rate_limit(tmp_path) -> None:
    password = AdminPasswordStore(tmp_path / "admin.json")
    password.set_password("a sufficiently long password")
    assert password.verify("a sufficiently long password")
    assert "sufficiently" not in password.path.read_text()

    sessions = SessionManager(idle_seconds=10, absolute_seconds=20)
    session = sessions.create(now=0)
    assert sessions.validate(session, now=5)
    csrf = sessions.issue_csrf(session)
    assert sessions.consume_csrf(session, csrf)
    assert not sessions.consume_csrf(session, csrf)
    assert not sessions.validate(session, now=21)
    assert not SessionManager().validate(session)

    limiter = LoginRateLimiter(attempts=2, window_seconds=10)
    limiter.fail("10.8.0.2", now=0)
    limiter.fail("10.8.0.2", now=1)
    assert not limiter.allowed("10.8.0.2", now=2)
    assert limiter.allowed("10.8.0.2", now=12)
