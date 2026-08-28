from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from app.admin.app import create_admin_app
from app.admin.browser import DataBrowser
from app.admin.checks import PreflightRunner
from app.admin.network import resolve_admin_network
from app.admin.security import AdminPasswordStore
from app.logging import configure_logging
from app.settings.bootstrap import load_bootstrap
from app.settings.control import ControlChannel
from app.settings.store import SettingsStore


def main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    secrets_directory = Path(os.environ.get("NEWS_SECRETS_DIR", "/run/news-secrets"))
    settings_directory = Path(os.environ.get("NEWS_SETTINGS_DIR", "/var/lib/news-settings"))
    bootstrap = load_bootstrap(secrets_directory)
    network = resolve_admin_network(
        interface_name=os.environ.get("AMNEZIA_ADMIN_INTERFACE") or None,
        bind_address=os.environ.get("AMNEZIA_ADMIN_BIND_ADDRESS") or None,
        allowed_cidrs=os.environ.get("AMNEZIA_ADMIN_ALLOWED_CIDRS") or None,
    )
    store = SettingsStore(settings_directory, bootstrap.settings_key_file)
    control = ControlChannel(settings_directory, bootstrap.control_key_file)
    database_host_override = os.environ.get("NEWS_ADMIN_DATABASE_HOST") or None
    app = create_admin_app(
        store=store,
        control=control,
        password_store=AdminPasswordStore(settings_directory / "admin.json"),
        network=network,
        secrets_directory=secrets_directory,
        cookie_secure=os.environ.get("ADMIN_COOKIE_SECURE", "false").lower() == "true",
        preflight=PreflightRunner(database_host_override=database_host_override),
        data_browser=DataBrowser(database_host_override=database_host_override),
    )
    uvicorn.run(
        app,
        host=network.bind_address,
        port=int(os.environ.get("ADMIN_PORT", "8080")),
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
