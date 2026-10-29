"""The Head serves the built wiki at /wiki/ — to signed-in Users only."""

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import main as head_main
from conftest import USERS


@pytest.fixture
def wiki(head, monkeypatch, tmp_path):
    root = tmp_path / "wiki"
    (root / "Start").mkdir(parents=True)
    (root / "index.html").write_text("home")
    (root / "Start" / "index.html").write_text("start node")
    (root / "Start" / "Glossary.html").write_text("glossary")
    (root / "404.html").write_text("not here")
    (tmp_path / "secret.txt").write_text("nope")
    monkeypatch.setattr(head_main, "WIKI_DIR", root)
    c = TestClient(head_main.app, base_url="https://head")
    return c


def _sign_in(c):
    r = c.post("/api/login", json={"username": "alice", "password": USERS["alice"]})
    assert r.status_code == 200


class TestTheWiki:
    def test_no_session_goes_to_sign_in_and_back(self, wiki):
        r = wiki.get("/wiki/Start/Glossary", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?next=/wiki/Start/Glossary"

    def test_pages_resolve_the_quartz_way(self, wiki):
        _sign_in(wiki)
        assert wiki.get("/wiki/").text == "home"
        assert wiki.get("/wiki/Start/").text == "start node"
        assert wiki.get("/wiki/Start/Glossary").text == "glossary"
        assert wiki.get("/wiki/Start/Glossary.html").text == "glossary"
        (head_main.WIKI_DIR / "using-ccc").mkdir()
        (head_main.WIKI_DIR / "using-ccc" / "index.html").write_text("using")
        assert wiki.get("/wiki/Using-CCC/").text == "using"

    def test_bare_wiki_redirects_to_the_slash(self, wiki):
        _sign_in(wiki)
        r = wiki.get("/wiki", follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == "/wiki/"

    def test_a_missing_page_is_the_wikis_404(self, wiki):
        _sign_in(wiki)
        r = wiki.get("/wiki/Nope")
        assert r.status_code == 404 and r.text == "not here"

    def test_nothing_outside_the_wiki(self, wiki):
        _sign_in(wiki)
        r = wiki.get("/wiki/..%2Fsecret.txt")
        assert r.status_code == 404 and "nope" not in r.text
