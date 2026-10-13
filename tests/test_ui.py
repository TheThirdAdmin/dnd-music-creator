import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dndmusic.app import build_services, create_app
from dndmusic.config import Settings
from fakes import FakeBackend


@pytest.fixture
def client(tmp_path):
    services = build_services(Settings(8000, "http://dm.test:8000", (), tmp_path), cast_backend=FakeBackend())
    with TestClient(create_app(lambda: services, start_background=False)) as c:
        yield c


def test_index_is_mobile_page(client):
    html = client.get("/").text
    assert '<meta name="viewport"' in html and 'rel="manifest"' in html
    assert 'id="scenes"' in html and 'id="editor"' in html


@pytest.mark.parametrize("path,ctype", [
    ("/static/app.js", "javascript"),
    ("/static/style.css", "text/css"),
    ("/static/manifest.webmanifest", ""),
    ("/static/icon.svg", "image/svg+xml"),
])
def test_static_assets(client, path, ctype):
    r = client.get(path)
    assert r.status_code == 200 and ctype in r.headers["content-type"]


def test_scene_form_uses_location_mood_tempo(client):
    html = client.get("/").text
    for needle in ('name="location"', 'name="mood"', 'name="tempo"', 'name="choir"',
                   'name="start" value="build"', 'name="start" value="straight"'):
        assert needle in html
    assert html.index('name="location"') < html.index('name="mood"') < html.index('name="tempo"')
    js = client.get("/static/app.js").text
    for gone in ("genre", "tones", "intensity", "vocals"):
        assert gone not in js


def test_settings_has_quality_slider_and_credit(client):
    html = client.get("/").text
    assert 'id="quality"' in html and 'min="1"' in html and 'max="3"' in html
    assert "Powered by Stability AI" in html
    js = client.get("/static/app.js").text
    for label in ("Primary school guitar lesson", "Regional orchestra on a good night", "(almost) John Williams"):
        assert label in js


def test_quality_hint_uses_curly_quotes_and_agrees_in_number(client):
    js = client.get("/static/app.js").text
    assert "\\u201c" in js and "\\u201d" in js
    assert "'need' : 'needs'" in js
    assert '`"${QUALITY_LABELS' not in js


PALETTE = ("ground", "panel", "frame", "frame-edge", "text", "muted", "accent", "good", "bad", "info")


def test_theme_palette_font_and_no_rounded_corners(client):
    css = client.get("/static/style.css").text
    for name in PALETTE:
        assert f"--{name}:" in css
    assert "border-radius" not in css.replace("border-radius: 0", "")
    assert "blur(" not in css and "gradient(" not in css
    assert "@font-face" in css
    font = client.get("/static/fonts/PixelifySans-subset.woff2")
    assert font.status_code == 200 and "font/woff2" in font.headers["content-type"]
    assert 0 < len(font.content) <= 25_000
    assert client.get("/static/fonts/OFL.txt").status_code == 200


def test_index_has_two_background_layers_and_shade(client):
    html = client.get("/").text
    assert 'id="bgA"' in html and 'id="bgB"' in html and 'class="bg-shade"' in html


def test_bg_manifest_paths_exist(client):
    r = client.get("/static/bg/manifest.json")
    assert r.status_code == 200 and "json" in r.headers["content-type"]
    m = r.json()
    paths = [p for s in m["sets"].values() for p in s] + m["generic"] + list(m.get("icons", {}).values())
    assert m["generic"] and paths
    for p in paths:
        assert client.get(f"/static/bg/{p}").status_code == 200, p


def test_cards_and_bar_show_scene_icon(client):
    js = client.get("/static/app.js").text
    assert 'class="sicon"' in js and 'alt="${esc(s.name)}"' in js and "iconFor" in js
    assert "/static/icon.svg" in js  # generic emblem fallback
    assert 'id="nowicon"' in client.get("/").text
    css = client.get("/static/style.css").text
    assert "image-rendering: pixelated" in css
    svg = client.get("/static/icon.svg").text
    assert "#e8b64a" in svg and "#14101f" in svg and "rx=" not in svg


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_bg_logic_node_tests():
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(["node", "--test", "tests/bg.test.mjs"], cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-500:]


def test_describe_scene_ui(client):
    html = client.get("/").text
    assert 'id="describe"' in html and 'id="descr"' in html and 'id="describego"' in html
    assert 'id="describehint"' in html and "Keep it short" in html
    assert 'maxlength="300"' in html and "sea battle against pirates at dusk" in html
    js = client.get("/static/app.js").text
    assert "/api/scene-from-text" in js and "describeScene" in js and "New scene (from text)" in js
    css = client.get("/static/style.css").text
    assert "#describe" in css and "min-height: 44px" in css.split("#describe", 1)[1]


def test_library_tab_and_controls_present(client):
    html = client.get("/").text
    for ident in ('data-tab="library"', 'id="tab-library"', 'id="favbtn"', 'id="banbtn"', 'id="libcap"',
                  'id="preview"', 'id="importfile"', 'href="/api/library/export"'):
        assert ident in html
    js = client.get("/static/app.js").text
    for ref in ("/api/library", "/api/tracks/", "library_cap_gb", "/api/library/import", "/api/library/cleanup"):
        assert ref in js


def test_players_page_and_dm_control(client):
    html = client.get("/players").text
    assert 'id="like"' in html and 'id="dislike"' in html and "/static/players.js" in html
    js = client.get("/static/players.js").text
    assert "/api/players/vote" in js and "/api/players/state" in js
    assert 'id="players"' in client.get("/").text


def test_scene_search_box(client):
    assert 'id="scenesearch"' in client.get("/").text
    js = client.get("/static/app.js").text
    assert "sceneMatches" in js and "#scenesearch" in js


def test_runpod_settings_and_banners(client):
    html = client.get("/").text
    assert 'id="runpod-key"' in html and 'type="password"' in html and 'autocomplete="off"' in html
    assert "Turn auto-pay off in Runpod (Settings, Billing) so spending stops at your balance." in html
    for needle in ('id="rp-save"', 'id="rp-test"', 'id="rp-reset"', 'id="statusrunpod"'):
        assert needle in html
    js = client.get("/static/app.js").text
    assert "Warming up the generator on Runpod. First track in about " in js
    assert "The first start can take 6-10 minutes (container download and model load); later starts are much quicker." in js
    assert "Runpod balance is $" in js and "tracks left" in js
    assert "localStorage" not in js[js.index("async function saveRunpod"):js.index("async function resetRunpod")]


def test_track_progress_bar_under_volume(client):
    html = client.get("/").text
    assert html.index('id="volume"') < html.index('id="progress"')
    assert 'role="progressbar"' in html and 'aria-valuemin="0"' in html and 'aria-valuemax="100"' in html
    js = client.get("/static/app.js").text
    assert "#progressfill" in js and "Math.min(100" in js
    assert ".progress" in client.get("/static/style.css").text
