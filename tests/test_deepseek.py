from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from jobwatch.ai import AIError, ANALYSIS_SCHEMA, PROMPT, DeepSeekChat, Responses, fingerprint, validate_analysis
from jobwatch.cli import run_watch
from jobwatch.common import read_json, write_json
from jobwatch.config import AI_DEFAULTS, load_settings


def response(content, reason="stop"):
    body = dict(choices=[dict(finish_reason=reason, message=dict(content=content))],
                usage=dict(prompt_tokens=20, completion_tokens=12))
    return BytesIO(json.dumps(body).encode())


class DeepSeekTests(TestCase):
    def test_all_scope_uses_non_candidates_and_retains_excluded_results(self):
        sample = [
            dict(id="one", title="后端工程师", duties="集成内部智能体"),
            dict(id="two", title="产品经理", duties="负责产品路线图"),
            dict(id="three", title="智能体方向", duties="职责描述不清楚"),
            dict(id="four", title="无正文"),
        ]
        responses = {"后端工程师": ("match", "duties"), "产品经理": ("unrelated", "duties"),
                     "智能体方向": ("match", "title")}

        def fixture_response(_prompt, _schema, payload):
            results = []
            for index, job in enumerate(payload["jobs"]):
                verdict, field = responses[job["title"]["0"]]
                results.append(dict(index=index, verdict=verdict, evidence_field=field, evidence_index=0, facts=[]))
            return {"results": results}
        with TemporaryDirectory() as temp, patch.dict("os.environ", {"DEEPSEEK_" "API_KEY": "fixture-only"}):
            root = Path(temp)
            write_json(root / "jobs.json", sample)
            write_json(root / "sources.json", [dict(id="sample", company="样例", adapter="json_file", path="jobs.json")])
            config = root / "watch.json"
            write_json(config, dict(registry="sources.json", tasks={
                "survey": dict(mode="role", direction="AI 应用研发", sources="all", ai_scope="all")
            }, ai=dict(provider="deepseek", model="fixture")))
            with patch.object(DeepSeekChat, "request", side_effect=fixture_response) as request:
                summary = run_watch(config, "survey", use_ai=True)
            self.assertEqual(len(request.call_args.args[2]["jobs"]), 3)
            self.assertEqual((summary["ai_eligible"], summary["ai_no_body"]), (3, 1))
            run = Path(summary["report"]).parent
            all_results = read_json(run / "ai/results.json")
            self.assertEqual({r["id"]: r["analysis"]["verdict"] for r in all_results if r["analysis"]},
                             {"one": "match", "two": "unrelated", "three": "uncertain"})
            self.assertEqual(summary["ai_verdicts"], dict(match=1, unrelated=1, uncertain=1))
            self.assertIn("后端工程师", (run / "report.md").read_text(encoding="utf-8"))
            self.assertNotIn("产品经理", (run / "report.md").read_text(encoding="utf-8"))

    def test_provider_defaults_and_chat_transport(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / "sources.json", [dict(id="local", company="Fixture",
                                                    adapter="json_file", path="jobs.json")])
            config = root / "watch.json"
            write_json(config, dict(registry="sources.json",
                                    tasks=dict(fixture=dict(mode="company", sources=["local"])),
                                    ai=dict(provider="deepseek", model="deepseek-flash")))
            _, _, _, settings = load_settings(config)
        self.assertEqual(settings["base_url"], "https:" "//api.deepseek.com")
        self.assertEqual(settings["api_key_env"], "DEEPSEEK_" "API_KEY")
        with patch.dict("os.environ", {"DEEPSEEK_" "API_KEY": "fixture-only"}):
            client = DeepSeekChat(settings)
        client.opener = Mock()
        roles = [{"title": "Agent 开发", "duties": "开发智能体服务。", "requirements": "", "bonus": ""}]
        valid = dict(results=[dict(index=0, verdict="match", evidence_field="duties",
                                   evidence_index=0, facts=[])])
        client.opener.open.return_value = response(json.dumps(valid))
        result, discarded = client.analyze(client.analysis_payload("Agent 开发", roles), roles)
        self.assertEqual(result[0]["verdict"], "match")
        self.assertEqual(result[0]["evidence"], roles[0]["duties"])
        self.assertEqual(discarded, 0)
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https:" "//api.deepseek.com/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-only")
        payload = json.loads(request.data)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(json.loads(payload["messages"][1]["content"])["jobs"], [
            {"title": {"0": "Agent 开发"}, "duties": {"0": "开发智能体服务。"}, "requirements": {}, "bonus": {}},
        ])
        self.assertEqual(client.usage, dict(input_tokens=20, output_tokens=12))
        with patch.dict("os.environ", {"OPENAI_" "API_KEY": "fixture-only"}):
            other = Responses(dict(AI_DEFAULTS, model=client.model, base_url=client.base))
        self.assertNotEqual(fingerprint(client, "Agent 开发", roles[0]),
                            fingerprint(other, "Agent 开发", roles[0]))

    def test_empty_or_truncated_output_is_rejected_and_usage_is_retained(self):
        settings = {**AI_DEFAULTS, "model": "fixture", "base_url": DeepSeekChat.default_base,
                    "api_key_env": DeepSeekChat.default_key_env}
        with patch.dict("os.environ", {"DEEPSEEK_" "API_KEY": "fixture-only"}):
            client = DeepSeekChat(settings)
        client.opener = Mock()
        client.opener.open.side_effect = [response("{}", "length"), response("  ")]
        for reason in ("Token 上限", "空内容"):
            with self.subTest(reason=reason), self.assertRaisesRegex(AIError, reason):
                client.request(PROMPT, ANALYSIS_SCHEMA, {})
        self.assertEqual(client.usage, dict(input_tokens=40, output_tokens=24))

    def test_analysis_reconstructs_quotes_and_rejects_only_invalid_facts(self):
        with patch.dict("os.environ", {"DEEPSEEK_" "API_KEY": "fixture-only"}):
            client = DeepSeekChat(dict(AI_DEFAULTS, model="fixture", api_key_env="DEEPSEEK_" "API_KEY"))
        client.opener = Mock()
        roles = [{"title": "Engineer", "duties": "Build AI tools. Maintain pipelines.",
                  "requirements": "熟悉 Python 和 SQL。\n了解数据仓库。", "bonus": ""}]
        valid_fact = dict(name="python", kind="skill", field="requirements", evidence_index=0)
        candidate = dict(results=[dict(index=0, verdict="match", evidence_field="duties", evidence_index=0,
                                       facts=[valid_fact, dict(valid_fact, name="Python"),
                                              dict(valid_fact, name="数据库"),
                                              dict(valid_fact, name="SQL", evidence_index=99),
                                              dict(valid_fact, name="SQL", evidence_index=1)])])
        client.opener.open.return_value = response(json.dumps(candidate))
        result, discarded = client.analyze(client.analysis_payload("AI 开发", roles), roles)
        self.assertEqual(discarded, 4)
        self.assertEqual(result[0]["evidence"], "Build AI tools.")
        self.assertEqual(result[0]["facts"], [
            dict(name="Python", kind="skill", field="requirements", evidence="熟悉 Python 和 SQL。"),
        ])
        self.assertEqual(validate_analysis({"results": [dict(index=0, **result[0])]}, roles), result)

    def test_analysis_rejects_missing_mandatory_evidence(self):
        with patch.dict("os.environ", {"DEEPSEEK_" "API_KEY": "fixture-only"}):
            client = DeepSeekChat(dict(AI_DEFAULTS, model="fixture", api_key_env="DEEPSEEK_" "API_KEY"))
        client.opener = Mock()
        roles = [{"title": "Engineer", "duties": "Build AI tools.", "requirements": "", "bonus": ""}]
        payload = client.analysis_payload("AI 开发", roles)
        for span in (1, -1, True, "0"):
            candidate = dict(results=[dict(index=0, verdict="match", evidence_field="duties",
                                           evidence_index=span, facts=[])])
            client.opener.open.return_value = response(json.dumps(candidate))
            with self.subTest(span=span), self.assertRaisesRegex(AIError, "不存在的原文片段"):
                client.analyze(payload, roles)
