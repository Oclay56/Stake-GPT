from __future__ import annotations

import pytest

from app.stake_sgm_browser import _check_page_ready, _find_or_open_fixture_page, fixture_url


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.navigated_to: list[str] = []

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.url = url
        self.navigated_to.append(url)


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages

    def new_page(self) -> FakePage:
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


class FakeLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, *, timeout: int) -> str:
        return self.text


class FakeReadyPage:
    def __init__(self, text: str) -> None:
        self.text = text

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "body"
        return FakeLocator(self.text)


def test_find_or_open_fixture_page_refreshes_restricted_region_tab():
    fixture_slug = "46575343-miami-marlins-atlanta-braves"
    page = FakePage(
        fixture_url(fixture_slug)
        + "?regionKey=US&country=US&region=GA&modal=restrictedRegion"
    )
    context = FakeContext([page])

    found = _find_or_open_fixture_page(context, fixture_slug)

    assert found is page
    assert page.navigated_to == [fixture_url(fixture_slug)]


def test_find_or_open_fixture_page_reuses_clean_fixture_tab():
    fixture_slug = "46575343-miami-marlins-atlanta-braves"
    page = FakePage(fixture_url(fixture_slug))
    context = FakeContext([page])

    found = _find_or_open_fixture_page(context, fixture_slug)

    assert found is page
    assert page.navigated_to == []


def test_check_page_ready_reports_cloudflare_verification():
    page = FakeReadyPage(
        "stake.com\nPerforming security verification\n"
        "This website uses a security service to protect against malicious bots."
    )

    with pytest.raises(RuntimeError, match="Cloudflare verification"):
        _check_page_ready(page)
