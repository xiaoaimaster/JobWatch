import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from jobwatch.cli import run_watch
from jobwatch.config import load_settings
from jobwatch.report import selection


KEYWORDS = ["AI 应用开发", "大模型应用开发"]


class RoleSelectionTests(TestCase):
    def test_only_directly_negated_mentions_are_ignored(self):
        task = dict(mode="role", cities=[], types=[], pinned_urls=[], keywords=KEYWORDS, candidate_keywords=[])
        job = dict(url="", title="软件工程师", duties="", requirements="")
        cases = (
            ("仅使用现成 AI 工具整理文档，不负责 AI 应用开发。", "", ""),
            ("岗位不涉及 AI 应用开发。", "", ""),
            ("", "无需具备大模型应用开发经验。", ""),
            ("不做 AI 应用开发，但负责现有产品维护。", "", ""),
            ("不负责 AI 应用开发\uff1b参与 AI 应用开发原型验证。", "", "AI 应用开发"),
            ("不负责 AI 应用开发但参与 AI 应用开发原型验证。", "", "AI 应用开发"),
            ("负责 AI 应用开发，不负责 AI 应用开发团队的招聘。", "", "AI 应用开发"),
            ("无需负责 AI 应用开发", "有大模型应用开发经验优先。", "大模型应用开发"),
            ("不用值班，参与大模型应用开发。", "", "大模型应用开发"),
            ("不仅负责 AI 应用开发，还要维护平台。", "", "AI 应用开发"),
            ("并非不涉及 AI 应用开发。", "", "AI 应用开发"),
            ("负责 AI\n应用开发。", "", "AI 应用开发"),
        )
        for duties, requirements, keyword in cases:
            with self.subTest(duties=duties, requirements=requirements):
                expected = ("正文候选", keyword) if keyword else ("", "")
                self.assertEqual(selection(task, dict(job, duties=duties, requirements=requirements)), expected)

    def test_body_rules_link_context_to_the_target(self):
        task = dict(mode="role", cities=[], types=[], pinned_urls=[], keywords=KEYWORDS,
                    candidate_keywords=["大模型应用", "AIGC", "AI应用"],
                    body_candidate_rules=[dict(terms=["Agent", "智能体"], context=["开发", "集成", "搭建"])])
        job = dict(url="", title="研发工程师", duties="", requirements="")
        cases = (
            ({"duties": "将大模型 agent 与实验分析工具集成。"}, ("正文候选", "Agent + 集成")),
            ({"requirements": "具备从零搭建智能体的经验。"}, ("正文候选", "智能体 + 搭建")),
            ({"duties": "负责线上 Agent 产品的部署与运维。"}, ("", "")),
            ({"duties": "负责 Agent 产品运维，另行开发监控平台。"}, ("", "")),
            ({"duties": "负责 Agent 产品运维。开发监控平台。"}, ("", "")),
            ({"duties": "负责 Agent 产品运维\n开发监控平台。"}, ("", "")),
            ({"duties": "负责 Agent 产品运维 / 开发监控平台。"}, ("", "")),
            ({"duties": "运维 Agent 并开发监控工具。"}, ("", "")),
            ({"duties": "开发监控工具并维护现有 Agent。"}, ("", "")),
            ({"duties": "开发客服 Agent 并维护生产环境。"}, ("正文候选", "Agent + 开发")),
            ({"duties": "维护旧 Agent 并开发新 Agent。"}, ("正文候选", "Agent + 开发")),
            ({"duties": "开发并维护客服 Agent。"}, ("正文候选", "Agent + 开发")),
            ({"duties": "负责客服 Agent 的交付，包括开发和上线验证。"}, ("正文候选", "Agent + 开发")),
            ({"duties": "承担客服智能体的实施\uff1a具体包括搭建、内部验收。"}, ("正文候选", "智能体 + 搭建")),
            ({"duties": "负责客服 Agent 的交付，包括开发监控工具。"}, ("", "")),
            ({"duties": "负责客服 Agent 的交付。包括开发和上线验证。"}, ("", "")),
            ({"duties": "不负责客服 Agent 的交付，包括开发和上线验证。"}, ("", "")),
            ({"duties": "运维已有 Agent，包括开发和上线监控工具。"}, ("", "")),
            ({"duties": "负责 Agent 产品运维。", "requirements": "具备开发经验。"}, ("", "")),
            ({"duties": "不负责 Agent 开发。"}, ("", "")),
            ({"duties": "开发 Node.js Agent。"}, ("正文候选", "Agent + 开发")),
            ({"title": "AIGC 工程师"}, ("待分析候选", "AIGC")),
            ({"title": "大模型应用产品经理"}, ("待分析候选", "大模型应用")),
            ({"title": "AI应用与创新工程师"}, ("待分析候选", "AI应用")),
            ({"title": "大模型应用开发工程师"}, ("标题匹配", "大模型应用开发")),
        )
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(selection(task, dict(job, **fields)), expected)

    def test_body_rule_increment_on_nine_synthetic_jobs(self):
        examples = {
            "platform": ("平台工程师", "开发客服 Agent。"),
            "integration": ("业务系统工程师", "将 Agent 集成到知识库问答系统。"),
            "solutions": ("解决方案工程师", "从零搭建智能体。"),
            "production": ("生产运维工程师", "只部署、监控和维护已有 Agent。"),
            "observability": ("可观测性工程师", "运维 Agent 并开发监控工具。"),
            "infrastructure": ("基础设施工程师", "负责 Agent 运维。开发监控工具。"),
            "delivery": ("业务交付工程师", "负责客服 Agent 的交付，包括开发和上线验证。"),
            "project": ("项目工程师", "参与 Agent 集成方案讨论。"),
            "backend": ("后端工程师", "负责 AI 应用开发。"),
        }
        expected = {
            "a": {"backend": "AI 应用开发"},
            "b": {"backend": "AI 应用开发", "platform": "Agent + 开发", "integration": "Agent + 集成",
                  "solutions": "智能体 + 搭建", "delivery": "Agent + 开发", "project": "Agent + 集成"},
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = [dict(id=key, title=title, duties=duties, requirements="具备团队协作经验。")
                    for key, (title, duties) in examples.items()]
            (root / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
            (root / "sources.json").write_text(json.dumps([
                dict(id="fixture", company="测试公司", adapter="json_file", path="jobs.json"),
            ]), encoding="utf-8")
            task = dict(mode="role", sources=["fixture"], direction="AI 应用开发", keywords=KEYWORDS,
                        candidate_keywords=["LLM 应用", "大模型应用", "AI应用", "AIGC"])
            cleaned = {}
            for group in expected:
                cfg = root / f"{group}.json"
                options = dict(task)
                if group == "b":
                    options["body_candidate_rules"] = [dict(terms=["Agent", "智能体"], context=["开发", "集成", "搭建"])]
                cfg.write_text(json.dumps(dict(registry="sources.json", output=group, tasks={"ai-apps": options})),
                               encoding="utf-8")
                result = run_watch(cfg, task="ai-apps")
                self.assertEqual(result["cleaned"], 9)
                folder = Path(result["report"]).parent
                cleaned[group] = (folder / "cleaned.csv").read_bytes()
                with (folder / "jobs.csv").open(encoding="utf-8-sig", newline="") as stream:
                    self.assertEqual({row["id"]: row["reason"] for row in csv.DictReader(stream)}, expected[group])
            self.assertEqual(cleaned["a"], cleaned["b"])

    def test_configured_body_rules_run_without_ai(self):
        jobs = [
            dict(id="research", title="应用研究员", duties="集成 Agent 与实验分析工具。"),
            dict(id="lead", title="大模型应用算法负责人", duties="负责多模态研发和大模型评估。"),
            dict(id="workflow", title="AI应用与创新工程师", duties="设计、落地并迭代大模型与 Agent 工作流。"),
            dict(id="product", title="大模型应用产品经理", duties="规划智能客服产品，协同研发交付。"),
            dict(id="ops", title="数据应用 SRE 工程师", duties="负责现有 Agent 产品运维，开发监控工具。"),
            dict(id="aigc", title="AIGC 工程师", duties="搭建生成式 AI 美术工作流。"),
        ]
        task = dict(mode="role", sources=["fixture"], direction="AI 应用开发", keywords=KEYWORDS,
                    candidate_keywords=["大模型应用", "AI应用", "AIGC"],
                    body_candidate_rules=[dict(terms=["Agent"], context=["开发", "集成"])])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
            (root / "sources.json").write_text(json.dumps([
                dict(id="fixture", company="测试公司", adapter="json_file", path="jobs.json"),
            ]), encoding="utf-8")
            cfg = root / "watch.json"
            settings = dict(registry="sources.json", tasks={"ai-apps": task})
            cfg.write_text(json.dumps(settings), encoding="utf-8")
            result = run_watch(cfg, task="ai-apps")
            self.assertEqual(result["sources"], {"fixture": "complete"})
            self.assertEqual((result["cleaned"], result["candidates"]), (6, 5))
            folder = Path(result["report"]).parent
            with (folder / "jobs.csv").open(encoding="utf-8-sig", newline="") as stream:
                selected = {row["id"]: (row["match"], row["reason"]) for row in csv.DictReader(stream)}
            self.assertEqual(selected, {"research": ("正文候选", "Agent + 集成"),
                                        "lead": ("待分析候选", "大模型应用"),
                                        "workflow": ("待分析候选", "AI应用"),
                                        "product": ("待分析候选", "大模型应用"),
                                        "aigc": ("待分析候选", "AIGC")})
            report = (folder / "report.md").read_text(encoding="utf-8")
            self.assertIn("Agent + 集成", report)
            self.assertNotIn("数据应用 SRE 工程师", report)

            for rules in (None, "Agent", [{"terms": ["Agent"], "context": []}],
                          [{"terms": ["Agent"], "context": ["  "]}]):
                with self.subTest(invalid_rules=rules):
                    task["body_candidate_rules"] = rules
                    cfg.write_text(json.dumps(settings), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "body_candidate_rules"):
                        load_settings(cfg)

    def test_cross_company_run_excludes_the_negated_job_from_filtered_outputs(self):
        groups = {
            "starbridge": ("星桥软件", [
                {"id": "shared-01", "title": "大模型应用开发工程师"},
                {"id": "llm", "title": "LLM 应用工程师"},
                {"id": "admin", "title": "行政助理", "duties": "使用 AI 工具辅助办公。"},
            ]),
            "cloudridge": ("云岚系统", [
                {"id": "shared-01", "title": "大模型应用开发工程师"},
                {"id": "platform", "title": "平台工程师", "duties": "负责 AI 应用开发与平台集成。"},
                {"id": "legal", "title": "法务专员"},
            ]),
            "spiritboat": ("灵舟科技", [
                {"id": "software", "title": "软件工程师", "requirements": "具备大模型应用开发经验。"},
                {"id": "clerk", "title": "办公室文员", "duties": "仅使用现成 AI 工具整理文档，不负责 AI 应用开发。"},
            ]),
        }
        expected = {
            ("starbridge", "shared-01"): ("标题匹配", "大模型应用开发"),
            ("starbridge", "llm"): ("待分析候选", "LLM 应用"),
            ("cloudridge", "shared-01"): ("标题匹配", "大模型应用开发"),
            ("cloudridge", "platform"): ("正文候选", "AI 应用开发"),
            ("spiritboat", "software"): ("正文候选", "大模型应用开发"),
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = []
            original = {}
            for source, (company, jobs) in groups.items():
                name = f"{source}.json"
                registry.append(dict(id=source, company=company, adapter="json_file", path=name))
                for job in jobs:
                    job["url"] = "https:" f"//{source}.example.invalid/jobs/{job['id']}"
                    original[source, job["id"]] = (company, job)
                (root / name).write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
            (root / "sources.json").write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
            cfg = root / "watch.json"
            cfg.write_text(json.dumps({
                "registry": "sources.json",
                "tasks": {"ai-apps": dict(mode="role", sources=list(groups), direction="AI 应用开发",
                                         keywords=KEYWORDS, candidate_keywords=["LLM 应用"])},
            }, ensure_ascii=False), encoding="utf-8")
            result = run_watch(cfg, task="ai-apps")
            self.assertEqual(result["sources"], dict.fromkeys(groups, "complete"))
            self.assertEqual(result["cleaned"], 8)
            self.assertEqual(result["candidates"], 5)
            folder = Path(result["report"]).parent
            with (folder / "cleaned.csv").open(encoding="utf-8-sig", newline="") as stream:
                cleaned = {(row["source"], row["id"]): row for row in csv.DictReader(stream)}
            self.assertEqual(cleaned.keys(), original.keys())
            with (folder / "jobs.csv").open(encoding="utf-8-sig", newline="") as stream:
                selected = {(row["source"], row["id"]): row for row in csv.DictReader(stream)}
            self.assertEqual({key: (row["match"], row["reason"]) for key, row in selected.items()}, expected)
            report = (folder / "report.md").read_text(encoding="utf-8")
            changes = json.loads((folder / "changes.json").read_text(encoding="utf-8"))
            ai_inputs = json.loads((folder / "ai" / "input.json").read_text(encoding="utf-8"))
            self.assertEqual({(row["source"], row["id"]) for row in changes}, expected.keys())
            self.assertEqual({(row["source"], row["id"]) for row in ai_inputs},
                             {("cloudridge", "platform"), ("spiritboat", "software")})
            for key, (company, job) in original.items():
                self.assertEqual((cleaned[key]["company"], cleaned[key]["url"]), (company, job["url"]))
                self.assertEqual((cleaned[key]["duties"], cleaned[key]["requirements"]),
                                 (job.get("duties", ""), job.get("requirements", "")))
                if key in expected:
                    self.assertEqual((selected[key]["company"], selected[key]["url"]), (company, job["url"]))
                    self.assertIn(f"<{job['url']}>", report)
                else:
                    self.assertNotIn(job["title"], report)
