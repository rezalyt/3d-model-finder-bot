import importlib

import pytest


@pytest.fixture(scope="module")
def bot(monkeypatch_module):
    monkeypatch_module.setenv("BOT_TOKEN", "test-token")
    module = importlib.import_module("bot")
    return module


@pytest.fixture
def clear_state(bot):
    bot.search_cache.clear()
    bot.search_timestamps.clear()
    yield
    bot.search_cache.clear()
    bot.search_timestamps.clear()


def test_extract_and_normalize_query(bot):
    query, fmt = bot.extract_and_normalize_query("Найди STL кота для печати")
    assert query == "cat печати"
    assert fmt == "stl"


def test_extract_query_handles_empty_input(bot):
    assert bot.extract_and_normalize_query("   ") == ("", None)


def test_cache_is_bounded(bot, clear_state, monkeypatch):
    monkeypatch.setattr(bot, "CACHE_MAX", 2)
    bot.cache_put(("a", False, None, None), [1])
    bot.cache_put(("b", False, None, None), [2])
    bot.cache_put(("c", False, None, None), [3])

    assert len(bot.search_cache) == 2
    assert bot.cache_get(("a", False, None, None)) is None
    assert bot.cache_get(("c", False, None, None)) == [3]


def test_cache_expires(bot, clear_state, monkeypatch):
    bot.cache_put(("a", False, None, None), [1])
    key = ("a", False, None, None)
    bot.search_cache[key]["ts"] -= bot.CACHE_TTL + 1
    assert bot.cache_get(key) is None
    assert key not in bot.search_cache


def test_search_rate_limit(bot, clear_state):
    user_id = 123456
    assert bot.rate_limit_search(user_id) == 0
    assert bot.rate_limit_search(user_id) > 0


def test_build_caption_escapes_html(bot):
    caption = bot.build_caption(
        {
            "name": "<cat>",
            "license": {"label": "CC BY & CC0"},
            "formats": ["stl", "obj"],
            "source": "test",
        },
        1,
    )
    assert "&lt;cat&gt;" in caption
    assert "CC BY &amp; CC0" in caption
    assert "STL, OBJ" in caption


def test_normalize_service_model(bot):
    model = bot.normalize_service_model(
        {
            "name": "Chair",
            "sourceUrl": "https://example.test/chair",
            "license": "CC0",
            "formats": ["stl"],
            "source": "printables",
        },
        1,
    )
    assert model["name"] == "Chair"
    assert model["viewerUrl"] == "https://example.test/chair"
    assert model["license"]["label"] == "CC0"
    assert model["source"] == "printables"


def test_preview_url_prefers_explicit_thumbnail(bot):
    assert bot.get_preview_url({"thumbnail": "https://example.test/a.jpg"}) == "https://example.test/a.jpg"


def test_preview_url_uses_256_thumbnail(bot):
    model = {
        "thumbnails": {
            "images": [
                {"size": 128, "url": "https://example.test/128.jpg"},
                {"size": 256, "url": "https://example.test/256.jpg"},
            ]
        }
    }
    assert bot.get_preview_url(model) == "https://example.test/256.jpg"
