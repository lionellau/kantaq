"""Config tests (E22-T2): HUB_MODE selection + loopback defaults."""

from kantaq_runtime.config import HubMode, Settings


def test_defaults_are_local_and_loopback() -> None:
    settings = Settings(_env_file=None)
    assert settings.hub_mode is HubMode.local
    assert settings.host == "127.0.0.1"  # loopback only (FR-E22-2)
    assert settings.port == 3939
    assert settings.supabase_url is None


def test_supabase_mode_parsed_from_values() -> None:
    settings = Settings(
        _env_file=None,
        hub_mode="supabase",
        supabase_url="https://abc.supabase.co",
        supabase_anon_key="anon-key",
    )
    assert settings.hub_mode is HubMode.supabase
    assert settings.supabase_url == "https://abc.supabase.co"
