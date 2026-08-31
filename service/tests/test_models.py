"""Model discovery is dynamic, but selection stays server-controlled."""

from service import models


def test_catalog_caches_the_live_discovery(monkeypatch):
    found = {
        "default": "auto",
        "auto_model": "gemini-a",
        "source": "gemini",
        "models": [{"id": "gemini-a"}],
    }
    calls = []
    monkeypatch.setattr(models, "_cache", None)
    monkeypatch.setattr(models, "_live_catalog", lambda: calls.append(1) or found)

    assert models.model_catalog() is found
    assert models.model_catalog() is found
    assert calls == [1]


def test_auto_and_an_advertised_model_are_selectable():
    catalog = {
        "auto_model": "gemini-auto",
        "models": [{"id": "gemini-auto"}, {"id": "gemini-debug"}],
    }

    assert models.select_model("auto", catalog) == "gemini-auto"
    assert models.select_model("gemini-debug", catalog) == "gemini-debug"


def test_an_arbitrary_model_id_is_rejected():
    catalog = {"auto_model": "gemini-auto", "models": [{"id": "gemini-auto"}]}

    try:
        models.select_model("gemini-invented", catalog)
    except ValueError as exc:
        assert "available Gemini model" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("an unadvertised model was accepted")
