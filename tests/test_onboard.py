from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch

from jobwatch.cli import main, run_watch
from jobwatch.common import read_json, write_json
from jobwatch.discover import discover
from jobwatch.onboard import lead_entries, onboard, provider_profile
from jobwatch.sources import page_records


class CareersHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            body = '<a href="/careers">Careers</a>'
        elif self.path == "/robots.txt":
            body = "User-agent: *\nDisallow: /blocked\n"
        elif self.path in ("/careers", "/page-two"):
            number = 1 if self.path == "/careers" else 2
            body = f"""<article itemscope itemtype="https:{'//'}schema.org/JobPosting">
              <a itemprop="url" href="/jobs/{number}"><h2 itemprop="title">Agent 开发 {number}</h2></a>
              <p itemprop="description">岗位职责\uff1a开发工作流</p>
            </article>"""
            if number == 1:
                body += '<a rel="next" href="/page-two">Next</a>'
        elif self.path == "/embedded":
            body = '<script src="https:' '//boards.greenhouse' '.io/embed/job_board/js?for=demo"></script>'
        else:
            body = '<a href="https:' '//example.invalid/careers">Other site</a>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8" if self.path != "/robots.txt" else "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())


class OnboardTests(TestCase):
    def setUp(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), CareersHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.base = "http:" f"//127.0.0.1:{server.server_port}"

    @patch("jobwatch.sources.time.sleep")
    def test_batch_to_runnable_config_and_idempotent_registry(self, _sleep):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            targets = (
                ("星桥", self.base, "external"),
                ("未知企业", self.base + "/unknown", "external"),
                ("限制访问", self.base + "/blocked", "external"),
                ("站内线索", self.base + "/ignored", "nowcoder"),
            )
            leads = root / "leads.json"
            evidence = "https:" "//www.nowcoder" ".com/jobs/school/schedule"
            write_json(leads, dict(pages=[dict(source=evidence, companies=[
                dict(company=name, url=url, link_type=kind) for name, url, kind in targets
            ])]))
            registry = root / "existing.json"
            prior = dict(id="existing", company="本地样例", adapter="json_file", path="keep.json")
            write_json(registry, [prior])
            with redirect_stdout(StringIO()) as stdout:
                code = main(["onboard", "--from-leads", str(leads), "--access-reviewed",
                             "--registry", str(registry), "--output", temp])
            self.assertEqual(code, 2)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["ready"], 1)
            self.assertEqual([row["status"] for row in result["results"]], ["ready", "needs_adapter", "access_failed"])
            match = result["results"][0]
            self.assertEqual((match["pages"], match["jobs"], match["jobs_with_body"]), (2, 2, 2))
            self.assertEqual(result["registry_update"]["added"], [match["task"]])
            actual = run_watch(result["watch_config"], match["task"])
            self.assertEqual((actual["candidates"], actual["sources"][match["task"]]), (2, "complete"))
            saved = read_json(registry)
            self.assertEqual(saved[0], prior)
            self.assertEqual(saved[1]["discovered_via"], evidence)
            again = onboard([self.base], root, company="星桥", registry_path=registry)
            self.assertEqual(again["registry_update"]["existing"], [match["task"]])
            self.assertEqual(read_json(registry), saved)

    @patch("jobwatch.sources.time.sleep")
    @patch("jobwatch.sources.WebSource.fetch")
    def test_greenhouse_embed_and_title_detail_validation(self, fetch, _sleep):
        def response(url, path):
            if url.endswith("/robots.txt"):
                return b"User-agent: *\nDisallow:\n"
            if url == self.base + "/embedded":
                return b'<script src="https:' b'//boards.greenhouse' b'.io/embed/job_board/js?for=demo"></script>'
            if url.endswith("/jobs"):
                return json.dumps({"jobs": [{"id": 123, "title": "Developer", "absolute_url":
                                              "https:" "//job-boards.greenhouse" ".io/demo/jobs/123"}],
                                   "meta": {"total": 1}}).encode()
            if url.endswith("/jobs/123"):
                return json.dumps({"id": 456, "content": "wrong posting"}).encode()
            self.fail(f"unexpected request {url}")

        fetch.side_effect = response
        with TemporaryDirectory() as temp:
            result = onboard([self.base + "/embedded"], temp)
        self.assertEqual(result["ready"], 0)
        self.assertNotIn("watch_config", result)
        self.assertEqual(result["results"][0]["status"], "trial_failed")
        self.assertIn("详情记录 ID 与列表不一致", "".join(result["results"][0]["notes"]))

    def test_supported_api_profiles_and_excludes_unlisted_ashby_jobs(self):
        _, _, lever = provider_profile("https:" "//jobs.eu.lever" ".co/example")
        self.assertEqual(lever["url"], "https:" "//api.eu.lever" ".co/v0/postings/example?mode=json")
        self.assertEqual(lever["pagination"]["offset_param"], "skip")
        _, _, board = provider_profile("https:" "//jobs.ashbyhq" ".com/example")
        self.assertEqual(board["detail_id_path"], "posting.id")
        _, _, api = provider_profile("https:" "//api.ashbyhq" ".com/posting-api/job-board/example")
        payload = json.dumps({"jobs": [{"isListed": False}, {"isListed": True}]}).encode()
        self.assertEqual(page_records(payload, dict(adapter="json_http", **api))[0], [{"isListed": True}])
        self.assertIsNone(provider_profile("https:" "//jobs.lever.co.example.invalid/example"))

    @patch("jobwatch.sources.WebSource.get")
    def test_enterprise_link_falls_back_to_canonical_and_deduplicates_baidu_board(self, get):
        url = "https:" "//www.nowcoder" ".com/enterprise/139"
        campus = "https:" "//talent.baidu" ".com/jobs/list?recruitType=GRADUATE"
        data = {"store": {"enterprise": {"enterpriseInfo": {
            "companyId": 139, "name": "百度", "officalEncodeUrl": "https:" "//talent.baidu" ".com/jobs/",
            "buttonInfo": {"showButton": True, "name": "官网投递", "url": campus},
        }}}}
        get.side_effect = [b"<html></html>", ("<script>window.__INITIAL_STATE__=" +
                                             json.dumps(data) + "</script>").encode()]
        with TemporaryDirectory() as temp:
            root = Path(temp)
            result = discover([url + "?page=1"], root, companies=["百度"])
            self.assertEqual(result["pages"][0]["retrieved_url"], url)
            self.assertEqual(lead_entries(root / "leads.json"), [("百度", campus, url)])
        _, _, mapping = provider_profile(campus)
        self.assertEqual(mapping["query_form"]["fields"]["recruitType"], "GRADUATE")
        self.assertIsNone(provider_profile("https:" "//talent.baidu" ".com/jobs/list?recruitType=INTERN"))
