"""Wiki pages link to the Dashboard as `dashboard:/…`; the build turns those back into Dashboard paths."""

from cuttle import setup


def test_dashboard_links_become_dashboard_paths(tmp_path):
    (tmp_path / "using-ccc").mkdir()
    page = tmp_path / "using-ccc" / "karma.html"
    page.write_text('<a href="dashboard:/karma" class="external">Open Karma</a> <a href="../dives">Dives</a>')
    setup.dashboard_links(tmp_path)
    assert page.read_text() == '<a data-router-ignore href="/karma" class="external">Open Karma</a> <a href="../dives">Dives</a>'
