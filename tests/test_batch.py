from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit

import json
import os
from tempfile import TemporaryDirectory

from jobwatch.browser import BrowserPages, launch_chromium
from jobwatch.cleaning import text
from jobwatch.discover import SCHEDULE_URL, career_links, discover, schedule_leads
from jobwatch.sources import collect, pagination_url
import test_watch


def nowcoder_state(**state):
    return f"<script>window.__INITIAL_STATE__={json.dumps(state)};</script>".encode()


class BatchTests(TestCase):
    def test_escaped_and_raw_html(self):
        self.assertEqual(text("&lt;p&gt;熟悉 Python&lt;/p&gt;&lt;p&gt;A &amp;amp; B&lt;/p&gt;"),
                         "熟悉 Python\nA & B")
        self.assertEqual(text("<p>C++ &lt; 11</p><script>ignore</script><li>SQL</li>"), "C++ < 11\nSQL")

    def test_offset_keeps_repeated_filters(self):
        url = pagination_url("https://example.test/jobs?city=Tokyo&city=Kyoto&offset=9",
                             {"offset_param": "offset", "size_param": "limit", "page_size": 4}, 2)
        self.assertEqual(parse_qsl(urlsplit(url).query),
                         [("city", "Tokyo"), ("city", "Kyoto"), ("offset", "8"), ("limit", "4")])

    def test_discovery_splits_text_and_removes_tracking(self):
        page = '''<html><body>
        https://amazon.jobs/en/jobs/123/software-engineerSoftware will be truncated
        <script>const sources = "https%3A%2F%2Fcampus.example.test%2F%23%2Fjobs%3Fcode%3Dsecret";</script>
        <a href="https://jobs.example.test/referral/campus/share?token=abc">入口</a>
        <a href="https://www.nowcoder.com/jobs/detail/222">站内</a>
        </body></html>'''.encode()
        self.assertEqual(career_links(page), [
            "https://amazon.jobs/en/jobs/123/software-engineer",
            "https://campus.example.test/#/jobs",
            "https://jobs.example.test/",
        ])

    @patch("jobwatch.discover.WebSource.get")
    def test_discovery_accepts_schedule_and_preserves_company_links(self, get):
        state = {"app": {"137": {"scheduleData": {
            "currentPage": 1, "totalPage": 5, "totalCount": 20, "datas": [
                {"name": "招聘板", "companyId": 1,
                 "customWangshenLink": "https://app.mokahr.com/campus-recruitment/example?utm_source=fixture&lang=zh"},
                {"name": "站内企业", "companyId": "2", "customWangshenLink": "/enterprise/2"},
                {"name": "公告企业", "companyId": 3, "customWangshenLink": "https://mp.weixin.qq.com/s/example"},
                {"name": "缺链接", "companyId": 4},
                {"name": "异常链接", "companyId": 5, "customWangshenLink": "javascript:alert(1)"},
                {"name": "片段路由", "companyId": 6,
                 "customWangshenLink": "https://campus.example.com/#/jobs?code=ref&city=sh"},
            ],
        }}}}
        raw = f"<script>window.__INITIAL_STATE__={json.dumps(state)};</script>".encode()
        get.side_effect = [raw, b'<a href="https://jobs.example.com/jobs/7">Job</a>']
        legacy = "https://www.nowcoder.com/school/schedule?typeId=2837"
        post = "https://www.nowcoder.com/discuss/123"
        with TemporaryDirectory() as temp:
            output = Path(temp)
            result = discover([legacy, post], output)
            self.assertEqual(json.loads((output / "leads.json").read_text(encoding="utf-8")), result)
        self.assertEqual([c.args[0] for c in get.call_args_list], [SCHEDULE_URL + "?typeId=2837", post])
        self.assertEqual(result["candidate_count"], 3)
        schedule, discussion = result["pages"]
        self.assertEqual(schedule["schedule"], {"page": 1, "total_pages": 5, "total_companies": 20})
        self.assertEqual([c["link_type"] for c in schedule["companies"]],
                         ["external", "nowcoder", "announcement", "missing", "missing", "external"])
        self.assertEqual(schedule["companies"][0], {"company": "招聘板", "company_id": "1", "link_type": "external",
                                                    "url": "https://app.mokahr.com/campus-recruitment/example?lang=zh"})
        self.assertEqual(schedule["candidates"], ["https://app.mokahr.com/campus-recruitment/example?lang=zh",
                                                   "https://campus.example.com/#/jobs?city=sh"])
        self.assertEqual(discussion, {"source": post, "status": "found", "candidates": ["https://jobs.example.com/jobs/7"]})

    def test_schedule_distinguishes_structure_change_from_empty_results(self):
        for data in (b'<html><p>Login</p></html>',
                     b'<script>window.__INITIAL_STATE__={"app":{"0":{"scheduleData":{"totalCount":2,"datas":[]}}}};</script>'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                schedule_leads(data)
        empty = b'<script>window.__INITIAL_STATE__={"app":{"0":{"scheduleData":{"totalCount":0,"datas":[]}}}};</script>'
        self.assertEqual(schedule_leads(empty)["companies"], [])

    @patch("jobwatch.discover.WebSource.get")
    def test_company_discovery_filters_names_and_follows_matching_enterprise(self, get):
        schedule = nowcoder_state(app={"120": {"scheduleData": {"totalCount": 3, "datas": [
            {"name": "星桥软件", "companyId": 1, "customWangshenLink": "/enterprise/1"},
            {"name": "云岚系统", "companyId": 2, "customWangshenLink": "https://jobs.example.test/open"},
            {"name": "无关企业", "companyId": 3},
        ]}}})
        enterprise = nowcoder_state(store={"enterprise": {"enterpriseInfo": {
            "companyId": 1, "name": "星桥软件",
            "officalEncodeUrl": "/jump?url=https%3A%2F%2Fjobs.example.test%2Fcampus%3Fproject%3D4&entityId=0",
            "buttonInfo": {"showButton": True, "name": "官网投递",
                           "url": "https://jobs.example.test/search?recruitType=GRADUATE"},
        }}})
        get.side_effect = [schedule, enterprise, enterprise]
        with TemporaryDirectory() as temp:
            output = Path(temp)
            result = discover([], output, companies=["星桥", "云岚", "缺失"])
            self.assertEqual(result["scope"], "public_pages")
            self.assertEqual(result["candidate_count"], 3)
            self.assertEqual(result["company_matches"][0]["candidates"], [
                "https://jobs.example.test/search?recruitType=GRADUATE",
                "https://jobs.example.test/campus?project=4",
            ])
            self.assertEqual(result["company_matches"][1]["matched_queries"], ["云岚"])
            direct = discover(["https://www.nowcoder.com/enterprise/1"], output, companies=["星桥"])
            self.assertEqual(direct["candidate_count"], 2)
        self.assertEqual([call.args[0] for call in get.call_args_list], [
            SCHEDULE_URL, "https://www.nowcoder.com/enterprise/1", "https://www.nowcoder.com/enterprise/1",
        ])

    def test_chromium_prefers_playwright_and_only_falls_back_if_missing(self):
        chromium = Mock()
        with patch.dict(os.environ, {"JOBWATCH_CHROMIUM": ""}), patch(
            "jobwatch.browser.shutil.which", return_value="/usr/bin/chromium"
        ) as which:
            self.assertIs(launch_chromium(chromium, RuntimeError), chromium.launch.return_value)
            self.assertNotIn("executable_path", chromium.launch.call_args.kwargs)
            which.assert_not_called()
            chromium.launch.side_effect = [RuntimeError("Executable doesn't exist"), Mock()]
            launch_chromium(chromium, RuntimeError)
            self.assertEqual(chromium.launch.call_args.kwargs["executable_path"], "/usr/bin/chromium")

    def test_chromium_reports_restricted_socket(self):
        chromium = Mock()
        chromium.launch.side_effect = RuntimeError("socket() failed: Operation not permitted")
        with patch.dict(os.environ, {"JOBWATCH_CHROMIUM": "/custom/chromium"}):
            with self.assertRaisesRegex(ValueError, "Unix 套接字"):
                launch_chromium(chromium, RuntimeError)
        chromium.launch.assert_called_once_with(executable_path="/custom/chromium", proxy=None, timeout=12000)

    def test_browser_disallows_unconfigured_post_and_redirect(self):
        browser = BrowserPages({"url": "https://jobs.example.test/careers", "browser": {}})
        browser.error = RuntimeError
        browser.timeout = 500
        browser.site = Mock()
        browser.folder = Path(".")
        browser.assets = {}
        browser.failures = []
        route = Mock()
        route.request.resource_type = "xhr"
        route.request.url = "https://jobs.example.test/api"
        route.request.method = "POST"
        browser.route(route)
        route.abort.assert_called_once()
        route.fetch.assert_not_called()
        route.reset_mock()
        route.request.method = "GET"
        route.fetch.return_value.status = 302
        browser.route(route)
        route.fetch.assert_called_once_with(max_redirects=0, max_retries=0, timeout=500)
        route.abort.assert_called_once()
        route.fulfill.assert_not_called()

    def test_robots_is_checked_before_browser_start(self):
        browser = BrowserPages({"url": "https://jobs.example.test/careers", "browser": {}})
        site = Mock()
        site.check_access.side_effect = ValueError("robots.txt 禁止该路径")
        with patch.object(browser, "start") as start:
            with self.assertRaisesRegex(ValueError, "robots.txt"):
                browser.get(site, "https://jobs.example.test/disallowed", Path("unused.html"))
            start.assert_not_called()

    def test_asset_robots_error_is_reported_and_request_stays_blocked(self):
        browser = BrowserPages({"url": "https://jobs.example.test/careers", "browser": {}})
        browser.error = RuntimeError
        browser.timeout = 500
        asset = Mock()
        browser.assets = {("https", "assets.example.test", 443): asset}
        browser.failures = []
        route = Mock()
        route.request.resource_type = "script"
        route.request.url = "https://assets.example.test/app.js"
        route.request.method = "GET"
        asset.check_access.side_effect = HTTPError("https://assets.example.test/robots.txt", 400, "bad request", {}, None)
        with patch.object(Path, "mkdir"):
            browser.folder = Path(".")
            browser.route(route)
        self.assertIn("assets.example.test 的 robots.txt 检查失败（HTTP 400）", browser.diagnostics())
        route.abort.assert_called_once()
        route.fetch.assert_not_called()
        route.reset_mock()
        asset.check_access.side_effect = None
        route.fetch.return_value.status = 400
        browser.failures.clear()
        with patch.object(Path, "mkdir"):
            browser.route(route)
        self.assertEqual(browser.failures, ["script assets.example.test HTTP 400"])
        route.fulfill.assert_called_once_with(response=route.fetch.return_value)

    def test_pagination_counts_matching_requests_and_statuses(self):
        browser = BrowserPages({"url": "https://jobs.example.test/careers", "browser": {}})
        browser.error = RuntimeError
        browser.timeout = 500
        browser.folder, browser.site, browser.assets, browser.failures = Path("."), Mock(), {}, []
        trace = {"response_path": "/search", "requests_seen": 0, "http_statuses": set()}
        browser.pagination_trace = trace
        route = Mock()
        route.request.resource_type = "xhr"
        route.request.method = "GET"
        route.fetch.return_value.status = 200
        with patch.object(Path, "mkdir"):
            route.request.url = "https://jobs.example.test/other"
            browser.route(route)
            route.request.url = "https://jobs.example.test/search?offset=10"
            browser.route(route)
        self.assertEqual(trace, {"response_path": "/search", "requests_seen": 1, "http_statuses": {200}})


class CollectBatchTests(TestCase):
    def setUp(self):
        self.site = test_watch.SiteTests()
        self.site.setUp()
        self.addCleanup(self.site.doCleanups)

    @patch("jobwatch.sources.time.sleep")
    def test_browser_page_without_pagination_is_partial(self, _sleep):
        source = self.site.source("/careers", "html_http", items_xpath="//article",
                                  fields={"title": "./a/text()", "url": "./a/@href"}, browser={})
        with patch("jobwatch.sources.IsolatedBrowser", return_value=MagicMock()) as renderer:
            instance = renderer.return_value
            instance.close.return_value = ""
            instance.get.return_value = b'<article><a href="/j/1">Job</a></article>'
            result = collect(source, self.site.root / "out")
        self.assertEqual((result.status, len(result.jobs)), ("partial", 1))
        self.assertIn("分页规则", result.errors[0])

    @skipUnless(os.environ.get("JOBWATCH_TEST_BROWSER") == "1", "需要可启动的 Chromium")
    @patch("jobwatch.sources.time.sleep")
    def test_render_real_browser_and_fetch_public_data(self, _sleep):
        source = self.site.source("/dynamic", "html_http", items_xpath="//article",
                                  fields={"title": "./a/text()", "url": "./a/@href"},
                                  single_page=True, browser={"timeout_seconds": 5})
        result = collect(source, self.site.root / "rendered")
        self.assertEqual(result.errors, [])
        self.assertEqual(result.status, "complete")
        self.assertEqual(next(iter(result.jobs.values()))["title"], "Dynamic jobs")
