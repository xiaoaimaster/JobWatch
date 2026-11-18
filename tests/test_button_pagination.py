import json
import os
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, Mock, patch

from jobwatch.browser import BrowserPages
from jobwatch.config import validate_source
from jobwatch.sources import collect
import test_watch


class ButtonPaginationTests(TestCase):
    def setUp(self):
        self.site = test_watch.SiteTests()
        self.site.setUp()
        self.addCleanup(self.site.doCleanups)
        options = {
            "next_button_xpath": "//button[@id='next']",
            "current_page_xpath": "//*[@id='current']",
            "page_size": 2,
        }
        self.source = self.site.source(
            "/button-jobs", "html_http", items_xpath="//article",
            total_xpath="string(//*[@id='total'])", empty_xpath="//*[@id='empty']",
            browser={"timeout_seconds": 5, "pagination": options},
            fields={"id": "./@data-id", "title": "./a/text()", "url": "./a/@href"},
        )

    def run_pages(self, pages, **settings):
        with patch("jobwatch.sources.IsolatedBrowser", return_value=MagicMock()) as renderer:
            browser = renderer.return_value
            browser.close.return_value = ""
            browser.get.return_value = pages[0]
            browser.advance.side_effect = pages[1:]
            result = collect(dict(self.source, **settings), self.site.root / "run")
        return result, browser.advance

    def test_collects_three_pages_with_short_last_page(self):
        pages = (listing(1, 5, [1, 2]), listing(2, 5, [3, 4]), listing(3, 5, [5]))
        result, advance = self.run_pages(pages)
        self.assertEqual(result.status, "complete")
        self.assertEqual(set(result.jobs), {"1", "2", "3", "4", "5"})
        self.assertEqual((result.pages, result.declared_total), (3, 5))
        self.assertEqual([call.args[1:] for call in advance.call_args_list], [(2, 2), (3, 1)])

    def test_page_budget_preserves_first_page_and_marks_partial(self):
        result, advance = self.run_pages([listing(1, "10,000", [1, 2])], max_pages=1)
        self.assertEqual((result.status, result.pages, len(result.jobs)), ("partial", 1, 2))
        self.assertEqual(result.declared_total, 10000)
        self.assertIn("上限", result.errors[0])
        advance.assert_not_called()

    def test_page_number_total_overlap_and_mismatched_count(self):
        cases = [
            (listing(1, 4, [3, 4]), "页码未切换"),
            (listing(2, 5, [3, 4]), "总数变化"),
            (listing(2, 4, [2, 3]), "重复"),
            (listing(2, 4, [3]), "岗位数"),
            (listing(2, 4, [3, 4], links=[2, 4]), "重复"),
        ]
        for second, message in cases:
            with self.subTest(message=message):
                result, _advance = self.run_pages([listing(1, 4, [1, 2]), second])
                self.assertEqual((result.status, result.pages, set(result.jobs)), ("partial", 1, {"1", "2"}))
                self.assertIn(message, result.errors[0])

    def test_zero_result_requires_success_validation(self):
        empty = b'<div id="total">0</div><div id="empty">No jobs</div>'
        result, _advance = self.run_pages([empty])
        self.assertEqual(result.status, "failed")
        buttons = dict(self.source["browser"]["pagination"], response_path="/button-data",
                       response_success_path="ok", response_success_value=True)
        result, _advance = self.run_pages([empty], browser={"pagination": buttons})
        self.assertEqual(result.status, "complete")

    def test_requires_total_and_excludes_link_pagination(self):
        for patch_fields in ({"total_xpath": ""}, {"next_xpath": "//a/@href"}, {"single_page": True}):
            with self.subTest(patch_fields=patch_fields):
                with self.assertRaisesRegex(ValueError, "点击分页"):
                    validate_source(dict(self.source, **patch_fields), self.site.root)

    def test_overlay_configuration_requires_both_selectors(self):
        pagination = self.source["browser"]["pagination"]
        for config in ({}, {"overlay_xpath": "//div"}, {"overlay_xpath": "//div", "close_button_xpath": " "}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                browser = {"pagination": dict(pagination, dismiss_overlay=config)}
                validate_source(dict(self.source, browser=browser), self.site.root)

    def test_overlay_is_closed_only_when_present_and_control_is_unique(self):
        browser = BrowserPages(self.source)
        browser.timeout = 200
        browser.page = Mock()
        overlay, close = Mock(), Mock()
        browser.page.locator.side_effect = lambda selector: {
            "xpath=//*[@id='guide']": overlay, "xpath=//*[@id='close-guide']": close,
        }[selector]
        overlay.filter.return_value = overlay
        close.filter.return_value = close
        options = {"overlay_xpath": "//*[@id='guide']", "close_button_xpath": "//*[@id='close-guide']"}
        overlay.count.return_value = 0
        self.assertFalse(browser.dismiss_pagination_overlay(options))
        close.click.assert_not_called()
        overlay.count.return_value = 1
        close.count.return_value = 2
        with self.assertRaisesRegex(ValueError, "未唯一匹配"):
            browser.dismiss_pagination_overlay(options)
        close.click.assert_not_called()
        close.count.return_value = 1
        self.assertTrue(browser.dismiss_pagination_overlay(options))
        close.click.assert_called_once_with(timeout=200)
        overlay.first.wait_for.assert_called_once_with(state="hidden", timeout=200)

    def test_http_success_with_business_failure_is_rejected(self):
        browser = BrowserPages(self.source)
        browser.error = RuntimeError
        response = Mock(status=200)
        response.json.return_value = {"ok": False}
        with self.assertRaisesRegex(ValueError, "业务失败"):
            browser.verify_search(response, {"response_success_path": "ok", "response_success_value": True})

    def test_pagination_trace_distinguishes_click_response_and_update_timeouts(self):
        self.source["browser"]["pagination"]["response_path"] = "/button-data"
        for phase in ("点击下一页按钮", "等待岗位查询响应", "等待页码和岗位更新"):
            with self.subTest(phase=phase):
                browser = BrowserPages(self.source)
                browser.error, browser.timeout_error, browser.timeout = RuntimeError, TimeoutError, 200
                browser.page = MagicMock()
                browser.page.content.return_value = "<html><body></body></html>"
                browser.page.evaluate.side_effect = ["1", ["1", "2"], "1", ["1", "2"]]
                button = browser.page.locator.return_value
                button.count.return_value = 1
                button.is_disabled.return_value = False
                response = browser.page.expect_response.return_value
                response.__enter__.return_value.value.status = 200
                if phase == "点击下一页按钮":
                    button.click.side_effect = TimeoutError
                elif phase == "等待岗位查询响应":
                    response.__exit__.side_effect = TimeoutError
                else:
                    browser.page.wait_for_function.side_effect = TimeoutError
                file = self.site.root / "page-002.html"
                with self.assertRaisesRegex(ValueError, phase + "超时"):
                    browser.advance(file, 2, 2)
                trace = json.loads(file.with_suffix(".pagination.json").read_text(encoding="utf-8"))
                self.assertEqual((trace["phase"], trace["page_before"], trace["page_after"]), (phase, "1", "1"))
                self.assertEqual(trace["click_returned"], phase != "点击下一页按钮")
                self.assertEqual((trace["requests_seen"], trace["rows_repeated"]), (0, 2))

    @skipUnless(os.environ.get("JOBWATCH_TEST_BROWSER") == "1", "需要可启动的 Chromium")
    @patch("jobwatch.sources.time.sleep")
    def test_real_browser_clicks_two_pages(self, _sleep):
        settings = self.source["browser"]["pagination"]
        settings.update(response_path="/button-data", response_success_path="ok",
                        response_success_value=True)
        result = collect(self.source, self.site.root / "rendered")
        self.assertEqual(result.errors, [])
        self.assertEqual((result.status, result.pages), ("complete", 2))
        self.assertEqual(set(result.jobs), {"1", "2", "3"})
        trace = json.loads((self.site.root / "rendered/page-002.pagination.json").read_text(encoding="utf-8"))
        self.assertEqual((trace["page_before"], trace["page_after"], trace["rows_after"], trace["rows_repeated"]),
                         ("1", "2", 1, 0))
        self.assertEqual((trace["requests_seen"], trace["http_statuses"]), (1, [200]))

    @skipUnless(os.environ.get("JOBWATCH_TEST_BROWSER") == "1", "需要可启动的 Chromium")
    @patch("jobwatch.sources.time.sleep")
    def test_real_browser_closes_site_guide_before_paginating(self, _sleep):
        settings = self.source["browser"]["pagination"]
        settings.update(response_path="/button-data", response_success_path="ok", response_success_value=True,
                        dismiss_overlay={"overlay_xpath": "//*[@id='guide']", "close_button_xpath": "//*[@id='close-guide']"})
        source = dict(self.source, url=self.source["url"] + "?guide=1")
        result = collect(source, self.site.root / "guide")
        self.assertEqual(result.errors, [])
        self.assertEqual((result.status, result.pages, set(result.jobs)), ("complete", 2, {"1", "2", "3"}))
        trace = json.loads((self.site.root / "guide/page-002.pagination.json").read_text(encoding="utf-8"))
        self.assertTrue(trace["overlay_dismissed"])
        self.assertEqual((trace["page_before"], trace["page_after"], trace["rows_repeated"]), ("1", "2", 0))


def listing(page, total, ids, links=None):
    if links is None:
        links = ids
    cards = "".join(f'<article data-id="{key}"><a href="/j/{link}">Job {key}</a></article>'
                    for key, link in zip(ids, links))
    return f'<div id="current">{page}</div><div id="total">{total}</div>{cards}'.encode()
