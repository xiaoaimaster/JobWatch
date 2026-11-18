from contextlib import closing
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from jobwatch import store
from jobwatch.ai import AIError, Responses, validate_analysis
from jobwatch.cli import demo, finish, new_run, run_watch
from jobwatch.config import AI_DEFAULTS, load_settings, validate_source
from jobwatch.report import changes
from jobwatch.sources import collect

class OfflineTests(TestCase):
    def test_both_modes_and_two_rounds(self):
        with TemporaryDirectory() as directory:
            runs = demo(Path(directory))["runs"]
            self.assertEqual(len(runs), 4)
            self.assertEqual([len(run["sources"]) for run in runs], [2, 1, 2, 1])
            self.assertEqual([run["cleaned"] for run in runs], [4, 3, 4, 2])
            folder = Path(runs[2]["report"]).parent
            diff = json.loads((folder / "changes.json").read_text(encoding="utf-8"))
            self.assertEqual({row["id"]: row["change"] for row in diff},
                             {"C2": "新发现", "S1": "修改", "S2": "本次未检出"})
            self.assertTrue((folder / "raw" / "cloud" / "input.json").is_file())
            self.assertTrue((folder / "cleaned.csv").is_file())
            self.assertTrue(all(run["ai_validated"] == 0 for run in runs))

    def test_long_description_survives_csv_roundtrip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            description = "= " + ("岗位描述" * 33000)
            limit = csv.field_size_limit()
            (root / "jobs.json").write_text(json.dumps([{
                "id": "long", "title": "工程师", "duties": description,
            }]), encoding="utf-8")
            (root / "sources.json").write_text(json.dumps([{
                "id": "local", "company": "测试", "adapter": "json_file", "path": "jobs.json",
            }]), encoding="utf-8")
            cfg = root / "watch.json"
            cfg.write_text(json.dumps({
                "registry": "sources.json",
                "tasks": {"company": {"mode": "company", "sources": ["local"]}},
            }), encoding="utf-8")
            result = run_watch(cfg, task="company")
            self.assertEqual((result["sources"]["local"], result["ai_eligible"]), ("complete", 1))
            folder = Path(result["report"]).parent
            self.assertIn("'" + description, (folder / "cleaned.csv").read_text(encoding="utf-8-sig"))
            data = json.loads((folder / "ai" / "input.json").read_text(encoding="utf-8"))
            self.assertEqual(data[0]["duties"], description)
            self.assertEqual(csv.field_size_limit(), limit)

    def test_failure_budget_resume_cache(self):
        with TemporaryDirectory() as directory:
            example = demo(Path(directory))
            output, sources, tasks, _ = load_settings(Path(example["sample"]) / "watch.json")
            settings = dict(AI_DEFAULTS, batch_size=1, max_requests=1, model="fixture")
            with closing(store.connect(output)) as db, patch.dict("os.environ", {"OPENAI_" "API_KEY": "fixture"}):
                run_id = new_run(db, output, "ai-apps", tasks["ai-apps"], sources, None)
                ai = Responses(settings)
                ai.opener = Mock(side_effect=AssertionError("unexpected live API"))
                ai.opener.open.side_effect = model_response
                summary = finish(db, output, run_id, ai)
                self.assertEqual(summary["ai_validated"], 1)
                self.assertEqual(summary["ai_requests"], 1)
                self.assertIn("上限", summary["ai_message"])
                retry = Responses(dict(settings, max_requests=5))
                retry.opener = ai.opener
                second = finish(db, output, run_id, retry)
                self.assertEqual(second["ai_validated"], 3)
                self.assertEqual(second["ai_requests"], 2)
                third_run = new_run(db, output, "ai-apps", tasks["ai-apps"], sources, None)
                cached = Responses(settings)
                cached.opener = Mock()
                cached.opener.open.side_effect = AssertionError("cache should serve all rows")
                third = finish(db, output, third_run, cached)
                self.assertEqual((third["ai_validated"], third["ai_requests"]), (3, 0))

    def test_ai_recovery_requires_exact_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "jobs.json"
            raw.write_text(json.dumps([{"id": 1, "new_title": "AI 应用开发"}]), encoding="utf-8")
            source = validate_source(dict(id="a", company="测试", adapter="json_file", path=raw.name,
                                          fields={"id": "id", "title": "old_title"},
                                          recoverable_fields=["title"]), root)
            ai = Mock()
            ai.request.return_value = {"title": "AI 应用开发"}
            recovered = collect(source, root / "raw", ai)
            self.assertEqual((recovered.status, recovered.recovered_fields), ("ai_assisted", {"1": ["title"]}))
            self.assertEqual(recovered.jobs["1"]["title"], "AI 应用开发")
            ai.request.return_value = {"title": "不存在的名称"}
            failure = collect(source, root / "bad", ai)
            self.assertEqual(failure.status, "partial")
            self.assertEqual(failure.jobs, {})

    def test_rejects_wrong_job_evidence(self):
        rows = [{"title": "工程师", "duties": "测试", "requirements": "", "bonus": ""}]
        with self.assertRaises(AIError):
            validate_analysis({"results": [{"index": 0, "verdict": "match", "evidence_field": "duties",
                                            "evidence": "编造项", "facts": []}]}, rows)

    def test_http_auth_failure_has_no_secret(self):
        with patch.dict("os.environ", {"OPENAI_" "API_KEY": "sensitive-fixture"}):
            ai = Responses(dict(AI_DEFAULTS, model="fixture"))
            ai.opener = Mock()
            ai.opener.open.side_effect = HTTPError("https:" "//example.invalid", 401, "sensitive-fixture", {}, None)
            with self.assertRaisesRegex(AIError, "^AI API HTTP 401$"):
                ai.request("extract", {}, {})
            self.assertEqual(ai.requests, 1)


def model_response(request, timeout):
    payload = json.loads(request.data)
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["store"] is False
    records = json.loads(payload["input"])["jobs"]
    result = {"results": [dict(index=index, verdict="match", evidence_field="duties",
                               evidence=job["duties"], facts=[]) for index, job in enumerate(records)]}
    body = {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]}
    return BytesIO(json.dumps(body).encode())


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        code = 200
        content_type = "text/html"
        if url.path == "/robots.txt":
            content_type = "text/plain"
            body = "User-agent: *\nDisallow: /blocked\n"
        elif url.path == "/api":
            content_type = "application/json"
            page = parse_qs(url.query).get("p", ["1"])[0]
            jobs = [{"id": page, "title": "AI 应用开发", "url": f"/detail/{page}"}] if page in ("1", "2") else []
            body = json.dumps({"data": {"items": jobs, "total": 2}})
        elif url.path.startswith("/detail/"):
            content_type = "application/json"
            body = json.dumps({"requirements": "熟悉 Python"}, ensure_ascii=False)
        elif url.path == "/careers":
            body = '<article><a href="/open/job">AI 应用开发</a><p>部署 Agent</p></article><a rel="next" href="/page2">下一页</a>'
        elif url.path == "/page2":
            body = '<article><a href="/open/other">大模型应用开发</a></article>'
        elif url.path == "/ats-jobs":
            body = '<article data-id="a"><a href="/ats/a">Dynamic job</a></article>'
        elif url.path == "/ats/a":
            body = """<html><body><div class="jobDetail">
              <span class="job-title">Dynamic job</span>
              <div class="block-title">职位描述</div><div class="block-content" id="duties"></div>
              <div class="block-title">职位要求</div><div class="block-content" id="requirements"></div>
              </div><script>
              fetch('/ats-detail-data').then(response => response.json()).then(data => {
                document.querySelector('#duties').textContent = data.duties;
                document.querySelector('#requirements').textContent = data.requirements;
              });</script></body></html>"""
        elif url.path == "/ats-detail-data":
            content_type = "application/json"
            body = json.dumps({"duties": "Develop the product.", "requirements": "Experience with Python."})
        elif url.path == "/dynamic":
            body = """<html><body><script>
            fetch('/dynamic-data').then(x => x.text()).then(label => {
              document.body.insertAdjacentHTML('beforeend', '<article><a href="/j/1">' + label + '</a></article>');
            });</script></body></html>"""
        elif url.path == "/button-jobs":
            guide = ""
            if parse_qs(url.query).get("guide") == ["1"]:
                guide = ('<div id="guide" style="position:fixed;inset:0;z-index:10;background:#ddd">'
                         '<button id="close-guide" type="button" onclick="this.parentElement.hidden=true">'
                         'Close guide</button></div>')
            body = """<html><body>
              <div id="current">1</div><div id="total">0</div><div id="jobs"></div>
              <button id="next" type="button">Next</button>
              <script>
              async function load(page) {
                  const data = await (await fetch('/button-data?p=' + page)).json();
                  document.querySelector('#current').textContent = page;
                  document.querySelector('#total').textContent = data.total;
                  document.querySelector('#jobs').innerHTML = data.ids.map(id =>
                    '<article data-id="' + id + '"><a href="/j/' + id + '">Job ' + id + '</a></article>'
                  ).join('');
              }
              document.querySelector('#next').onclick = () => load(2);
              load(1);
              </script>""" + guide + "</body></html>"
        elif url.path == "/button-data":
            content_type = "application/json"
            page = parse_qs(url.query).get("p", ["1"])[0]
            body = json.dumps({"ok": True, "total": 3, "ids": [1, 2] if page == "1" else [3]})
        elif url.path == "/dynamic-data":
            content_type = "text/plain"
            body = "Dynamic jobs"
        elif url.path == "/broken":
            body = "<html><h1>服务错误页</h1></html>"
        else:
            code, body = 404, ""
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


class SiteTests(TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.base = "http:" f"//127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def source(self, path, adapter, **options):
        cfg = dict(id="fixture", company="测试公司", adapter=adapter, url=self.base + path,
                   access_reviewed=True, max_pages=3, **options)
        return validate_source(cfg, self.root)

    @patch("jobwatch.sources.time.sleep")
    def test_json_pagination_detail_and_page_limit(self, sleep):
        source = self.source("/api", "json_http", items_path="data.items", pagination={"page_param": "p"},
                             fields={"id": "id", "title": "title", "url": "url"},
                             detail_fields={"requirements": "requirements"})
        full = collect(source, self.root / "full")
        self.assertEqual(full.status, "complete")
        self.assertEqual(full.pages, 3)
        self.assertEqual(full.jobs["2"]["requirements"], "熟悉 Python")
        with_total = collect(dict(source, total_path="data.total"), self.root / "total")
        self.assertEqual((with_total.status, with_total.pages), ("complete", 2))
        partial = collect(dict(source, max_pages=1), self.root / "partial")
        self.assertEqual(partial.status, "partial")
        task = dict(mode="company", cities=[], types=[], pinned_urls=[])
        self.assertEqual([item["change"] for item in changes(task, full.jobs, partial.jobs, False)],
                         ["本次无法判断"])

    @patch("jobwatch.sources.time.sleep")
    def test_html_pagination_missing_selector_and_robots(self, sleep):
        source = self.source("/careers", "html_http", items_xpath="//article",
                             fields={"title": "./a/text()", "url": "./a/@href", "duties": "./p/text()"},
                             next_xpath="//a[@rel='next']/@href")
        complete = collect(source, self.root / "html")
        self.assertEqual(complete.status, "complete")
        self.assertIn(self.base + "/open/job", complete.jobs)
        self.assertEqual(complete.jobs[self.base + "/open/job"]["title"], "AI 应用开发")
        broken = collect(dict(source, url=self.base + "/broken"), self.root / "broken")
        self.assertEqual(broken.status, "failed")
        blocked = collect(dict(source, url=self.base + "/blocked"), self.root / "blocked")
        self.assertEqual(blocked.status, "failed")
        self.assertIn("robots.txt 禁止", blocked.errors[0])
