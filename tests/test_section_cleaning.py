import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from jobwatch.ai import AIError, BudgetExceeded, Responses
from jobwatch.cli import run_watch
from jobwatch.common import read_json, write_json


class SectionCleaningTests(TestCase):
    def write_config(self, root, jobs):
        config = root / "watch.json"
        write_json(root / "jobs.json", jobs)
        write_json(root / "sources.json", [dict(id="fixture", company="样例", adapter="json_file", path="jobs.json")])
        write_json(config, dict(registry="sources.json", ai=dict(model="fixture", batch_size=1),
                                tasks={"one": dict(mode="company", sources=["fixture"],
                                                   ai_clean=True, ai_scope="all")}))
        return config

    def read_cleaned(self, summary):
        folder = Path(summary["report"]).parent
        with (folder / "cleaned.csv").open(encoding="utf-8-sig", newline="") as file:
            return {row["id"]: row for row in csv.DictReader(file)}

    def test_cleaned_sections_feed_analysis_and_survive_a_resume(self):
        jobs = [
            {"id": "known", "title": "明确章节", "duties": (
                "About Us\n样例企业\nWhat you'll do\n开发智能体。\nQualifications\n熟悉 SQL。"
                "\nNice to have\n熟悉 Go。\nBenefits\n带薪休假。")},
            {"id": "mixed", "title": "混合正文", "duties": "样例公司。\n集成模型。\n熟悉 Python。\n了解 MCP。"},
            {"id": "mapped", "title": "字段已映射", "duties": "维护中间件。", "requirements": "了解 Linux。"},
        ]
        analysis_inputs = []

        def answer(_prompt, _schema, payload, *, recovery=False):
            job = payload["jobs"][0]
            if recovery:
                self.assertEqual(job, {"title": "混合正文", "lines": dict(enumerate(jobs[1]["duties"].splitlines()))})
                return {"results": [{"index": 0, "sections": [
                    {"start": index, "label": label}
                    for index, label in enumerate(("context", "duties", "requirements", "bonus"))
                ]}]}
            analysis_inputs.append(job)
            return {"results": [dict(index=0, verdict="match", evidence_field="duties",
                                     evidence=job["duties"], facts=[])]}

        with TemporaryDirectory() as directory, patch.dict("os.environ", {"OPENAI_" "API_KEY": "fixture-only"}):
            config = self.write_config(Path(directory), jobs)
            with patch.object(Responses, "request", side_effect=answer) as request:
                summary = run_watch(config, "one", use_ai=True)
                self.assertEqual(sum(call.kwargs.get("recovery", False) for call in request.call_args_list), 1)
            self.assertEqual(summary["ai_validated"], 3)
            fields = self.read_cleaned(summary)
            self.assertEqual((fields["mixed"]["duties"], fields["mixed"]["requirements"], fields["mixed"]["bonus"]),
                             ("集成模型。", "熟悉 Python。", "了解 MCP。"))
            self.assertEqual((fields["known"]["duties"], fields["known"]["requirements"], fields["known"]["bonus"]),
                             ("开发智能体。", "熟悉 SQL。", "熟悉 Go。"))
            self.assertEqual(fields["mapped"]["requirements"], "了解 Linux。")
            self.assertIn(dict(title="混合正文", duties="集成模型。", requirements="熟悉 Python。", bonus="了解 MCP。"),
                          analysis_inputs)
            audit = read_json(summary["ai_cleaning"]["report"])
            self.assertEqual((audit["summary"]["rules"], audit["summary"]["ai"]), (1, 1))
            contexts = {row["id"]: row["context"] for row in audit["jobs"]}
            self.assertEqual(contexts["mixed"], "样例公司。")
            self.assertIn("带薪休假。", contexts["known"])
            with patch.object(Responses, "request", side_effect=AssertionError("unexpected request")):
                cached = run_watch(config, "one", use_ai=True)
                resumed = run_watch(config, resume=summary["run_id"])
            self.assertEqual(cached["ai_cleaning"]["cached"], 1)
            self.assertEqual(resumed["ai_validated"], 3)
            self.assertEqual(self.read_cleaned(cached), fields)
            self.assertEqual(self.read_cleaned(resumed), fields)

    def test_invalid_labels_and_budget_limits_leave_text_available_for_analysis(self):
        jobs = [{"id": "one", "title": "未知章节", "duties": "开发工具。\n熟悉 Python。"}]
        for reply in ({"results": [{"index": 0, "sections": [{"start": 1, "label": "duties"}]}]},
                      AIError("AI API 网络请求失败"), BudgetExceeded("已达到本次辅助解析上限")):
            with self.subTest(reply=reply), TemporaryDirectory() as directory, patch.dict(
                    "os.environ", {"OPENAI_" "API_KEY": "fixture-only"}):
                config = self.write_config(Path(directory), jobs)

                def answer(_prompt, _schema, payload, *, recovery=False):
                    if recovery:
                        if isinstance(reply, AIError):
                            raise reply
                        return reply
                    self.assertEqual(payload["jobs"][0]["duties"], jobs[0]["duties"])
                    return {"results": [dict(index=0, verdict="match", evidence_field="duties",
                                             evidence="开发工具。", facts=[])]}

                with patch.object(Responses, "request", side_effect=answer):
                    summary = run_watch(config, "one", use_ai=True)
                self.assertEqual(summary["ai_cleaning"]["fallback"], 1)
                self.assertEqual(summary["ai_validated"], 1)
                self.assertEqual(self.read_cleaned(summary)["one"]["duties"], jobs[0]["duties"])
