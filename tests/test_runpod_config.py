import stat

from dndmusic.runpod.config import RunpodConfig, load_config, save_config


def cfg(**kw):
    return RunpodConfig(**{"endpoint_id": "ep1", "api_key": "secret-key-xyz", "rate_per_hr": 0.58, **kw})


def test_round_trip_and_mode(tmp_path):
    c = cfg(daily_cap=2.5, budget_usd=20.0)
    save_config(tmp_path, c)
    assert load_config(tmp_path, {}) == c
    assert stat.S_IMODE((tmp_path / "runpod.json").stat().st_mode) == 0o600


def test_env_overrides_file(tmp_path):
    save_config(tmp_path, cfg())
    got = load_config(tmp_path, {"RUNPOD_API_KEY": "envkey", "RUNPOD_ENDPOINT_ID": "envep"})
    assert (got.api_key, got.endpoint_id, got.rate_per_hr) == ("envkey", "envep", 0.58)


def test_env_alone_is_enough(tmp_path):
    got = load_config(tmp_path, {"RUNPOD_API_KEY": "k", "RUNPOD_ENDPOINT_ID": "e"})
    assert got is not None and got.rate_per_hr == 0.0


def test_nothing_configured_is_none(tmp_path):
    assert load_config(tmp_path, {}) is None
    assert load_config(tmp_path, {"RUNPOD_API_KEY": "k"}) is None


def test_repr_hides_key():
    assert "secret-key-xyz" not in repr(cfg())
