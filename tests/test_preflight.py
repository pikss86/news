from app.admin.checks import PreflightRunner
from tests.factories import settings


async def test_unreachable_database_reports_component_errors() -> None:
    runner = PreflightRunner()
    postgresql, migrations = await runner.check_database(
        settings(database_url="postgresql+asyncpg://news:secret@127.0.0.1:1/news")
    )
    assert not postgresql["ok"]
    assert postgresql["status"] == "error"
    assert not migrations["ok"]
    assert migrations["status"] == "blocked"
    assert "secret" not in str((postgresql, migrations))


async def test_tdlib_and_storage_preflight_in_container() -> None:
    runner = PreflightRunner()
    configured = settings(tdlib_data_dir="/var/lib/tdlib/preflight-test")
    tdlib = await runner.check_tdlib(configured)
    storage = await runner.check_storage(configured)
    assert tdlib["ok"]
    assert storage["ok"]
