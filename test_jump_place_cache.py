from jump_search import JumpSearchMixin


class _Search(JumpSearchMixin):
    pass


def test_cached_online_places_remain_search_candidates(tmp_path, monkeypatch):
    cache = tmp_path / "place_cache.json"
    monkeypatch.setattr("jump_search.PLACE_CACHE_PATH", str(cache))
    search = _Search()
    search._cache_place_result("Darling Harbour, Sydney", -33.8748, 151.2000)

    candidates = search._cached_place_candidates("darling", lambda _label: 0)

    assert candidates[0][0] == "Darling Harbour, Sydney"
    assert candidates[0][4] == "cache"
