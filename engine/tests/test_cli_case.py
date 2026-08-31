"""The ``silkscreen case`` subcommand and the generate command's --case flags.

Everything here is offline: the ``--no-model`` path is deterministic by
contract (no API call at all), the render path is exercised with a stub module
whose ``render_stl`` raises ``RenderUnavailable``, and the generate-command
flag tests monkeypatch ``generate_pcb`` so no model or key is ever touched.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from silkscreen import cli
from silkscreen.enclosure.errors import RenderFailed, RenderUnavailable

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ref.kicad_pcb"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(), reason="board fixture not present"
)

# The raw fixture has no Edge.Cuts outline (test_enclosure_geometry.py proves
# board_envelope refuses it), so the case tests draw one the same way
# set_board_outline does: four gr_lines closing a rectangle.
OUTLINE_X0, OUTLINE_Y0, OUTLINE_X1, OUTLINE_Y1 = -4.0, -7.0, 15.0, 19.0


def _outlined_fixture(tmp_path: Path) -> Path:
    text = FIXTURE.read_text(encoding="utf-8")
    corners = [
        (OUTLINE_X0, OUTLINE_Y0),
        (OUTLINE_X1, OUTLINE_Y0),
        (OUTLINE_X1, OUTLINE_Y1),
        (OUTLINE_X0, OUTLINE_Y1),
    ]
    lines = []
    for i in range(4):
        sx, sy = corners[i]
        ex, ey = corners[(i + 1) % 4]
        lines.append(
            f'  (gr_line (start {sx} {sy}) (end {ex} {ey}) '
            f'(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'
        )
    body = text.rstrip()
    assert body.endswith(")")
    out = tmp_path / "outlined.kicad_pcb"
    out.write_text(body[:-1] + "\n" + "\n".join(lines) + "\n)\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------- case


def test_no_model_writes_a_complete_scad(tmp_path, capsys):
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.scad"

    code = cli.main(["case", str(board), "-o", str(out), "--no-model"])

    assert code == 0
    text = out.read_text(encoding="utf-8")
    # The default-spec case: both printable halves plus the parameter header
    # carrying the defaults the IR documents (2.0 mm wall, 1.0 mm clearance).
    assert "module base()" in text
    assert "module lid()" in text
    assert "wall = 2.000;" in text
    assert "clearance = 1.000;" in text
    captured = capsys.readouterr()
    assert str(out) in captured.out
    # The receipt is printed, with its signed margins.
    assert "Case fit:" in captured.out
    assert "+" in captured.out


def test_no_model_is_deterministic(tmp_path):
    board = _outlined_fixture(tmp_path)
    a = tmp_path / "a.scad"
    b = tmp_path / "b.scad"
    assert cli.main(["case", str(board), "-o", str(a), "--no-model"]) == 0
    assert cli.main(["case", str(board), "-o", str(b), "--no-model"]) == 0
    assert a.read_bytes() == b.read_bytes()


def test_no_model_needs_no_api_key(tmp_path, monkeypatch):
    """The offline path must never construct a model, key or no key."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def boom(*a, **kw):  # pragma: no cover - only fires on regression
        raise AssertionError("--no-model constructed a model")

    monkeypatch.setattr(cli, "GeminiModel", boom)
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.scad"
    assert cli.main(["case", str(board), "-o", str(out), "--no-model"]) == 0
    assert out.exists()


def test_a_board_without_an_outline_is_refused(tmp_path, capsys):
    out = tmp_path / "case.scad"
    code = cli.main(["case", str(FIXTURE), "-o", str(out), "--no-model"])
    assert code == 1
    assert not out.exists()
    assert "Edge.Cuts" in capsys.readouterr().err


def _stub_render(monkeypatch, render_stl):
    """Install a stand-in silkscreen.enclosure.render for one test.

    The module is owner C's and may not exist yet in this tree; the CLI
    imports it via importlib, which consults sys.modules first, so the stub
    works whether or not the real module has landed.
    """
    stub = types.ModuleType("silkscreen.enclosure.render")
    stub.render_stl = render_stl
    monkeypatch.setitem(sys.modules, "silkscreen.enclosure.render", stub)


def test_stl_without_openscad_names_the_executable(tmp_path, monkeypatch, capsys):
    def unavailable(scad, out_path, **kw):
        raise RenderUnavailable("openscad")

    _stub_render(monkeypatch, unavailable)
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.scad"

    code = cli.main(["case", str(board), "-o", str(out), "--no-model", "--stl"])

    assert code == 2
    err = capsys.readouterr().err
    assert "'openscad'" in err
    # The .scad itself is complete; only the render is missing.
    assert out.exists()


def test_stl_render_failure_is_reported(tmp_path, monkeypatch, capsys):
    def failing(scad, out_path, **kw):
        raise RenderFailed("CGAL error: something went sideways")

    _stub_render(monkeypatch, failing)
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.scad"

    code = cli.main(["case", str(board), "-o", str(out), "--no-model", "--stl"])

    assert code == 1
    assert "CGAL error" in capsys.readouterr().err


def test_stl_success_reports_the_stl_path(tmp_path, monkeypatch, capsys):
    written = {}

    def ok(scad, out_path, **kw):
        written["path"] = Path(out_path)
        Path(out_path).write_text("solid case\nendsolid case\n")
        return Path(out_path)

    _stub_render(monkeypatch, ok)
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.scad"

    code = cli.main(["case", str(board), "-o", str(out), "--no-model", "--stl"])

    assert code == 0
    assert written["path"] == out.with_suffix(".stl")
    assert str(out.with_suffix(".stl")) in capsys.readouterr().out


def test_case_subcommand_threads_the_rigorous_flag(tmp_path, monkeypatch):
    """``silkscreen case --rigorous`` reaches propose_enclosure; the default
    stays fast. The CLI imports propose_enclosure at call time, so patching
    the module attribute intercepts it."""
    import silkscreen.agents.enclosure as agent_enclosure
    from silkscreen.enclosure.ir import parse_enclosure_spec
    from silkscreen.enclosure.verify import verify_fit

    seen = {}

    def fake_propose(model, envelope, *, style_hint="", rigorous=False, **kw):
        seen["rigorous"] = rigorous
        spec = parse_enclosure_spec({})
        return spec, verify_fit(spec, envelope), 0

    monkeypatch.setattr(agent_enclosure, "propose_enclosure", fake_propose)
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.scad"

    assert cli.main(["case", str(board), "-o", str(out), "--rigorous"]) == 0
    assert seen["rigorous"] is True
    assert cli.main(["case", str(board), "-o", str(out)]) == 0
    assert seen["rigorous"] is False


# ------------------------------------------------- generate command flags


def test_board_only_case_into_a_missing_directory_writes_the_board(
    tmp_path, monkeypatch, capsys
):
    """Regression: ``--board-only --case`` with an output directory that does
    not exist yet must complete and write the board. The enclosure.scad write
    used to run outside the stage's try, before ``_finish``, and its
    FileNotFoundError killed the whole run."""
    import json

    from test_agents import _scripted_pipeline_model
    from test_enclosure_agent import GOOD_ENCLOSURE

    monkeypatch.setenv("SILKSCREEN_ENGINE", "sdk")
    model = _scripted_pipeline_model()
    model.by_marker["ENCLOSURE-SPEC v1"] = json.dumps(GOOD_ENCLOSURE)
    monkeypatch.setattr(cli, "GeminiModel", lambda name: model)

    out = tmp_path / "does" / "not" / "exist" / "board.kicad_pcb"
    assert not out.parent.exists()
    code = cli.main(
        ["a 3.3V motor driver board", "-o", str(out), "--board-only", "--case"]
    )

    assert code == 0
    assert out.exists()
    # --board-only promises only the routed board; the receipt still prints.
    assert not (out.parent / "enclosure.scad").exists()
    captured = capsys.readouterr()
    assert "Case fit:" in captured.out
    assert str(out) in captured.out


def _fake_result(enclosure=None):
    return SimpleNamespace(
        summary=lambda: "1 part board",
        board=SimpleNamespace(parts=[], warnings=[]),
        findings=[],
        route=None,
        artifacts=[],
        project_path=None,
        enclosure=enclosure,
    )


@pytest.fixture()
def captured_generate(monkeypatch):
    seen = {}

    def fake_generate_pcb(model, intent, **kw):
        seen.update(kw)
        return _fake_result()

    monkeypatch.setattr(cli, "generate_pcb", fake_generate_pcb)
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())
    return seen


def test_case_flag_opts_in_to_the_enclosure_kwargs(
    tmp_path, captured_generate, capsys
):
    out = tmp_path / "board.kicad_pcb"
    code = cli.main(
        ["an ldo board", "-o", str(out), "--case", "--case-style", "usb left"]
    )
    assert code == 0
    assert captured_generate["enclosure"] is True
    assert captured_generate["enclosure_style"] == "usb left"
    # Fast is the default: rigor is opt-in via --rigorous.
    assert captured_generate["enclosure_rigorous"] is False
    # A run whose stage failed says so, and still exits cleanly.
    assert "without one" in capsys.readouterr().err


def test_rigorous_flag_opts_in_to_the_strict_loop(tmp_path, captured_generate):
    out = tmp_path / "board.kicad_pcb"
    code = cli.main(["an ldo board", "-o", str(out), "--case", "--rigorous"])
    assert code == 0
    assert captured_generate["enclosure"] is True
    assert captured_generate["enclosure_rigorous"] is True


def test_without_case_the_kwargs_are_absent(tmp_path, captured_generate):
    """The default call must stay byte-for-byte what it always was."""
    out = tmp_path / "board.kicad_pcb"
    assert cli.main(["an ldo board", "-o", str(out)]) == 0
    assert "enclosure" not in captured_generate
    assert "enclosure_style" not in captured_generate
    assert "enclosure_rigorous" not in captured_generate


def test_case_success_prints_the_receipt(tmp_path, monkeypatch, capsys):
    fit = SimpleNamespace(
        margins_nm={"x": 1_000_000, "y": 1_000_000, "z": -550_000},
        warnings=("U1 height defaulted to 3.0 mm",),
        params_mm={"wall": 2.0},
    )
    enclosure = SimpleNamespace(fit=fit, repair_rounds=2, scad="// scad")
    monkeypatch.setattr(
        cli, "generate_pcb", lambda model, intent, **kw: _fake_result(enclosure)
    )
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())

    code = cli.main(["an ldo", "-o", str(tmp_path / "b.kicad_pcb"), "--case"])

    assert code == 0
    captured = capsys.readouterr()
    assert "Case fit:" in captured.out
    assert "+1.000 mm" in captured.out
    assert "-0.550 mm" in captured.out, "a collision must keep its sign"
    assert "height defaulted" in captured.err
    assert "repair rounds: 2" in captured.err
