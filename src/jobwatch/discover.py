"""Find employer recruitment links on public Nowcoder pages."""
from html import unescape
import re
from pathlib import Path
import unicodedata
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from .cleaning import text, web_url
from .common import project_lock, write_json
from .sources import WebSource, lookup, parse_html, parse_json

NOWCODER_ORIGIN = "https:" "//www.nowcoder" ".com"
NOWCODER = re.compile("https:" r"//www\.nowcoder" r"\.com/(?:enterprise/\d+(?:/discussion)?|discuss/\d+|feed/main/detail/[a-f0-9]+)(?:\?.*)?$")
ENTERPRISE = re.compile("https:" r"//www\.nowcoder" r"\.com/enterprise/([0-9]+)(?:\?[^#\s]*)?")
SCHEDULE = re.compile("https:" r"//www\.nowcoder" r"\.com/(?:jobs/)?school/schedule/?(?:\?[^#\s]*)?")
SCHEDULE_PATH = "/jobs/school/schedule"
SCHEDULE_URL = NOWCODER_ORIGIN + SCHEDULE_PATH
SCHEDULE_SCRIPT = {"json_script": {"xpath": "//script[not(@src)]/text()", "prefix": "window.__INITIAL_STATE__="}}
LINK = re.compile(r"https?:" r"//[A-Za-z0-9.:-]+(?:[/?#][A-Za-z0-9\-._~:/?#@!$&()*+,;=%\[\]]*)?", re.I)
PLAINTEXT_END = re.compile(r"(?<=[a-z0-9])(?=[A-Z][a-z])")
ESCAPED_SLASH = re.compile(r"\\u002[fF]|\\/")
TRACKING = {"entityid", "code", "token", "share_token", "sourcetoken", "spread", "ref", "referralcode"}


def clean_query(value):
    params = [(key, item) for key, item in parse_qsl(value, keep_blank_values=True)
              if key.lower() not in TRACKING and not key.lower().startswith("utm_")]
    return urlencode(params)


def clean_link(parts):
    path, sep, query = parts.fragment.partition("?")
    fragment = path + ("?" + cleaned if sep and (cleaned := clean_query(query)) else "")
    return urlunsplit(parts._replace(query=clean_query(parts.query), fragment=fragment))


def nowcoder_host(host):
    return host == "nowcoder" ".com" or host.endswith(".nowcoder" ".com")


def career_links(data):
    doc = parse_html(data)
    fields = [(value, True) for value in doc.xpath("//a/@href")]
    fields.extend((value, False) for value in doc.xpath("//body//text()[not(ancestor::style)]"))
    links = set()
    for value, exact in fields:
        decoded = ESCAPED_SLASH.sub("/", str(value))
        decoded = unquote(decoded) if re.search(r"https?%3a%2f%2f", decoded, re.I) else decoded
        for match in LINK.findall(unescape(decoded)):
            match = match.rstrip("),.;")
            if not exact and "/jobs/" in match:
                match = PLAINTEXT_END.split(match, maxsplit=1)[0]
            try:
                parts = urlsplit(web_url(match))
            except ValueError:
                continue
            host = parts.hostname
            if not host or nowcoder_host(host):
                continue
            if not (re.search(r"(?:^|\.)(?:jobs?|careers?|campus|recruit|zhaopin)\.", host, re.I)
                    or re.search(r"/(?:jobs?|careers|campus|recruit(?:ment)?)(?:/|$)", parts.path, re.I)):
                continue
            if parts.path.startswith("/referral/") and not clean_query(parts.query):
                links.add(urlunsplit(parts._replace(path="/", query="", fragment="")))
                continue
            links.add(clean_link(parts))
    return sorted(links)


def schedule_link(value):
    if not isinstance(value, str) or not value.strip():
        return "", "missing"
    try:
        parts = urlsplit(web_url(unescape(value.strip()), SCHEDULE_URL))
        if not parts.hostname:
            return "", "missing"
        parts.port
    except ValueError:
        return "", "missing"
    if nowcoder_host(parts.hostname):
        kind = "nowcoder"
    elif parts.hostname == "mp.weixin.qq" ".com":
        kind = "announcement"
    else:
        kind = "external"
    return clean_link(parts), kind


def schedule_leads(data):
    state = parse_json(data, SCHEDULE_SCRIPT)
    app = state.get("app") if isinstance(state, dict) else None
    schedules = [node["scheduleData"] for node in app.values()
                 if isinstance(node, dict) and "scheduleData" in node] if isinstance(app, dict) else []
    if len(schedules) != 1 or not isinstance(schedules[0], dict):
        raise ValueError("牛客校招日程未找到唯一的企业列表")
    schedule = schedules[0]
    records = schedule.get("datas")
    total = schedule.get("totalCount")
    if not isinstance(records, list) or (not records and (type(total) is not int or total != 0)):
        raise ValueError("牛客校招日程企业记录缺失，无法确认空结果")
    companies = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("牛客校招日程企业记录格式不正确")
        name, company_id = record.get("name"), record.get("companyId")
        if not isinstance(name, str) or not name.strip() or type(company_id) not in (str, int):
            raise ValueError("牛客校招日程企业记录缺少名称或 ID")
        if not re.fullmatch(r"[0-9]+", str(company_id)):
            raise ValueError("牛客校招日程企业 ID 格式不正确")
        link, kind = schedule_link(record.get("customWangshenLink"))
        companies.append({"company": name.strip(), "company_id": str(company_id), "url": link, "link_type": kind})
    links = sorted({row["url"] for row in companies if row["link_type"] == "external"})
    return {
        "candidates": links,
        "companies": companies,
        "schedule": {"page": schedule.get("currentPage"), "total_companies": total,
                     "total_pages": schedule.get("totalPage")},
    }


def search_key(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def enterprise_url(company_id):
    return f"{NOWCODER_ORIGIN}/enterprise/{company_id}"


def recruitment_link(value):
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(web_url(unescape(value), NOWCODER_ORIGIN))
    except ValueError:
        return ""
    if parts.netloc == "www.nowcoder" ".com" and parts.path == "/jump":
        targets = parse_qs(parts.query).get("url", [])
        if len(targets) != 1:
            return ""
        value = targets[0]
    link, kind = schedule_link(value)
    return link if kind == "external" else ""


def enterprise_lead(raw, company_id):
    info = lookup(parse_json(raw, SCHEDULE_SCRIPT), "store.enterprise.enterpriseInfo")
    if not isinstance(info, dict) or str(info.get("companyId")) != company_id or not text(info.get("name")):
        raise ValueError("牛客企业页未返回与企业 ID 一致的公开数据")
    button = info.get("buttonInfo")
    urls = [info.get("officalEncodeUrl")]
    if isinstance(button, dict) and button.get("showButton") and button.get("name") == "官网投递":
        urls.insert(0, button.get("url"))
    return dict(company=text(info["name"]), company_id=company_id,
                candidates=list(dict.fromkeys(link for value in urls if (link := recruitment_link(value)))))


def discover_companies(pages, queries, site, folder):
    companies = {}
    for page in pages:
        for record in page.get("companies", []):
            cid = record["company_id"]
            name = search_key(record["company"])
            matched = [query for query in queries if search_key(query) in name]
            if not matched:
                continue
            item = companies.setdefault(cid, dict(company=record["company"], company_id=cid,
                                                  company_url=enterprise_url(cid), matched_queries=[],
                                                  candidates=[], discovered_via=[]))
            for query in matched:
                if query not in item["matched_queries"]:
                    item["matched_queries"].append(query)
            if page["source"] not in item["discovered_via"]:
                item["discovered_via"].append(page["source"])
            links = record.get("candidates", [])
            if "url" in record and (link := recruitment_link(record["url"])):
                links = [link]
            item["candidates"] = list(dict.fromkeys([*item["candidates"], *links]))
    attempts = 0
    for item in companies.values():
        if item["candidates"]:
            item["status"] = "found"
        elif any(ENTERPRISE.fullmatch(url) for url in item["discovered_via"]):
            item["status"] = "no_links"
        elif attempts < 20:
            attempts += 1
            try:
                raw = site.get(item["company_url"], folder / f"company-{item['company_id']}.html")
                item["candidates"] = enterprise_lead(raw, item["company_id"])["candidates"]
                item["status"] = "found" if item["candidates"] else "no_links"
            except (OSError, ValueError) as error:
                item.update(status="failed", error=str(error))
        else:
            item["status"] = "not_checked"
    return list(companies.values())


def discover(urls, output, *, companies=()):
    if len(companies) > 20 or any(not search_key(name) or len(name) > 80 for name in companies):
        raise ValueError("每批最多 20 个企业名，企业名须为 1 至 80 个字符")
    if companies:
        urls = urls or [SCHEDULE_URL]
        valid_url = lambda url: SCHEDULE.fullmatch(url) or ENTERPRISE.fullmatch(url)
    else:
        valid_url = lambda url: NOWCODER.fullmatch(url) or SCHEDULE.fullmatch(url)
    if not urls or len(urls) > 20 or any(not valid_url(url) for url in urls):
        if companies:
            raise ValueError("企业搜索每批最多接受 20 个牛客校招日程或企业页地址")
        raise ValueError("每批最多 20 条，须为牛客公开校招日程、企业页或帖子 URL")
    output = Path(output)
    with project_lock(output / "leads.json"):
        return discover_pages(urls, output, companies)


def discover_pages(urls, output, companies):
    raw = output / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    site = WebSource({"url": NOWCODER_ORIGIN, "interval_seconds": 3})
    pages = []
    for index, url in enumerate(dict.fromkeys(urls), 1):
        try:
            is_schedule = bool(SCHEDULE.fullmatch(url))
            target = urlunsplit(urlsplit(url)._replace(path=SCHEDULE_PATH)) if is_schedule else url
            data = site.get(target, raw / f"nowcoder-{index:02}.html")
            if is_schedule:
                result = schedule_leads(data)
            elif companies and (enterprise := ENTERPRISE.fullmatch(url)):
                canonical = enterprise_url(enterprise[1])
                try:
                    lead = enterprise_lead(data, enterprise[1])
                except ValueError:
                    if target == canonical:
                        raise
                    target = canonical
                    data = site.get(target, raw / f"nowcoder-{index:02}-canonical.html")
                    lead = enterprise_lead(data, enterprise[1])
                result = dict(candidates=lead["candidates"], companies=[lead])
            else:
                result = {"candidates": career_links(data)}
            status = "found" if result["candidates"] else "no_links"
            if target != url and not is_schedule:
                result["retrieved_url"] = target
            pages.append({"source": url, "status": status, **result})
        except (OSError, ValueError) as error:
            pages.append({"source": url, "status": "failed", "error": str(error), "candidates": []})
    result = dict(pages=pages)
    sources = pages
    if companies:
        matches = discover_companies(pages, list(dict.fromkeys(companies)), site, raw)
        result.update(mode="company", scope="public_pages", queries=list(companies), company_matches=matches)
        sources = matches
    result["candidate_count"] = len({link for source in sources for link in source["candidates"]})
    write_json(output / "leads.json", result)
    return result
