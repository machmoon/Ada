"""Provider ordering for the live service model factory."""

from service.app import build_model


def test_glm_is_the_only_provider_without_google_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv(
        "OPENCODE_FALLBACK_MODEL", "opencode-go/glm-5.3-flash"
    )

    model = build_model()

    assert [provider.name for provider in model.providers] == [
        "opencode-glm-5.3-flash"
    ]
    assert model.providers[0].model.timeout_s == 300.0


def test_glm_follows_both_gemini_tiers(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv(
        "OPENCODE_FALLBACK_MODEL", "opencode-go/glm-5.3-flash"
    )

    model = build_model()

    assert [provider.name for provider in model.providers] == [
        "gemini-primary",
        "gemini-cheap",
        "opencode-glm-5.3-flash",
    ]
