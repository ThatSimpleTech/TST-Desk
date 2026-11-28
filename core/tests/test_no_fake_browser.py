"""Browser tools say when no real browser is behind them (CU-09).

The shipped default is `computer_use.browser: mock`, and Playwright is not
a dependency, so in a packaged app the browser tools ran against the mock:
browser_navigate("https://example.com/") reported success and a 1x1 PNG
came back, with no hint that nothing was opened.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_security_suite import make_dispatcher
from tstd.browser.factory import browser_driver_from_config
from tstd.browser.mock import MockBrowserDriver
from tstd.config import default_config_yaml
from tstd.config_write import check_config_text
from tstd.tools.browser import register_browser_handlers


async def test_the_shipped_default_refuses_and_says_why(tmp_path: Path) -> None:
    config = check_config_text(default_config_yaml(), tmp_path / "config.yaml")
    driver = browser_driver_from_config(config, tmp_path)
    assert isinstance(driver, MockBrowserDriver) and driver.stands_in
    dispatcher = make_dispatcher(tmp_path)
    register_browser_handlers(dispatcher, driver)
    for name, args in (
        ("browser_navigate", {"url": "https://example.com/"}),
        ("browser_screenshot", {}),
    ):
        result = await dispatcher.dispatch("tc", name, args)
        assert result.status == "error" and result.error_code == "no_real_browser"
        assert "nothing was opened" in result.output


async def test_a_mock_a_test_builds_still_scripts_pages(tmp_path: Path) -> None:
    dispatcher = make_dispatcher(tmp_path)
    register_browser_handlers(
        dispatcher, MockBrowserDriver(pages={"https://example.com/": "Example"})
    )
    result = await dispatcher.dispatch("tc", "browser_navigate", {"url": "https://example.com/"})
    assert result.status == "success"
