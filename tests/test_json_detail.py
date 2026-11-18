from contextlib import redirect_stdout
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch

from jobwatch.cli import main
from jobwatch.config import validate_source
from jobwatch.sources import collect, detail_url, page_records


SOURCE = {
    "id": "fixture", "company": "测试公司", "adapter": "json_http",
    "url": "https:" "//board.example.invalid/jobs?listing=1", "access_reviewed": True,
    "items_path": "jobs", "total_path": "total",
    "fields": {"id": "id", "title": "title", "url": "absolute_url"},
    "detail_url_template": "/jobs/{id}", "detail_id_path": "record.id",
    "detail_fields": {"duties": "record.content"},
}


class JsonDetailTests(TestCase):
    def test_static_html_with_published_json_supports_list_and_detail(self):
        source = dict(SOURCE, json_script={"xpath": "//script/text()", "prefix": "window.jobBoard ="},
                      fields={"id": "id", "title": "title"}, detail_url_template="/jobs/{id}")
        prefix = source["json_script"]["prefix"]
        listing = {"jobs": [{"id": "j7", "title": "工程师"}], "total": 1}
        detail = {"record": {"id": "j7", "content": "维护工具。"}}
        shell = '<html><script>window.other = {{}};</script><script> {} {}; </script></html>'
        encoded = [shell.format(prefix, json.dumps(body)).encode() for body in (listing, detail)]
        with TemporaryDirectory() as directory, patch("jobwatch.sources.WebSource.get", side_effect=encoded) as get:
            root = Path(directory)
            result = collect(validate_source(source, root), root / "output")
            self.assertEqual(result.status, "complete")
            self.assertEqual(result.jobs["j7"]["url"], "https:" "//board.example.invalid/jobs/j7")
            self.assertEqual(result.jobs["j7"]["duties"], "维护工具。")
            self.assertEqual(get.call_args.args, ("https:" "//board.example.invalid/jobs/j7", root / "output/detail-00001.html"))
            with self.assertRaisesRegex(ValueError, "唯一的内嵌 JSON"):
                page_records(f"<script>{prefix} {{}}</script><script>{prefix} {{}}</script>".encode(), source)

    def test_template_requires_a_literal_same_origin_path_with_one_id_placeholder(self):
        for template in ("//foreign.example/{id}", "https:" "//foreign.example/{id}", "/jobs/{title}",
                         "/jobs/{id}/{id}", "/jobs/x-{id}", "/jobs/../{id}", "/jobs/%2e%2e/{id}",
                         "/jobs\\/{id}", "/jobs/{id}?next=1", "/jobs/{id}#ref", None):
            with self.subTest(template=template), self.assertRaisesRegex(ValueError, "detail_url_template"):
                validate_source(dict(SOURCE, detail_url_template=template), Path.cwd())
        for path in ("", "record..id", None):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "detail_id_path"):
                validate_source(dict(SOURCE, detail_id_path=path), Path.cwd())
        for job_id in ("..", ".", "a/b", "a\\b", "a%2Fb"):
            with self.subTest(job_id=job_id), self.assertRaisesRegex(ValueError, "详情路径段"):
                detail_url(SOURCE, {"id": job_id})
        self.assertEqual(detail_url(SOURCE, {"id": "研发 #1"}),
                         "https:" "//board.example.invalid/jobs/%E7%A0%94%E5%8F%91%20%231")

    @patch("jobwatch.sources.WebSource.throttle")
    def test_cli_keeps_public_urls_and_rejects_mismatched_detail_ids(self, _throttle):
        jobs = [{"id": item, "title": label, "absolute_url": "https:" f"//careers.example.invalid/{item}"}
                for item, label in ((7, "工程师"), (8, "研究员"), (9, "设计师"))]
        pages = {"/jobs?listing=1": {"jobs": jobs, "total": 3},
                 "/jobs/7": {"record": {"id": "7", "content": "维护工具。"}},
                 "/jobs/8": {"record": {"id": 99, "content": "其他岗位内容。"}}}
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hits.append(self.path)
                if self.path == "/robots.txt":
                    body = b"User-agent: *\nAllow: /\n"
                elif self.path in pages:
                    body = json.dumps(pages[self.path]).encode("utf-8")
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with TemporaryDirectory() as directory, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                root = Path(directory)
                source = dict(SOURCE, url="http:" f"//127.0.0.1:{server.server_port}/jobs?listing=1", max_details=2)
                (root / "sources.json").write_text(json.dumps([source]), encoding="utf-8")
                config = root / "watch.json"
                config.write_text(json.dumps({"registry": "sources.json", "tasks": {
                    "one": {"mode": "company", "sources": ["fixture"]},
                }}), encoding="utf-8")
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["run", "--config", str(config), "--task", "one"]), 2)
                summary = json.loads(output.getvalue())
                folder = Path(summary["report"]).parent
                self.assertEqual(summary["sources"], {"fixture": "partial"})
                self.assertEqual(summary["collection"]["fixture"], {"pages": 1, "collected": 3, "declared_total": 3})
                self.assertEqual(summary["details"]["fixture"], {"success": 1, "failed": 1, "skipped": 1})
                self.assertEqual(hits, ["/robots.txt", "/jobs?listing=1", "/jobs/7", "/jobs/8"])
                self.assertIn("详情记录 ID 与列表不一致", (folder / "report.md").read_text(encoding="utf-8"))
                for name in ("cleaned.csv", "jobs.csv"):
                    with (folder / name).open(newline="", encoding="utf-8-sig") as stream:
                        self.assertEqual([(job["url"], job["duties"]) for job in csv.DictReader(stream)], [
                            (job["absolute_url"], "维护工具。" if i == 0 else "") for i, job in enumerate(jobs)
                        ])
            finally:
                server.shutdown()
                thread.join()
