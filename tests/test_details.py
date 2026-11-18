import os
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

from jobwatch.config import ATS_READY, validate_source
from jobwatch.sources import collect, parse_html
import test_watch


def detail(title, duties="开发接口。", requirements="熟悉 Python。"):
    return f"""<html><body><div class="jobDetail__fixture jobDetail">
      <div class="job-header"><span class="job-title">{title}</span></div>
      <div class="job-info">广州</div>
      <div class="block-title">职位描述</div><div class="block-content">{duties}</div>
      <div class="block-title">职位要求</div><div class="block-content">{requirements}</div>
      <div class="apply-block"><button>投递</button></div>
      </div></body></html>""".encode()


class AtsDetailTests(TestCase):
    def setUp(self):
        self.site = test_watch.SiteTests()
        self.site.setUp()
        self.addCleanup(self.site.doCleanups)
        self.source = self.site.source("/ats-jobs", "html_http", items_xpath="//article",
                                       fields={"id": "./@data-id", "title": "./a/text()", "url": "./a/@href"},
                                       single_page=True, browser={"timeout_seconds": 5},
                                       detail_profile="ats_job_detail")

    def test_no_empty_placeholders_are_ready(self):
        self.assertTrue(parse_html(detail("工程师")).xpath(ATS_READY))
        self.assertFalse(parse_html(detail("工程师", requirements=" ")).xpath(ATS_READY))
        self.assertFalse(parse_html(b"<html><body>Loading</body></html>").xpath(ATS_READY))

    def test_detail_limit_counts_and_title_check(self):
        listing = b'''<article data-id="a"><a href="/ats/a">Job A</a></article>
                      <article data-id="b"><a href="/ats/b">Job B</a></article>
                      <article data-id="c"><a href="/ats/c">Job C</a></article>'''
        with patch("jobwatch.sources.IsolatedBrowser", return_value=MagicMock()) as mocked:
            browser = mocked.return_value
            browser.close.return_value = ""
            browser.get.side_effect = [listing, detail("Job A"), detail("Another job")]
            result = collect(dict(self.source, max_details=2), self.site.root / "sample")
        self.assertEqual(result.details, {"success": 1, "failed": 1, "skipped": 1})
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.jobs["a"]["duties"], "开发接口。")
        self.assertEqual(result.jobs["a"]["requirements"], "熟悉 Python。")
        self.assertEqual(result.jobs["b"]["duties"], "")
        self.assertIn("标题与列表不一致", result.errors[-1])
        self.assertEqual(browser.get.call_args_list[1].kwargs, {"detail": True})

    def test_profile_rejects_wrong_adapter(self):
        with self.assertRaisesRegex(ValueError, "需要 HTML 浏览器"):
            validate_source(dict(self.source, adapter="json_http", browser=None), self.site.root)

    @skipUnless(os.environ.get("JOBWATCH_TEST_BROWSER") == "1", "需要可启动的 Chromium")
    @patch("jobwatch.sources.time.sleep")
    def test_real_browser_waits_for_detail_body(self, _sleep):
        result = collect(self.source, self.site.root / "rendered")
        self.assertEqual(result.errors, [])
        self.assertEqual(result.details, {"success": 1, "failed": 0, "skipped": 0})
        self.assertEqual(result.jobs["a"]["duties"], "Develop the product.")
        self.assertEqual(result.jobs["a"]["requirements"], "Experience with Python.")
