from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from jobwatch.cli import run_watch
from jobwatch.common import read_json, write_json


WORKER = """
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(sys.argv[1])
source = json.loads((root / 'source.json').read_bytes())

def report(name, value):
    pending = root / (name + '.tmp')
    pending.write_text(json.dumps(value), encoding='utf-8')
    pending.replace(root / name)

while True:
    request_path = root / 'request.json'
    if not request_path.exists():
        time.sleep(0.025)
        continue
    request = json.loads(request_path.read_bytes())
    request_path.unlink()
    action = request['operation']
    source_id = source['id']
    output = Path(request['path']) if 'path' in request else None
    if action == 'close':
        if source_id == 'slow-close':
            report('phase.json', {'phase': '关闭会话'})
            time.sleep(60)
        report('reply.json', {})
        break
    if source_id == 'slow-list':
        report('phase.json', {'phase': '加载页面'})
        time.sleep(60)
    if source_id == 'slow-detail' and output.name == 'detail-00002.html':
        marker = output.parent / 'orphan.txt'
        child = 'from pathlib import Path; import sys,time; time.sleep(7); Path(sys.argv[1]).touch()'
        options = {'start_new_session': True} if os.name != 'nt' else {}
        subprocess.Popen([sys.executable, '-c', child, str(marker)], **options)
        report('phase.json', {'phase': '保存渲染页面'})
        time.sleep(60)
    if request['detail']:
        page = '<p>Develop an example system.</p>'
    else:
        page = ''.join(
            '<article><a href="/detail/' + str(i) + '">Engineer ' + str(i) + '</a></article>'
            for i in range(1, 4)
        )
    output.write_text(page, encoding='utf-8')
    report('reply.json', {})
"""


class BrowserWatchdogTests(TestCase):
    def test_stalled_list_detail_and_close_do_not_stop_remaining_sources(self):
        children = []

        def launch(directory):
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            proc = subprocess.Popen([sys.executable, "-c", WORKER, str(directory)], **options)
            children.append(proc)
            return proc

        with TemporaryDirectory() as temp:
            folder = Path(temp)
            source = dict(company="Browser Fixture", url="https:" "//example.test/jobs", adapter="html_http",
                          access_reviewed=True, single_page=True, browser={"operation_timeout_seconds": 2},
                          items_xpath="//article", fields={"title": "./a/text()", "url": "./a/@href"})
            registry = [
                dict(source, id="slow-list"),
                dict(source, id="slow-detail", detail_fields={"duties": "//p/text()"}),
                dict(source, id="slow-close"),
                {"id": "local", "company": "Local Fixture", "adapter": "json_file", "path": "jobs.json"},
            ]
            write_json(folder / "sources.json", registry)
            write_json(folder / "jobs.json", [{"id": "local-1", "title": "Engineer local"}])
            cfg = folder / "watch.json"
            write_json(cfg, dict(registry="sources.json",
                                 tasks={"engineer": {"mode": "role", "direction": "Engineer", "sources": "all"}}))
            with patch("jobwatch.browser_process.launch_worker", side_effect=launch):
                full = run_watch(cfg, task="engineer", scan="full")
                quick = run_watch(cfg, task="engineer", scan="quick")

            self.assertEqual(full["sources"], {"slow-list": "failed", "slow-detail": "partial",
                                               "slow-close": "partial", "local": "complete"})
            self.assertEqual(full["details"]["slow-detail"], {"success": 1, "failed": 1, "skipped": 1})
            self.assertEqual(full["cleaned"], 7)
            self.assertEqual(quick["cleaned"], 7)
            self.assertEqual({sid: quick["coverage"][sid]["method"] for sid in quick["sources"]},
                             {"slow-list": "refreshed", "slow-detail": "refreshed", "slow-close": "refreshed",
                              "local": "cached"})
            self.assertEqual([quick["coverage"][sid]["issue"] for sid in ("slow-list", "slow-detail", "slow-close")],
                             ["error", "error", "error"])
            raw = Path(full["report"]).parent / "raw"
            for sid, phase in (("slow-list", "加载页面"), ("slow-detail", "保存渲染页面"), ("slow-close", "关闭会话")):
                diag = read_json(raw / sid / "browser-watchdog.json")
                self.assertEqual(diag["phase"], phase)
                self.assertIn("硬超时", diag["message"])
            self.assertEqual(len(children), 6)
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertFalse((raw / "slow-detail" / "orphan.txt").exists())
