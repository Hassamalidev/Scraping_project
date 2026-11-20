from typer.testing import CliRunner

from prodata.cli import app

runner = CliRunner()


def test_sources_list_works_without_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1:1/none")
    from prodata import config, db

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    result = runner.invoke(app, ["sources", "list"])
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    assert result.exit_code == 0, result.output
    assert "tx_tsbpe_plumbing" in result.output and "tx_tdlr_licenses" in result.output


def test_pending_commands_say_which_milestone():
    result = runner.invoke(app, ["geocode"])
    assert result.exit_code == 0 and "M3" in result.output


def test_export_rejects_unknown_market():
    result = runner.invoke(app, ["export", "--market", "nyc"])
    assert result.exit_code == 2
