from contextlib import redirect_stdout
import csv
from http.server import ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch

from jobwatch.cli import main, run_watch
from jobwatch.common import write_json
from jobwatch.sources import Collection, PAGE_CAP

from test_watch import FixtureHandler


def settings(root, sources, **tasks):
    write_json(root / "sources.json", sources)
    path = root / "watch.json"
    write_json(path, dict(registry="sources.json", tasks=tasks))
    return path


def job_ids(summary):
    with (Path(summary["report"]).parent / "jobs.csv").open(encoding="utf-8-sig", newline="") as stream:
        return {(row["source"], row["id"]) for row in csv.DictReader(stream)}


class QuickScanTests(TestCase):
    def test_fault_snapshot_retries_within_fresh_window_and_budget_zero_reports_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [dict(id=sid, company=sid, adapter="json_file", path=f"{sid}.json")
                       for sid in ("faulty", "limited")]
            task = dict(mode="role", direction="AI", sources="all", search=dict(max_refresh=0))
            path = settings(root, sources, agents=task)
            faulty = Collection(pages=1, errors=["列表结束时岗位数与声明总数不一致"],
                                jobs={"a": dict(id="a", title="AI", source="faulty")})
            limited = Collection(pages=1, errors=[PAGE_CAP],
                                 jobs={"b": dict(id="b", title="AI", source="limited")})
            for capture, sid in ((faulty, "faulty"), (limited, "limited")):
                capture.jobs = {key: dict(job, url="", type="", location="", company=sid, status="未知",
                                          duties="", requirements="", bonus="") for key, job in capture.jobs.items()}
            with patch("jobwatch.cli.collect", side_effect=[faulty, limited]):
                run_watch(path, "agents", scan="full")
            argv = ["run", "--config", str(path), "--task", "agents"]
            with patch("jobwatch.cli.collect", side_effect=AssertionError("unexpected collection")):
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(argv), 2)
                cached = json.loads(output.getvalue())
            self.assertEqual(cached["coverage"]["faulty"]["issue"], "error")
            self.assertEqual(cached["coverage"]["limited"]["issue"], "limit")
            self.assertEqual(cached["coverage"]["faulty"]["method"], "cached")
            task["search"]["max_refresh"] = 1
            settings(root, sources, agents=task)
            fixed = Collection(jobs=faulty.jobs, pages=1, list_complete=True)
            with patch("jobwatch.cli.collect", return_value=fixed) as collect, redirect_stdout(StringIO()) as output:
                self.assertEqual(main(argv), 0)
            refreshed = json.loads(output.getvalue())
            self.assertEqual(collect.call_args.args[0]["id"], "faulty")
            self.assertEqual(refreshed["coverage"]["limited"]["method"], "cached")
            self.assertEqual(refreshed["sources"], {"faulty": "complete", "limited": "partial"})

    def test_reuses_other_tasks_and_reserves_slot_for_unseen_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = []
            for sid in ("history", "background", "priority", "new"):
                sources.append(dict(id=sid, company=sid, adapter="json_file", path=f"{sid}.json"))
                write_json(root / f"{sid}.json", [dict(id=sid, title="Agent 开发")])
            path = settings(root, sources,
                            seed=dict(mode="role", direction="其他方向",
                                      sources=["history", "background", "priority"]),
                            agents=dict(mode="role", direction="Agent 开发", sources="all",
                                        search=dict(fresh_hours=0, max_refresh=2, priority_sources=["priority"])))
            seed = run_watch(path, "seed")
            result = run_watch(path, "agents")
            self.assertEqual(result["scan"], "quick")
            self.assertEqual({sid: row["method"] for sid, row in result["coverage"].items()},
                             dict(history="cached", background="cached", priority="refreshed", new="refreshed"))
            self.assertEqual(result["coverage"]["history"]["observed_at"], seed["coverage"]["history"]["observed_at"])
            self.assertEqual(job_ids(result), {(sid, sid) for sid in result["sources"]})
            self.assertEqual(result["changes"], 2)
            self.assertEqual(run_watch(path, resume=result["run_id"])["coverage"], result["coverage"])

    def test_failed_refresh_uses_prior_snapshot_and_preserves_failure(self):
        with TemporaryDirectory() as directory, redirect_stdout(StringIO()) as stdout:
            root = Path(directory)
            source = dict(id="one", company="星桥", adapter="json_file", path="jobs.json")
            write_json(root / "jobs.json", [dict(id="original", title="Agent 开发")])
            path = settings(root, [source], agents=dict(mode="role", direction="Agent 开发", sources="all",
                                                        search=dict(fresh_hours=0, max_refresh=1)))
            before = run_watch(path, "agents")
            (root / "jobs.json").unlink()
            self.assertEqual(main(["run", "--config", str(path), "--task", "agents", "--scan", "quick"]), 2)
            after = json.loads(stdout.getvalue())
            data = after["coverage"]["one"]
            self.assertEqual((after["sources"]["one"], data["method"], data["snapshot_status"]),
                             ("failed", "fallback", "complete"))
            self.assertEqual(data["observed_at"], before["coverage"]["one"]["observed_at"])
            self.assertTrue(data["attempt_errors"])
            self.assertEqual(job_ids(after), {("one", "original")})
            report = Path(after["report"]).read_text(encoding="utf-8")
            self.assertIn("读取本地快照", report)
            self.assertIn("本次刷新", report)

    def test_fresh_snapshot_is_reused_but_changed_source_is_not(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = dict(id="one", company="星桥", adapter="json_file", path="jobs.json")
            task = dict(mode="role", direction="Agent 开发", sources="all", search={})
            write_json(root / "jobs.json", [dict(id="original", title="Agent 开发")])
            path = settings(root, [source], agents=task)
            before = run_watch(path, "agents")
            with patch("jobwatch.cli.collect", side_effect=AssertionError("unexpected collection")):
                same = run_watch(path, "agents")
            self.assertEqual(same["coverage"]["one"]["method"], "cached")
            self.assertEqual(same["coverage"]["one"]["observed_at"], before["coverage"]["one"]["observed_at"])
            self.assertEqual(same["changes"], 0)
            task["search"]["max_refresh"] = 0
            source["path"] = "different.json"
            settings(root, [source], agents=task)
            missing = run_watch(path, "agents")
            self.assertEqual(missing["sources"], {"one": "not_collected"})
            self.assertEqual(missing["coverage"]["one"], dict(company="星桥", method="not_collected", observed_at=None))
            self.assertEqual(job_ids(missing), set())


class FullScanTests(TestCase):
    @patch("jobwatch.sources.time.sleep")
    def test_full_scan_visits_all_sources_and_the_second_page(self, sleep):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        base = "http:" f"//127.0.0.1:{server.server_port}"
        sources = [
            dict(id="fixture", company="星桥", adapter="json_http", url=base + "/api", access_reviewed=True,
                 items_path="data.items", total_path="data.total", pagination={"page_param": "p"},
                 fields={"id": "id", "title": "title", "url": "url"}),
            dict(id="another", company="云原", adapter="html_http", url=base + "/careers", access_reviewed=True,
                 items_xpath="//article", fields={"title": "./a/text()", "url": "./a/@href"},
                 next_xpath="//a[@rel='next']/@href"),
        ]
        with TemporaryDirectory() as directory:
            path = settings(Path(directory), sources, agents=dict(mode="role", direction="应用开发", sources="all",
                                                                 search=dict(max_refresh=1)))
            result = run_watch(path, "agents", scan="full")
            self.assertEqual(result["scan"], "full")
            self.assertEqual(result["sources"], dict(fixture="complete", another="complete"))
            self.assertTrue(all(item["pages"] == 2 for item in result["collection"].values()))
            self.assertEqual(result["candidates"], 4)
