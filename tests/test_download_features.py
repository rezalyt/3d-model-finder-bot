import os
import sys

os.environ.setdefault("BOT_TOKEN", "test-token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_features import consume_download_slot, model_download_url, safe_filename  # noqa: E402


def test_download_url_requires_http_scheme():
    assert model_download_url({"downloadUrl": "https://example.test/model.glb"}) == "https://example.test/model.glb"
    assert model_download_url({"downloadUrl": "javascript:alert(1)"}) is None
    assert model_download_url({"downloadUrl": ""}) is None


def test_safe_filename_removes_unsafe_characters():
    filename = safe_filename({"name": "Cat <test>"}, "https://example.test/files/cat.glb")
    assert filename == "Cat _test_.glb"


def test_download_daily_limit_is_bounded(monkeypatch):
    import bot_features

    monkeypatch.setattr(bot_features, "DOWNLOAD_DAILY_LIMIT", 2)
    bot_features._download_usage.clear()

    assert consume_download_slot(123)[0] is True
    assert consume_download_slot(123)[0] is True
    assert consume_download_slot(123)[0] is False
