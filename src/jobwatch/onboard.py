"""Probe public recruitment links and produce runnable source configurations."""

from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import uuid

from lxml import etree

from .cleaning import web_url
from .common import digest, project_lock, read_json, write_json
from .config import validate_source
from .sources import CAP_ERRORS, WebSource, collect_pages, origin, parse_html


INTERVAL = 3
TRIAL_PAGES = 2
TRIAL_DETAILS = 2
TOKEN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
LEVER = {"jobs.lever" ".co": "api.lever" ".co", "jobs.eu.lever" ".co": "api.eu.lever" ".co",
         "api.lever" ".co": "api.lever" ".co", "api.eu.lever" ".co": "api.eu.lever" ".co"}
GREENHOUSE = {"boards.greenhouse" ".io", "job-boards.greenhouse" ".io", "boards-api.greenhouse" ".io"}
ASHBY = {"jobs.ashbyhq" ".com", "api.ashbyhq" ".com"}
JOB_ITEMS = "//*[@itemscope and (@itemtype='https:" "//schema" ".org/JobPosting' or @itemtype='http:" "//schema" ".org/JobPosting')]"
NEXT_LINK = "//a[@rel='next']/@href | //link[@rel='next']/@href"
CAREERS_PATH = re.compile(r"/(?:careers?|jobs?|recruit(?:ment)?|zhaopin|join[-_]us|positions?)/?$", re.I)


def provider_profile(url):
    parts = urlsplit(url)
    if parts.scheme != "https":
        return None
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    if host == "talent.baidu" ".com" and path in ("/jobs", "/jobs/list") and not parts.fragment:
        filters = parse_qs(parts.query)
        if filters.get("search") or filters.get("projectType") or filters.get("recruitType", ["GRADUATE"]) != ["GRADUATE"]:
            return None
        return "baidu", "baidu", dict(
            url="https:" "//talent.baidu" ".com/httservice/getPostListNew", careers_url="https:" "//talent.baidu" ".com/jobs/list",
            max_pages=200, items_path="data.list", total_path="data.total",
            pagination=dict(page_param="curPage", size_param="pageSize", page_size=10),
            query_form=dict(fields=dict(recruitType="GRADUATE", keyWord="", projectType=""),
                            success_path="status", success_value="ok", page_path="data.pageNum"),
            job_url_template="/jobs/detail/GRADUATE/{id}",
            fields=dict(id="postId", title="name", location="workPlace", type="projectType",
                        duties="workContent", requirements="serviceCondition"),
        )
    if host in GREENHOUSE:
        if host == "boards-api.greenhouse" ".io":
            match = re.fullmatch(rf"/v1/boards/({TOKEN})/jobs", path)
        elif path in ("/embed/job_board", "/embed/job_board/js"):
            tokens = parse_qs(parts.query).get("for", [])
            match = re.fullmatch(rf"({TOKEN})", tokens[0]) if len(tokens) == 1 else None
        else:
            match = re.fullmatch(rf"/({TOKEN})(?:/jobs/[0-9]+)?", path)
        if match:
            tenant = match[1]
            api_path = f"/v1/boards/{tenant}/jobs"
            source = dict(url="https:" "//boards-api.greenhouse" f".io{api_path}",
                          items_path="jobs", total_path="meta.total",
                          fields=dict(id="id", title="title", url="absolute_url", location="location.name"),
                          detail_url_template=api_path + "/{id}", detail_id_path="id",
                          detail_fields=dict(duties="content"))
            return "greenhouse", tenant, source
    elif host in LEVER:
        pattern = rf"/v0/postings/({TOKEN})" if host.startswith("api.") else rf"/({TOKEN})(?:/[a-fA-F0-9-]{{36}})?"
        if match := re.fullmatch(pattern, path):
            tenant = match[1]
            source = dict(url="https:" f"//{LEVER[host]}/v0/postings/{tenant}?mode=json",
                          pagination=dict(offset_param="skip", size_param="limit", page_size=50),
                          fields=dict(id="id", title="text", url="hostedUrl",
                                      location="categories.location", type="categories.commitment",
                                      duties="descriptionPlain"))
            return "lever", tenant, source
    elif host in ASHBY:
        pattern = rf"/posting-api/job-board/({TOKEN})" if host.startswith("api.") else rf"/({TOKEN})(?:/[a-fA-F0-9-]{{36}})?"
        if match := re.fullmatch(pattern, path):
            tenant = match[1]
            if host == "jobs.ashbyhq" ".com":
                source = dict(url="https:" "//jobs.ashbyhq" f".com/{tenant}", items_path="jobBoard.jobPostings",
                              json_script={"xpath": "//script/text()", "prefix": "window.__appData ="},
                              fields=dict(id="id", title="title", location="locationName", type="employmentType"),
                              detail_url_template=f"/{tenant}/" + "{id}", detail_id_path="posting.id",
                              detail_fields=dict(duties="posting.descriptionHtml"))
                return "ashby", tenant, source
            source = dict(url="https:" "//api.ashbyhq" f".com/posting-api/job-board/{tenant}",
                          items_path="jobs", listed_path="isListed",
                          fields=dict(title="title", url="jobUrl", location="location",
                                      type="employmentType", duties="descriptionHtml"))
            return "ashby", tenant, source
    return None


def usable_link(value, base):
    try:
        url = web_url(value, base)
        urlsplit(url).port
        return url
    except ValueError:
        return ""


def web_for(url, clients):
    key = origin(url)
    if key not in clients:
        clients[key] = WebSource({"url": url, "interval_seconds": INTERVAL})
    return clients[key]


def html_profile(tree, url):
    items = tree.xpath(JOB_ITEMS)
    if len(items) < 2 and not (
        items and (tree.xpath(NEXT_LINK) or tree.xpath(
            "//*[@itemtype='https:" "//schema" ".org/ItemList' or @itemtype='http:" "//schema" ".org/ItemList']"))
    ):
        return None
    return "html_jobposting", urlsplit(url).hostname, dict(
        adapter="html_http", url=url, items_xpath=JOB_ITEMS, next_xpath=NEXT_LINK,
        fields=dict(title="string((.//*[@itemprop='title']/@content | .//*[@itemprop='title'])[1])",
                    url="(.//*[@itemprop='url']/@href | .//*[@itemprop='url']/@content)[1]",
                    description="string((.//*[@itemprop='description']/@content | .//*[@itemprop='description'])[1])"),
    )


def identify(url, raw_folder, clients):
    visited = set()
    for step in range(3):
        if url in visited:
            return None, url, "招聘入口跳转重复"
        visited.add(url)
        if profile := provider_profile(url):
            return profile, url, ""
        try:
            raw = web_for(url, clients).get(url, raw_folder / f"entry-{step + 1}.html")
        except HTTPError as error:
            target = usable_link(error.headers.get("Location", ""), url) if error.headers else ""
            if error.code in (301, 302, 303, 307, 308) and target and (
                origin(target) == origin(url) or provider_profile(target)
            ):
                url = target
                continue
            raise
        tree = parse_html(raw)
        links = list(dict.fromkeys(link for value in tree.xpath("//a/@href | //iframe/@src")
                                   if (link := usable_link(value, url))))
        providers = {}
        embed_scripts = [link for value in tree.xpath("//script/@src") if (link := usable_link(value, url))
                         and urlsplit(link).netloc.lower() in GREENHOUSE
                         and urlsplit(link).path == "/embed/job_board/js"]
        for link in [*links, *embed_scripts]:
            if profile := provider_profile(link):
                providers[profile[2]["url"]] = (profile, link)
        if len(providers) == 1:
            profile, link = next(iter(providers.values()))
            return profile, link, ""
        if len(providers) > 1:
            return None, url, "页面包含多个不同招聘板，请指定目标招聘板链接"
        if profile := html_profile(tree, url):
            return profile, url, ""
        next_pages = [link for link in links if link not in visited and origin(link) == origin(url)
                      and not urlsplit(link).fragment and CAREERS_PATH.search(urlsplit(link).path)]
        if len(next_pages) != 1:
            return None, url, "未识别可验证的岗位列表，请提供实际列表地址或适配配置"
        url = next_pages[0]
    return None, url, "在试采入口查找范围内未识别岗位列表"


def lead_entries(path):
    data = read_json(path)
    if "company_matches" in data:
        entries = [(item["company"], link, item["company_url"])
                   for item in data["company_matches"] for link in item["candidates"]]
    else:
        entries = [(row["company"], row["url"], page["source"])
                   for page in data.get("pages", []) for row in page.get("companies", [])
                   if row.get("link_type") == "external"]
    unique = {}
    for company, link, evidence in entries:
        address = usable_link(link, "")
        profile = provider_profile(address) if address else None
        key = company, profile[2]["url"] if profile else link
        unique.setdefault(key, (company, link, evidence))
    return list(unique.values())


def trial_input(company, url, evidence, folder, clients):
    row = dict(company=company or "", input_url=url)
    raw = folder / "entry"
    raw.mkdir(parents=True)
    try:
        profile, entry, reason = identify(url, raw, clients)
        if profile is None:
            return dict(row, status="needs_adapter", reason=reason, entry_url=entry), None
        platform, tenant, mapping = profile
        company = company or tenant
        row.update(company=company, entry_url=entry, platform=platform, fetch_url=mapping["url"])
        sid = re.sub("[^a-z0-9_-]+", "-", tenant.lower()).strip("-_")[:42] or "company"
        sid = f"{sid}-{digest(mapping['url'])[:8]}"
        source = validate_source({"adapter": "json_http", **mapping, "id": sid, "company": company,
                                  "access_reviewed": True, "interval_seconds": INTERVAL,
                                  "careers_url": mapping.get("careers_url", entry), "discovered_via": evidence or url}, folder)
        client = web_for(source["url"], clients)
        trial = dict(source, max_pages=TRIAL_PAGES, max_details=TRIAL_DETAILS)
        trial_folder = folder / "trial"
        trial_folder.mkdir()
        client.check_access(source["url"], trial_folder)
        attempt = collect_pages(trial, trial_folder, None, web=client)
    except (OSError, ValueError, etree.LxmlError) as error:
        return dict(row, status="access_failed", reason=str(error)), None
    row.update(pages=attempt.pages, jobs=len(attempt.jobs), list_complete=attempt.list_complete,
               jobs_with_body=sum(bool(job["duties"] or job["requirements"]) for job in attempt.jobs.values()),
               details=attempt.details, notes=attempt.errors)
    if not attempt.jobs and attempt.status == "complete":
        return dict(row, status="empty", reason="列表读取成功，当前没有可验证的公开岗位"), None
    if not attempt.jobs or any(error not in CAP_ERRORS for error in attempt.errors):
        return dict(row, status="trial_failed", reason="正式采集器未通过试采，详情见 notes"), None
    if any(not job["url"] for job in attempt.jobs.values()):
        return dict(row, status="trial_failed", reason="试采中有岗位缺少有效的详情链接"), None
    return dict(row, status="ready", task=sid), source


def append_sources(path, sources):
    path = Path(path)
    with project_lock(path.with_name(path.name + ".write")):
        registry = read_json(path)
        if not isinstance(registry, list) or any(not isinstance(item, dict) or not item.get("id") for item in registry):
            raise ValueError("要更新的来源清单须为包含来源 ID 的 JSON 数组")
        urls = {item.get("url"): item["id"] for item in registry}
        ids = {item["id"] for item in registry}
        result = dict(path=str(path), added=[], existing=[], conflicts=[])
        for item in sources:
            sid = item["id"]
            if item["url"] in urls:
                result["existing"].append(urls[item["url"]])
            elif sid in ids:
                result["conflicts"].append(sid)
            else:
                registry.append(item)
                urls[item["url"]] = sid
                ids.add(sid)
                result["added"].append(sid)
        if result["added"]:
            write_json(path, registry)
        return result


def onboard(urls, output, *, company=None, leads=None, registry_path=None):
    entries = lead_entries(leads) if leads else [(company, url, "") for url in urls]
    if not entries:
        raise ValueError("没有带企业名称的招聘链接，请传入招聘网址或企业发现产生的 leads.json")
    entries = list(dict.fromkeys(entries))
    output = Path(output) / f"onboard-{uuid.uuid4().hex[:10]}"
    output.mkdir(parents=True)
    clients, rows, sources = {}, [], {}
    for index, (name, url, evidence) in enumerate(entries, 1):
        address = usable_link(url, "")
        if not address:
            rows.append(dict(company=name or "", input_url=url, status="invalid_url",
                             reason="请输入有效的 HTTP(S) 招聘链接"))
            continue
        row, source = trial_input(name, address, evidence, output / "raw" / f"{index:03}", clients)
        rows.append(row)
        if source:
            sources.setdefault(source["id"], source)
    result = dict(results=rows, ready=len(sources), report=str(output / "onboarding.json"))
    if sources:
        registry = output / "sources.json"
        watch = output / "watch.json"
        write_json(registry, list(sources.values()))
        write_json(watch, dict(registry=registry.name, tasks={
            sid: dict(mode="company", sources=[sid]) for sid in sources
        }))
        result.update(registry=str(registry), watch_config=str(watch))
        if registry_path:
            try:
                result["registry_update"] = append_sources(registry_path, sources.values())
            except (OSError, ValueError, RuntimeError) as error:
                result["registry_update"] = {"path": str(registry_path), "error": str(error)}
    write_json(output / "onboarding.json", result)
    return result
