"""Selection, deterministic snapshot comparison and exports."""
from collections import Counter
import csv
import re

from .common import normalized_proof

STATUSES = {"complete": "本地完成", "ai_assisted": "AI 辅助完成",
            "partial": "部分完成", "failed": "查询失败", "not_collected": "本次未查询，暂无本地数据"}
SCAN_MODES = {"quick": "快速查找", "full": "完整扫描"}
LABELS = {"baseline": "基线", "new": "新发现", "modified": "修改",
          "missing": "本次未检出", "unknown": "本次无法判断"}
BODY_NEGATION = (r"(?<!并非)(?<!不是)(?<!绝非)(?:不需要|不要求|无需|无须|不用|不必|不)"
                 r"(?:负责|涉及|参与|从事|承担|开展|进行|具备|掌握|熟悉|有|做)?")
BODY_CLAUSE = re.compile(r"([\uff0c,\u3002\uff01\uff1f!?\uff1b;\uff1a:\r\n]|\.(?=\s|$)|\s+/\s+)")
BODY_COORDINATION = re.compile(
    r"(?:并且?|以及|同时|另行|而且|但(?:是)?|和|及|与)"
    r"(?=开发|研发|集成|搭建|构建|设计|实现|交付|部署|运维|运营|监控|维护|测试|负责|承担|参与)"
)
DELIVERY = re.compile(r"(?:负责|承担).+(?:交付|实施|落地)$")
DELIVERY_LIST = re.compile(r"^(?:具体)?(?:包括|包含|涵盖)(?:其)?")
LIST_SEPARATOR = re.compile(r"$|[\u3001/]|以及|并|和|及|与")


def token(value):
    return normalized_proof(value).casefold()


def lookup_terms(haystack, terms):
    normalized = token(haystack)
    return [term for term in terms if token(term) in normalized]


def lookup_body_terms(body, terms):
    normalized = token(body)
    matches = []
    for term in terms:
        pattern = rf"(?P<negation>{BODY_NEGATION})?{re.escape(token(term))}"
        if any(hit.group("negation") is None for hit in re.finditer(pattern, normalized)):
            matches.append(term)
    return matches


def delivery_list_action(clause, contexts):
    addition = DELIVERY_LIST.match(clause)
    if not addition:
        return ""
    for context in contexts:
        action = token(context)
        end = addition.end() + len(action)
        if clause.startswith(action, addition.end()) and LIST_SEPARATOR.match(clause, end):
            return context
    return ""


def body_candidate_reason(job, rules):
    for field in ("duties", "requirements"):
        fragments = BODY_CLAUSE.split(job[field])
        delivery = ""
        for index in range(0, len(fragments), 2):
            clause = token(fragments[index])
            segments = BODY_COORDINATION.split(clause)
            for rule in rules:
                if delivery and (context := delivery_list_action(clause, rule["context"])):
                    if terms := lookup_body_terms(delivery, rule["terms"]):
                        return f"{terms[0]} + {context}"
                for position, segment in enumerate(segments):
                    terms = lookup_body_terms(segment, rule["terms"])
                    if not terms:
                        continue
                    if context := lookup_body_terms(segment, rule["context"]):
                        return f"{terms[0]} + {context[0]}"
                    if position:
                        prior = segments[position - 1]
                        shared = [word for word in rule["context"] if prior == token(word)]
                        if shared:
                            return f"{terms[0]} + {shared[0]}"
            delivery = ""
            if index + 1 < len(fragments) and fragments[index + 1] in "\uff0c,\uff1a:":
                tail = segments[-1]
                if DELIVERY.search(tail) and lookup_body_terms(tail, ("负责", "承担")):
                    delivery = tail
    return ""


def selection(task, job):
    if job["url"] in task["pinned_urls"]:
        return "关注岗位", job["url"]
    for field, setting in (("location", "cities"), ("type", "types")):
        if task[setting] and not lookup_terms(job[field], task[setting]):
            return "", ""
    if task["mode"] == "company":
        return "企业来源", job["company"]
    title_hits = lookup_terms(job["title"], task["keywords"])
    if title_hits:
        return "标题匹配", "\u3001".join(title_hits)
    for field in ("duties", "requirements"):
        mentions = lookup_body_terms(job[field], task["keywords"])
        if mentions:
            return "正文候选", "\u3001".join(mentions)
    rules = task.get("body_candidate_rules", [])
    if rules and (reason := body_candidate_reason(job, rules)):
        return "正文候选", reason
    wider = lookup_terms(job["title"], task["candidate_keywords"])
    return ("待分析候选", "\u3001".join(wider)) if wider else ("", "")


def changes(task, before, after, complete):
    result = []
    for job_id in sorted((before or {}).keys() | after.keys()):
        prior, current = (before or {}).get(job_id), after.get(job_id)
        if not any(selection(task, job)[0] for job in (prior, current) if job):
            continue
        fields = []
        if prior is None:
            kind = "baseline" if before is None else "new"
        elif current is None:
            kind = "missing" if complete else "unknown"
        else:
            fields = [key for key in current if normalized_proof(current[key]) != normalized_proof(prior[key]) or
                      (key in ("url", "status") and current[key] != prior[key])]
            if not fields:
                continue
            kind = "modified" if complete else "unknown"
        chosen = current or prior
        result.append(dict(source=chosen["source"], company=chosen["company"], id=job_id,
                           title=chosen["title"], url=chosen["url"], change=LABELS[kind],
                           fields="\u3001".join(fields), before=prior, after=current))
    return result


def csv_cell(value):
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or
                                   value.startswith(("\t", "\r", "\n", "'"))):
        return "'" + value
    return value


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: csv_cell(row.get(key, "")) for key in fields} for row in rows)


def read_cleaned_csv(path, rows, fields):
    largest_field = max((len(csv_cell(row.get(field, ""))) for row in rows for field in fields), default=0)
    previous_limit = csv.field_size_limit()
    if largest_field > previous_limit:
        csv.field_size_limit(largest_field)
    try:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            return [{key: value.removeprefix("'") for key, value in row.items()}
                    for row in csv.DictReader(stream)]
    finally:
        if largest_field > previous_limit:
            csv.field_size_limit(previous_limit)


def md(value):
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", " ".join(str(value).split()))


def render(folder, run, task, observed, differences, analyses, ai_summary):
    job_fields = ("source", "company", "id", "title", "type", "location", "status", "url",
                  "duties", "requirements", "bonus", "match", "reason", "ai_verdict", "ai_recovered_fields")
    change_fields = ("source", "company", "id", "title", "url", "change", "fields")
    mode = "单企业" if task["mode"] == "company" else f"跨企业岗位 \u00b7 {SCAN_MODES[task.get('scan', 'full')]}"
    lines = [f"# 岗位观察 \u00b7 {md(run['task'])}", "",
             f"- 模式\uff1a{mode}",
             f"- 观测时间\uff1a{md(run['started'])}",
             f"- 岗位方向\uff1a{md(task['direction'] or '全部')}",
             f"- AI\uff1a{md(ai_summary)}", "", "## 岗位变化", ""]
    for item in differences:
        lines.append(f"- **{md(item['change'])}** {md(item['company'])} \u00b7 {md(item['title'])}"
                     + (f"\uff08{md(item['fields'])}\uff09" if item["fields"] else ""))
    if not differences:
        lines.append("- 此次没有可报告的变化")
    lines.extend(["", "## 岗位和匹配理由", ""])
    rows, explained, shown = [], 0, 0
    counts = Counter()
    fact_evidence = {}
    matched_sources = Counter()
    for source, status, payload in observed:
        for job in payload["jobs"].values():
            why, reason = selection(task, job)
            analysis = analyses.get((source, job["id"]))
            if analysis and analysis["verdict"] == "match" and why in ("", "正文候选", "待分析候选"):
                why, reason = "职责匹配", analysis["evidence"]
            if analysis and analysis["verdict"] == "uncertain" and not why:
                why, reason = "待分析候选", analysis["evidence"] or "AI 判断职责证据不足"
            if not why:
                continue
            verdict = analysis["verdict"] if analysis else ""
            recovered = payload["recovered_fields"].get(job["id"], [])
            rows.append(dict(job, match=why, reason=reason, ai_verdict=verdict,
                             ai_recovered_fields="\u3001".join(recovered)))
            if verdict == "unrelated" and task["mode"] == "role" and why != "关注岗位":
                continue
            if verdict != "uncertain" and why not in ("正文候选", "待分析候选"):
                matched_sources[source] += 1
            shown += 1
            lines.append(f"- **{md(job['company'])} \u00b7 {md(job['title'])}**"
                         f"\uff08{md(why)}\uff1a{md(reason)}\uff1b{md(job['status'])}\uff09")
            if recovered:
                lines.append(f"  - AI 辅助解析字段\uff1a{md('\u3001'.join(recovered))}")
            if job["url"]:
                lines.append(f"  - <{job['url'].replace('<', '%3C').replace('>', '%3E')}>")
            if analysis and (verdict != "unrelated" or task["mode"] == "company"):
                explained += 1
                if analysis["evidence"] and why != "职责匹配":
                    lines.append(f"  - AI 判断\uff1a{md(verdict)} \u00b7 \u300c{md(analysis['evidence'])}\u300d")
                facts = set()
                for fact in analysis["facts"]:
                    key = (fact["kind"], fact["field"], fact["name"].casefold())
                    facts.add(key)
                    fact_evidence.setdefault(key, (job["title"], fact["evidence"], job["url"]))
                counts.update(facts)
    if not shown:
        lines.append("- 本次没有可展示的匹配或待分析候选")
    lines.extend(["", "## 来源完成情况", ""])
    for source, status, payload in observed:
        if status == "not_collected":
            lines.append(f"- **{md(payload['company'])} \u00b7 {md(source)}**\uff1a{STATUSES[status]}")
            continue
        total = payload.get("declared_total")
        coverage = f"{len(payload['jobs'])}/{total}" if total is not None else str(len(payload["jobs"]))
        method = payload.get("method", "refreshed")
        if method == "fallback":
            label = f"本次刷新{STATUSES[status]}\uff0c读取本地快照\uff08{STATUSES[payload['snapshot_status']]}\uff09"
        elif method == "cached":
            label = f"读取本地快照\uff08{STATUSES[status]}\uff09"
        else:
            label = f"本次采集 \u00b7 {STATUSES[status]}"
        if payload.get("issue") == "error":
            label += " \u00b7 采集异常"
        elif payload.get("issue") == "limit":
            label += " \u00b7 达到配置上限"
        lines.append(f"- **{md(payload['company'])} \u00b7 {md(source)}**\uff1a{label}\uff0c{payload['pages']} 页\uff0c"
                     f"{coverage} 条原始去重岗位\uff0c已确认匹配 {matched_sources[source]} 条\uff0c"
                     f"AI 辅助解析 {len(payload['recovered_fields'])} 条")
        if payload.get("observed_at"):
            lines.append(f"  - 本批岗位数据采集于 {md(payload['observed_at'])}")
        if method == "fallback":
            lines.append(f"  - 最近刷新尝试于 {md(payload['attempted_at'])} 结束\uff0c共读取 {payload['attempted_pages']} 页")
            lines.extend(f"  - 本次刷新\uff1a{md(error)}" for error in payload["attempt_errors"])
        if details := payload.get("details"):
            lines.append(f"  - 详情采集\uff1a成功 {details['success']}\uff0c失败 {details['failed']}\uff0c"
                         f"未尝试 {details['skipped']}")
        lines.extend(f"  - {md(error)}" for error in payload["errors"])
    lines.extend(["", "## 技能与经验", "",
                  f"统计范围\uff1a{explained} 条已展示且通过 AI 分析校验的岗位。", ""])
    fields = {"duties": "职责", "requirements": "要求", "bonus": "加分项"}
    for key, count in counts.most_common():
        kind, field, name = key
        title, evidence, link = fact_evidence[key]
        location = f" \u00b7 <{link.replace('<', '%3C').replace('>', '%3E')}>" if link else ""
        lines.append(f"- {md(name)}\uff08{'技能' if kind == 'skill' else '经验'}\uff0f{fields[field]}\uff09"
                     f" {count}/{explained}\u3002{md(title)}\uff1a\u300c{md(evidence)}\u300d{location}")
    write_csv(folder / "jobs.csv", job_fields, rows)
    write_csv(folder / "changes.csv", change_fields, differences)
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)
