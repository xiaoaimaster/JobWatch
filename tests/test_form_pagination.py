from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs

from jobwatch.config import validate_source
from jobwatch.sources import collect


class FormPaginationTests(TestCase):
    @patch("jobwatch.sources.WebSource.throttle")
    def test_queries_each_page_and_keeps_verified_pages_after_a_bad_response(self, _throttle):
        requests = []
        pages = {
            "1": {"status": "ok", "data": {"total": "3", "page": 1, "jobs": [
                {"id": "a", "name": "工程师", "body": "开发工具", "requirements": "Python"},
                {"id": "b", "name": "研究员", "body": "验证方案", "requirements": "实验设计"},
            ]}},
            "2": {"status": "ok", "data": {"total": "3", "page": 2, "jobs": [
                {"id": "c", "name": "设计师", "body": "设计界面", "requirements": "交互设计"},
            ]}},
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, body):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/robots.txt":
                    requests.append(("GET", self.path))
                    self.respond(b"User-agent: *\nAllow: /\n")
                else:
                    self.send_error(405)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                form = parse_qs(body, keep_blank_values=True)
                requests.append(("POST", self.path, form, self.headers["Origin"], self.headers["Referer"]))
                self.respond(json.dumps(pages[form["curPage"][0]]).encode())

        with TemporaryDirectory() as directory, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                root = Path(directory)
                base = f"http:" f"//127.0.0.1:{server.server_port}"
                source = validate_source({
                    "id": "fixture", "company": "测试公司", "adapter": "json_http",
                    "url": base + "/query", "careers_url": base + "/careers", "access_reviewed": True,
                    "items_path": "data.jobs", "total_path": "data.total",
                    "pagination": {"page_param": "curPage", "size_param": "pageSize", "page_size": 2},
                    "query_form": {"fields": {"search": ""}, "success_path": "status",
                                   "success_value": "ok", "page_path": "data.page"},
                    "job_url_template": "/jobs/{id}",
                    "fields": {"id": "id", "title": "name", "duties": "body", "requirements": "requirements"},
                }, root)
                result = collect(source, root / "complete")
                self.assertEqual((result.status, result.pages, result.declared_total), ("complete", 2, 3))
                self.assertEqual(list(result.jobs), ["a", "b", "c"])
                self.assertEqual(result.jobs["c"]["url"], base + "/jobs/c")
                self.assertEqual(result.jobs["c"]["requirements"], "交互设计")
                self.assertEqual(requests, [
                    ("GET", "/robots.txt"),
                    ("POST", "/query", {"search": [""], "curPage": ["1"], "pageSize": ["2"]}, base, base + "/careers"),
                    ("POST", "/query", {"search": [""], "curPage": ["2"], "pageSize": ["2"]}, base, base + "/careers"),
                ])
                payload = pages["2"]
                for bad, message in (
                    ({"status": "fail"}, "业务失败"),
                    (pages["1"], "未返回第 2 页"),
                    ({"status": "ok", "data": dict(payload["data"], jobs=[pages["1"]["data"]["jobs"][0]])}, "重复"),
                ):
                    with self.subTest(message=message):
                        pages["2"] = bad
                        result = collect(source, root / "incomplete")
                        self.assertEqual((result.status, result.pages, result.declared_total), ("partial", 1, 3))
                        self.assertEqual(list(result.jobs), ["a", "b"])
                        self.assertIn(message, result.errors[0])
                with self.assertRaisesRegex(ValueError, "同源"):
                    validate_source(dict(source, careers_url="https:" "//other.example/careers"), root)
            finally:
                server.shutdown()
                thread.join()
