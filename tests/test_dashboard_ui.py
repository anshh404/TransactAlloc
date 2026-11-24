from pathlib import Path


APP_JS = Path(__file__).parents[1] / "static" / "app.js"


def test_dashboard_drives_deadlock_from_live_state():
    """The Deadlock section renders the engine's live graph and session."""
    source = APP_JS.read_text(encoding="utf-8")

    assert 'const wfg = (state && state.wfg) || {};' in source
    assert '"state.deadlock"' in source or '(state && state.deadlock)' in source
    assert '"/api/deadlock/trigger"' in source
    assert '"/api/deadlock/resolve"' in source
    assert '"detected"' in source


def test_dashboard_has_no_scripted_demo_graph():
    """No scripted circular-wait graph or demo wording remains in the client."""
    source = APP_JS.read_text(encoding="utf-8").lower()

    assert "demowfg" not in source
    assert "deadlock demo" not in source
    assert "demo transcript" not in source
