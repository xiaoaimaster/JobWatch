from collections import Counter
from contextlib import redirect_stdout
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.request import OpenerDirector

from jobwatch.cli import main


TITLES = {"healthy": "平台开发", "timeout": "数据工程", "empty": "测试开发", "wrong": "产品运营"}


class RecoveryTests(TestCase):
    def setUp(self):
        self.phase = "baseline"
        self.hits = Counter()
        self.release_timeouts = Event()
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                case.hits[self.path] += 1
                content_type = "text/html"
                if self.path == "/robots.txt":
                    content_type = "text/plain"
                    body = "User-agent: *\nAllow: /\n"
                elif self.path == "/jobs":
                    entries = "".join(f'<article data-id="{key}"><a href="/detail/{key}">{title}</a></article>'
                                      for key, title in TITLES.items())
                    body = f'<span id="total">{len(TITLES)}</span>{entries}'
                else:
                    job_id = self.path.removeprefix("/detail/")
                    if job_id not in TITLES:
                        self.send_error(404)
                        return
                    title = TITLES[job_id]
                    duties = "维护内部工具。"
                    requirements = "熟悉 Python。"
                    if case.phase == "failed":
                        if job_id == "timeout":
                            case.release_timeouts.wait(5)
                            return
                        if job_id == "empty":
                            duties = requirements = ""
                        if job_id == "wrong":
                            title = "另一个岗位"
                    if case.phase == "recovered" and job_id == "healthy":
                        requirements = "熟悉 Python 和 SQL。"
                    body = f'<h1>{title}</h1><p id="duties">{duties}</p><p id="requirements">{requirements}</p>'
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.addCleanup(self.release_timeouts.set)
        self.base = "http:" f"//127.0.0.1:{server.server_port}"
        root = Path(directory.name)
        (root / "sources.json").write_text(json.dumps([{
            "id": "fixture", "company": "测试公司", "adapter": "html_http", "url": self.base + "/jobs",
            "access_reviewed": True, "single_page": True, "items_xpath": "//article",
            "total_xpath": "string(//*[@id='total'])",
            "fields": {"id": "./@data-id", "url": "./a/@href", "title": "./a/text()"},
            "detail_title_xpath": "//h1/text()",
            "detail_fields": {"duties": "//*[@id='duties']/text()", "requirements": "//*[@id='requirements']/text()"},
        }]), encoding="utf-8")
        self.config = root / "watch.json"
        self.config.write_text(json.dumps({
            "registry": "sources.json", "tasks": {"company": {"mode": "company", "sources": ["fixture"]}},
        }), encoding="utf-8")

    def run_collection(self, status, success, failed):
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["run", "--config", str(self.config), "--task", "company"])
        summary = json.loads(output.getvalue())
        folder = Path(summary["report"]).parent
        self.assertEqual(exit_code, 2 if failed else 0, (folder / "report.md").read_text(encoding="utf-8"))
        self.assertEqual(summary["sources"], {"fixture": status})
        self.assertEqual(summary["collection"]["fixture"], {"pages": 1, "collected": 4, "declared_total": 4})
        self.assertEqual(summary["details"]["fixture"], {"success": success, "failed": failed, "skipped": 0})
        for name in ("cleaned.csv", "jobs.csv"):
            with (folder / name).open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), len(TITLES))
            self.assertEqual({row["id"]: row["title"] for row in rows}, TITLES)
            for row in rows:
                job_id = row["id"]
                self.assertEqual(row["url"], self.base + f"/detail/{job_id}")
                if failed and job_id != "healthy":
                    self.assertEqual((row["duties"], row["requirements"]), ("", ""))
                else:
                    self.assertEqual(row["duties"], "维护内部工具。")
                    expected = "熟悉 Python 和 SQL。" if self.phase == "recovered" and job_id == "healthy" else "熟悉 Python。"
                    self.assertEqual(row["requirements"], expected)
        return folder, json.loads((folder / "changes.json").read_text(encoding="utf-8"))

    @patch("jobwatch.sources.WebSource.throttle")
    def test_restored_details_are_compared_with_last_complete_run(self, _throttle):
        self.run_collection("complete", 4, 0)
        original_open = OpenerDirector.open

        def shorten_timeout(opener, request, *, timeout):
            if request.full_url == self.base + "/detail/timeout":
                timeout = 0.3
            return original_open(opener, request, timeout=timeout)

        self.phase = "failed"
        with patch.object(OpenerDirector, "open", shorten_timeout):
            folder, differences = self.run_collection("partial", 1, 3)
        self.release_timeouts.set()
        self.assertEqual(self.hits["/detail/timeout"], 3)
        report = (folder / "report.md").read_text(encoding="utf-8")
        self.assertRegex(report, r"岗位 timeout[^\n]*timed out")
        self.assertRegex(report, r"岗位 empty[^\n]*详情字段均未命中")
        self.assertRegex(report, r"岗位 wrong[^\n]*详情页标题与列表不一致")
        self.assertEqual({row["id"]: row["change"] for row in differences},
                         {key: "本次无法判断" for key in ("timeout", "empty", "wrong")})

        self.phase = "recovered"
        _, differences = self.run_collection("complete", 4, 0)
        self.assertEqual([(row["id"], row["change"], row["fields"]) for row in differences],
                         [("healthy", "修改", "requirements")])
        self.assertEqual(differences[0]["before"]["requirements"], "熟悉 Python。")
        self.assertEqual(differences[0]["after"]["requirements"], "熟悉 Python 和 SQL。")
