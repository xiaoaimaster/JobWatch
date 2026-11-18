"""CLI for the standalone job watcher."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
from pathlib import Path
import re
import sqlite3
import sys
import uuid

from lxml import etree

from . import store
from .ai import (AI_CLIENTS, AIError, analysis_input,
                 fingerprint, validate_analysis)
from .common import digest, dump, project_lock, write_json
from .config import SEARCH_DEFAULTS, load_settings
from .report import changes, read_cleaned_csv, render, selection, write_csv
from .section_cleaning import clean_jobs
from .sources import collect, coverage_issue

RUN_ID = re.compile(r"\d{8}T\d{12}Z-[a-f0-9]{8}")
CSV_FIELDS = ("source", "company", "id", "title", "type", "location", "status",
              "url", "duties", "requirements", "bonus")


def source_identity(source):
    identity = {key: value for key, value in source.items()
                if key not in ("discovered_via", "interval_seconds", "max_pages", "max_details", "access_reviewed")}
    if "browser" in identity:
        identity["browser"] = {key: value for key, value in identity["browser"].items()
                               if key != "operation_timeout_seconds"}
    return identity


def quick_refresh(task, captures, now):
    options = task.get("search", SEARCH_DEFAULTS)
    cutoff = now - timedelta(hours=options["fresh_hours"])
    faults = {sid for sid, capture in captures.items() if capture and
              coverage_issue(capture[1]["snapshot_status"], capture[1]["errors"]) == "error"}
    stale = [sid for sid in task["sources"]
             if sid in faults or not captures[sid]
             or datetime.fromisoformat(captures[sid][1]["observed_at"]) <= cutoff]
    priority = {sid: index for index, sid in enumerate(options["priority_sources"])}

    def order(sid):
        tier = 0 if sid in faults else 1
        if not captures[sid]:
            return tier, priority.get(sid, len(priority)), 0, ""
        payload = captures[sid][1]
        hits = sum(bool(selection(task, job)[0]) for job in payload["jobs"].values())
        return tier, priority.get(sid, len(priority)), -hits, payload["observed_at"]

    stale.sort(key=order)
    limit = options["max_refresh"]
    selected = stale[:limit]
    if len(selected) > 1 and all(captures[sid] for sid in selected):
        unseen = next((sid for sid in stale[limit:] if not captures[sid]), None)
        if unseen is not None:
            selected[-1] = unseen
    return selected


def new_run(db, output, name, task, sources, ai, scan=None):
    scan = scan or ("quick" if "search" in task else "full")
    if scan == "quick" and task["mode"] != "role":
        raise ValueError("快速查找仅适用于 role 任务")
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:8]
    folder = output / "runs" / run_id
    folder.mkdir(parents=True)
    scope = digest({key: value for key, value in task.items()
                    if key not in ("sources", "search", "ai_scope", "ai_clean")})
    identities = {sid: source_identity(sources[sid]) for sid in task["sources"]}
    captures = {}
    refresh = task["sources"]
    if scan == "quick":
        captures = {sid: store.latest_capture(db, sid, identity) for sid, identity in identities.items()}
        refresh = quick_refresh(task, captures, now)
    settings = dict(task, scan=scan, source_companies={sid: sources[sid]["company"] for sid in identities})
    with db:
        db.execute("INSERT INTO runs(id, task, started, scope, settings) VALUES(?,?,?,?,?)",
                   (run_id, name, now.isoformat(), scope, dump(settings)))
    recovered = ai if ai and ai.config["recover_fields"] else None
    for sid in refresh:
        result = collect(sources[sid], folder / "raw" / sid, recovered)
        store.save_observation(db, run_id, sources[sid], digest([scope, identities[sid], "normalization-v2"]), result)
        if (capture := captures.get(sid)) and result.status in ("failed", "partial") and not result.jobs:
            store.reuse_observation(db, run_id, sid, capture[0])
    if scan == "quick":
        for sid in identities.keys() - set(refresh):
            capture = captures[sid]
            store.reuse_observation(db, run_id, sid, capture[0] if capture else None)
    with db:
        db.execute("UPDATE runs SET collection_ready=1 WHERE id=?", (run_id,))
    return run_id


def analyze(db, run_id, task, jobs, ai, folder):
    direction = task["direction"] or "归纳岗位技能与经验"
    error = ""
    queue = {}
    selected = []
    for job in jobs:
        if not any(job[field] for field in ("duties", "requirements", "bonus")):
            continue
        if task.get("ai_scope") != "all" and not selection(task, job)[0]:
            continue
        selected.append(job)
        if ai:
            record = analysis_input(job)
            key = fingerprint(ai, direction, record)
            queue.setdefault(key, (record, []))[1].append(job)
    packet_root = folder / "ai"
    packet_root.mkdir(exist_ok=True)
    if not ai:
        records = [dict(source=job["source"], id=job["id"], **analysis_input(job)) for job in selected]
        write_json(packet_root / "input.json", records)
        saved = store.results(db, run_id)
        return {key: result for key, (_, result) in saved.items()}, len(selected), "本次未调用 AI"
    initial_requests, initial_usage = ai.requests, dict(ai.usage)
    discarded_facts = 0
    try:
        pending = []
        for key, (record, aliases) in queue.items():
            cached = db.execute("SELECT payload FROM ai_cache WHERE key=?", (key,)).fetchone()
            if cached is not None:
                validate_analysis({"results": [dict(index=0, **json.loads(cached[0]))]}, [record])
                store.link_results(db, run_id, [(job, key, None) for job in aliases])
            else:
                pending.append(key)
        oversized = 0
        while pending:
            size = min(len(pending), ai.config["batch_size"])
            while True:
                batch = pending[:size]
                records = [queue[key][0] for key in batch]
                payload = ai.analysis_payload(direction, records)
                if len(json.dumps(payload, ensure_ascii=False)) <= ai.config["max_input_chars"] or size == 1:
                    break
                size //= 2
            if len(json.dumps(payload, ensure_ascii=False)) > ai.config["max_input_chars"]:
                oversized += len(queue[batch[0]][1])
                del pending[0]
                continue
            batch_name = digest(batch)[:24]
            write_json(packet_root / f"{batch_name}-input.json", payload)
            checked, discarded = ai.analyze(payload, records)
            discarded_facts += discarded
            write_json(packet_root / f"{batch_name}-validated.json", checked)
            linked = [(job, key, checked[index]) for index, key in enumerate(batch) for job in queue[key][1]]
            store.link_results(db, run_id, linked)
            del pending[:size]
        if oversized:
            error = f"{oversized} 条岗位超过 AI 单次输入字符上限"
    except AIError as exc:
        error = str(exc)
    finally:
        usage = {key: ai.usage[key] - initial_usage[key] for key in initial_usage}
        with db:
            db.execute("INSERT INTO ai_attempts(run_id,requests,usage,error) VALUES(?,?,?,?)",
                       (run_id, ai.requests - initial_requests, dump(usage), error))
    saved = store.results(db, run_id)
    accepted = {(job["source"], job["id"]): saved[(job["source"], job["id"])][1]
                for key, (_, aliases) in queue.items() for job in aliases
                if (job["source"], job["id"]) in saved and saved[(job["source"], job["id"])][0] == key}
    notices = [error] if error else []
    if discarded_facts:
        notices.append(f"本次丢弃 {discarded_facts} 条无法核对或重复的技能\uff0f经验")
    return accepted, len(selected), "\uff0c".join(notices)


def run_observations(db, run, task):
    run_id = run["id"]
    rows = {row["source"]: row for row in db.execute("SELECT * FROM observations WHERE run_id=?", (run_id,))}
    reused = store.reused_observations(db, run_id)
    observed, differences = [], []
    for sid in task["sources"]:
        actual = rows.get(sid)
        cached = reused.get(sid)
        if actual is not None:
            data = json.loads(actual["payload"])
            data.setdefault("observed_at", run["started"])
            data["method"] = "refreshed"
            status = actual["status"]
            prior = store.previous(db, run, sid, actual["scope"])
            differences.extend(changes(task, prior, data["jobs"], status in ("complete", "ai_assisted")))
        if cached is not None and cached["payload"] is not None:
            snapshot = json.loads(cached["payload"])
            snapshot.setdefault("observed_at", cached["started"])
            snapshot.update(method="cached", snapshot_status=cached["status"])
            if actual is not None:
                snapshot.update(method="fallback", attempted_at=data["observed_at"],
                                attempted_pages=data["pages"], attempt_errors=data["errors"])
            else:
                status = cached["status"]
            data = snapshot
        elif actual is None:
            data = dict(company=task.get("source_companies", {}).get(sid, sid), pages=0, jobs={}, errors=[],
                        recovered_fields={}, method="not_collected", observed_at=None)
            status = "not_collected"
        errors = data["attempt_errors"] if data["method"] == "fallback" else data["errors"]
        if issue := coverage_issue(status, errors):
            data["issue"] = issue
        observed.append((sid, status, data))
    return observed, differences


def finish(db, output, run_id, ai):
    run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run or not run["collection_ready"]:
        raise ValueError("运行不存在或上次在采集阶段中断，请重新采集")
    task = json.loads(run["settings"])
    folder = output / "runs" / run_id
    observed, differences = run_observations(db, run, task)
    jobs = [job for _, _, data in observed for job in data["jobs"].values()]
    cleanup = None
    if task.get("ai_clean"):
        cleanup_path = folder / "ai" / "cleaning.json"
        jobs, cleanup = clean_jobs(db, jobs, ai, cleanup_path)
        by_identity = {(job["source"], job["id"]): job for job in jobs}
        observed = [(sid, status, {**data, "jobs": {job_id: by_identity[sid, job_id] for job_id in data["jobs"]}})
                    for sid, status, data in observed]
    cleaned_path = folder / "cleaned.csv"
    write_csv(cleaned_path, CSV_FIELDS, jobs)
    write_json(folder / "changes.json", differences)
    cleaned_rows = read_cleaned_csv(cleaned_path, jobs, CSV_FIELDS)
    accepted, eligible, info = analyze(db, run_id, task, cleaned_rows, ai, folder)
    if task.get("ai_scope") == "all":
        keys = ("source", "company", "id", "title", "url")
        write_json(folder / "ai" / "results.json", [
            {**{key: job[key] for key in keys}, "analysis": accepted.get((job["source"], job["id"])),
             "has_body": any(job[field] for field in ("duties", "requirements", "bonus"))}
            for job in cleaned_rows
        ])
    ai_info = f"{len(accepted)}/{eligible} 条通过验证"
    if task.get("ai_scope") == "all":
        ai_info += f"\uff0c全量分析范围\uff0c{len(jobs) - eligible} 条暂无正文"
    if info:
        ai_info += "\uff0c" + info
    if cleanup is not None:
        ai_info += (f"\uff0c正文分段\uff1a规则 {cleanup['rules']} 条\uff0cAI {cleanup['ai']} 条\uff0c"
                    f"缓存 {cleanup['cached']} 条\uff0c保留混合正文 {cleanup['fallback']} 条")
    if ai:
        ai_info += f"\uff0c本次请求 {ai.requests} 次\uff0c服务返回用量 {dump(ai.usage)}"
    listed = render(folder, run, task, observed, differences, accepted, ai_info)
    summary = dict(run_id=run_id, report=str(folder / "report.md"), scan=task.get("scan", "full"),
                   sources={sid: status for sid, status, _ in observed},
                   collection={sid: dict(pages=data["pages"], collected=len(data["jobs"]),
                                         declared_total=data.get("declared_total"))
                               for sid, _, data in observed},
                   coverage={sid: {key: data[key] for key in ("company", "method", "observed_at",
                                                             "snapshot_status", "attempted_at",
                                                             "attempted_pages", "attempt_errors", "issue") if key in data}
                             for sid, _, data in observed},
                   details={sid: data["details"] for sid, _, data in observed if data.get("details") is not None},
                   cleaned=len(jobs), candidates=listed, changes=len(differences),
                   ai_recovered_jobs=sum(len(data["recovered_fields"]) for _, _, data in observed),
                   ai_validated=len(accepted), ai_eligible=eligible, ai_message=info)
    if ai:
        summary.update(ai_requests=ai.requests, ai_usage=ai.usage)
    if cleanup is not None:
        summary["ai_cleaning"] = {**cleanup, "report": str(cleanup_path)}
    if task.get("ai_scope") == "all":
        summary["ai_scope"] = "all"
        summary["ai_no_body"] = len(jobs) - eligible
        summary["ai_verdicts"] = {verdict: sum(result["verdict"] == verdict for result in accepted.values())
                                  for verdict in ("match", "unrelated", "uncertain")}
    write_json(folder / "summary.json", summary)
    return summary


def run_watch(config, task=None, resume=None, use_ai=False, env_file=None, scan=None):
    output, sources, tasks, settings = load_settings(config)
    if not resume and task not in tasks:
        raise ValueError(f"请选择任务\uff1a{', '.join(tasks)}")
    if resume and not RUN_ID.fullmatch(resume):
        raise ValueError("运行 ID 无效")
    if scan not in (None, "quick", "full") or (scan and resume):
        raise ValueError("--scan 只适用于新的采集或搜索任务")
    if use_ai and not settings:
        raise ValueError("请先在配置文件添加 ai 字段")
    ai = AI_CLIENTS[settings["provider"]](settings, env_file) if use_ai else None
    with project_lock(output / 'watch.sqlite3'), closing(store.connect(output)) as db:
        run_id = resume or new_run(db, output, task, tasks[task], sources, ai, scan)
        return finish(db, output, run_id, ai)


def demo(output):
    output.mkdir(parents=True, exist_ok=True)
    workspace = output / ("sample-" + uuid.uuid4().hex[:10])
    workspace.mkdir()
    data = files("jobwatch").joinpath("demo_data")
    for name in ("registry.json", "watch.json"):
        (workspace / name).write_bytes(data.joinpath(name).read_bytes())
    summaries = []
    for round_number in (1, 2):
        for company in ("studio", "cloud"):
            (workspace / f"{company}.json").write_bytes(
                data.joinpath(f"{company}-{round_number}.json").read_bytes()
            )
        for task in ("ai-apps", "studio-watch"):
            summaries.append(run_watch(workspace / "watch.json", task))
    return dict(sample=str(workspace), runs=summaries)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="通用招聘岗位观察")
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("discover", help="从牛客公开页面发现企业招聘入口")
    discovery.add_argument("urls", nargs="*", help="牛客公开页面地址")
    discovery.add_argument("--company", action="append", help="在校招日程返回页筛企业名，可重复")
    discovery.add_argument("--output", type=Path, required=True)
    onboarding = commands.add_parser("onboard", help="识别和试采招聘链接，生成可运行的来源配置")
    onboarding.add_argument("urls", nargs="*", help="招聘官网或托管招聘板链接")
    onboarding.add_argument("--company", help="企业显示名，使用同一企业的链接时可指定")
    onboarding.add_argument("--from-leads", type=Path, help="从企业发现结果 leads.json 批量读取外部链接")
    onboarding.add_argument("--registry", type=Path, help="将已通过试采的来源加入指定现有清单")
    onboarding.add_argument("--access-reviewed", action="store_true", help="已核实来源允许自动访问")
    onboarding.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check", help="离线检查配置")
    check.add_argument("--config", type=Path, required=True)
    commands.add_parser("browser-check", help="启动 Chromium 并离线检查 JavaScript 渲染")
    example = commands.add_parser("demo", help="运行两轮离线样例")
    example.add_argument("--output", type=Path, default=Path("demo-output"))
    run = commands.add_parser("run", help="采集、清洗、比较并生成报告")
    run.add_argument("--config", type=Path, required=True)
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--task")
    mode.add_argument("--resume")
    run.add_argument("--scan", choices=("quick", "full"), help="快速查找或完整扫描，默认按任务配置")
    run.add_argument("--ai", action="store_true", help="启用外部 AI API")
    run.add_argument("--env-file", type=Path, help="显式指定本机密钥文件，环境变量优先")
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            from .discover import discover
            result = discover(args.urls, args.output, companies=args.company or ())
        elif args.command == "onboard":
            from .onboard import onboard
            if not args.access_reviewed:
                raise ValueError("请先核实招聘来源的自动访问规则，再传入 --access-reviewed")
            if args.from_leads and (args.urls or args.company):
                raise ValueError("--from-leads 单独提供企业名和链接")
            result = onboard(args.urls, args.output, company=args.company, leads=args.from_leads,
                             registry_path=args.registry)
        elif args.command == "check":
            output, sources, tasks, ai = load_settings(args.config)
            for sid, source in sources.items():
                if source["adapter"] == "json_file" and not Path(source["path"]).is_file():
                    raise ValueError(f"{sid} 的本地输入不存在")
            result = dict(output=str(output), sources=list(sources), tasks=list(tasks), ai_configured=bool(ai))
        elif args.command == "browser-check":
            from .browser import browser_check
            result = browser_check()
        elif args.command == "demo":
            result = demo(args.output.resolve())
        else:
            if args.env_file and not args.ai:
                raise ValueError("--env-file 须与 --ai 一起使用")
            result = run_watch(args.config, args.task, args.resume, args.ai, args.env_file, args.scan)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, sqlite3.Error, etree.LxmlError) as error:
        parser.exit(1, f"执行失败\uff1a{error}\n")
    if args.command == "discover":
        if any(row["status"] == "failed" for key in ("pages", "company_matches") for row in result.get(key, [])):
            return 2
    if args.command == "onboard":
        update = result.get("registry_update", {})
        if update.get("error") or update.get("conflicts") or any(row["status"] != "ready" for row in result["results"]):
            return 2
    if args.command == "run":
        if any(status in ("partial", "failed") and
               (result["coverage"][sid]["method"] != "cached" or result["coverage"][sid].get("issue") == "error")
               for sid, status in result["sources"].items()):
            return 2
        if args.ai and result["ai_validated"] < result["ai_eligible"]:
            return 2
    return 0
