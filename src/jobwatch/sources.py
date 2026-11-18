"""Read source data, keep raw responses, and track completeness explicitly."""
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener
from urllib.robotparser import RobotFileParser
from datetime import datetime, timezone
import json
import time

from lxml import etree, html

from .ai import AIError, NoRedirect, recover_fields
from .browser_process import BrowserInterrupted, IsolatedBrowser
from .cleaning import normalize, text, web_url
from .common import normalized_proof

PAGE_CAP = "达到页数上限，列表可能尚未读取完毕"
DETAIL_CAP = "达到详情数量上限，部分岗位详情未读取"
CAP_ERRORS = {PAGE_CAP, DETAIL_CAP}


def coverage_issue(status, errors):
    if status == "partial" and errors and all(error in CAP_ERRORS for error in errors):
        return "limit"
    return "error" if status in ("partial", "failed") else ""


@dataclass
class Collection:
    jobs: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    recovered_fields: dict = field(default_factory=dict)
    pages: int = 0
    list_complete: bool = False
    declared_total: int | None = None
    details: dict | None = None

    @property
    def status(self):
        if self.list_complete and not self.errors:
            return "ai_assisted" if self.recovered_fields else "complete"
        return "partial" if self.jobs or self.pages else "failed"


def lookup(data, path):
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def xpath_value(node, query):
    values = node.xpath(query)
    if not isinstance(values, list):
        return str(values)
    return ["\n".join(value.itertext()) if isinstance(value, html.HtmlElement) else str(value)
            for value in values]


def xpath_fields(node, fields):
    if node.getparent() is not None:
        node = parse_html(etree.tostring(node, encoding="utf-8", method="html", with_tail=False))
    result = {field: xpath_value(node, expr) for field, expr in fields.items()}
    for field in ("id", "url"):
        if field in result and isinstance(result[field], list):
            if len(set(result[field])) > 1:
                raise ValueError(f"同一岗位匹配到多个 {field}")
            result[field] = next(iter(result[field]), "")
    return result


def query_url(url, values):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key not in values]
    return urlunsplit(parts._replace(query=urlencode(query + list(values.items())), fragment=""))


def pagination_params(settings, number):
    is_offset = "offset_param" in settings
    key = "offset_param" if is_offset else "page_param"
    start = settings.get("start", 0 if is_offset else 1)
    step = settings["page_size"] if is_offset else 1
    params = {settings[key]: start + number * step}
    if settings.get("size_param"):
        params[settings["size_param"]] = settings["page_size"]
    return params


def pagination_url(url, settings, number):
    return query_url(url, pagination_params(settings, number))


def origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


class WebSource:
    def __init__(self, source):
        self.base = source["url"]
        self.interval = source.get("interval_seconds", 2)
        self.next_request = 0.0
        self.opener = build_opener(NoRedirect())
        self.robots = None

    def check_access(self, url, folder):
        if origin(web_url(url)) != origin(self.base):
            raise ValueError("自动查询限于配置的同源地址")
        if self.robots is None:
            parsed = urlsplit(self.base)
            robots_url = urlunsplit(parsed._replace(path="/robots.txt", query="", fragment=""))
            robots = RobotFileParser()
            try:
                raw = self.fetch(robots_url, folder / "robots.txt")
                if raw.lstrip().startswith(b"<"):
                    raise ValueError("robots.txt 返回了 HTML，无法确认访问规则")
                robots.parse(raw.decode("utf-8-sig").splitlines())
            except HTTPError as error:
                if error.code not in (404, 410):
                    raise
                robots.parse([])
            crawl_delay = robots.crawl_delay("JobWatch")
            if crawl_delay is not None:
                self.interval = max(self.interval, crawl_delay)
                self.next_request = max(self.next_request, time.monotonic() + self.interval)
            self.robots = robots
        if not self.robots.can_fetch("JobWatch", url):
            raise ValueError("robots.txt 禁止该路径")

    def get(self, url, path):
        self.check_access(url, path.parent)
        return self.fetch(url, path)

    def post_query(self, url, path, fields, careers_url):
        self.check_access(url, path.parent)
        page = urlsplit(careers_url)
        headers = {
            "Origin": f"{page.scheme}:{chr(47)*2}{page.netloc}",
            "Referer": careers_url,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        }
        return self.fetch(url, path, urlencode(fields).encode("utf-8"), headers)

    def throttle(self):
        time.sleep(max(0, self.next_request - time.monotonic()))
        self.next_request = time.monotonic() + self.interval

    def fetch(self, url, path, data=None, headers=None):
        for attempt in range(2):
            self.throttle()
            try:
                req = Request(url, data=data, headers={"User-Agent": "JobWatch/0.2", **(headers or {})})
                with self.opener.open(req, timeout=20) as response:
                    raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ValueError("页面超过 8 MB")
                path.write_bytes(raw)
                return raw
            except HTTPError as error:
                if attempt or error.code not in (429, 500, 502, 503, 504):
                    raise
                retry_after = error.headers.get("Retry-After", "")
                try:
                    delay = int(retry_after)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        delay = 2
                if delay > 30:
                    raise
                self.next_request = max(self.next_request, time.monotonic() + max(0, delay))
            except (URLError, TimeoutError):
                if attempt:
                    raise


def scalar_values(data):
    if isinstance(data, dict):
        for value in data.values():
            yield from scalar_values(value)
    elif isinstance(data, list):
        for value in data:
            yield from scalar_values(value)
    elif type(data) in (int, str):
        yield str(data)


def parse_html(raw):
    return html.fromstring(raw, parser=html.HTMLParser(encoding="utf-8", no_network=True))


def parse_json(raw, source):
    if not (script := source.get("json_script")):
        return json.loads(raw)
    values = xpath_value(parse_html(raw), script["xpath"])
    if isinstance(values, str):
        values = [values]
    matches = [value.strip() for value in values if value.strip().startswith(script["prefix"])]
    if len(matches) != 1:
        raise ValueError("未找到唯一的内嵌 JSON 脚本")
    data = matches[0].removeprefix(script["prefix"]).lstrip()
    return json.JSONDecoder().raw_decode(data)[0]


def nonnegative_integer(value, name):
    try:
        if isinstance(value, str):
            digits = value.strip().replace(",", "").replace("\uff0c", "")
            if digits.isascii() and digits.isdecimal():
                return int(digits)
        if type(value) is float and value.is_integer() and value >= 0:
            return int(value)
        raise ValueError
    except (ValueError, OverflowError):
        raise ValueError(f"{name} 必须返回非负整数") from None


def page_records(raw, source, expected_page=None):
    if source["adapter"] == "html_http":
        tree = parse_html(raw)
        records = tree.xpath(source["items_xpath"])
        if not isinstance(records, list) or any(not isinstance(node, html.HtmlElement) for node in records):
            raise ValueError("列表 XPath 必须返回岗位节点")
        if not records and (not source.get("empty_xpath") or not tree.xpath(source["empty_xpath"])):
            raise ValueError("未找到岗位节点，也未找到空列表标记")
        next_links = xpath_value(tree, source["next_xpath"]) if source.get("next_xpath") else []
        if not isinstance(next_links, list) or len(next_links) > 1:
            raise ValueError("下一页 XPath 须返回一个链接属性或空列表")
        total = None
        if source.get("total_xpath"):
            total = nonnegative_integer(tree.xpath(source["total_xpath"]), "total_xpath")
        return records, next(iter(next_links), ""), total
    parsed = parse_json(raw, source)
    form = source.get("query_form")
    if form:
        status = lookup(parsed, form["success_path"])
        expected_status = form["success_value"]
        if type(status) is not type(expected_status) or status != expected_status:
            raise ValueError("岗位查询返回业务失败")
        page = lookup(parsed, form["page_path"])
        if type(page) is not int or page != expected_page:
            raise ValueError(f"岗位查询未返回第 {expected_page} 页")
    key = source.get("items_path")
    records = lookup(parsed, key) if key else parsed
    if not isinstance(records, list):
        raise ValueError("岗位列表路径未返回数组")
    total_path = source.get("total_path")
    total = lookup(parsed, total_path) if total_path else None
    if form and isinstance(total, str):
        total = nonnegative_integer(total, total_path)
    if total_path and (type(total) is not int or total < 0):
        raise ValueError("岗位总数路径未返回非负整数")
    if listed_path := source.get("listed_path"):
        flags = [lookup(record, listed_path) for record in records]
        if any(type(flag) is not bool for flag in flags):
            raise ValueError("岗位公开列表字段须为布尔值")
        records = [record for record, flag in zip(records, flags) if flag]
    return records, "", total


def map_fields(record, source, fields):
    if source["adapter"] == "html_http":
        return xpath_fields(record, fields)
    if not isinstance(record, dict):
        raise ValueError("岗位记录必须是对象")
    return {field: lookup(record, path) for field, path in fields.items()} if fields else record


def missing(mapped, fields):
    return [name for name in fields if not text(mapped.get(name))]


def detail_url(source, job):
    if template := source.get("detail_url_template"):
        return templated_job_url(source, template, job["id"])
    if not job["url"]:
        raise ValueError("岗位缺少详情地址")
    return job["url"]


def templated_job_url(source, template, job_id):
    if job_id in (".", "..") or any(character in job_id for character in "/\\%"):
        raise ValueError("岗位 ID 不能作为详情路径段")
    return web_url(template.replace("{id}", quote(job_id, safe="")), source["url"])


def recover(mapped, record, fields, client, result):
    if not fields or not client:
        return mapped, []
    if isinstance(record, html.HtmlElement):
        record = [str(value).strip() for value in record.xpath(
            './/text()[not(ancestor::script or ancestor::style or ancestor::noscript)]') if value.strip()]
    values = set(scalar_values(record))
    try:
        replacement = recover_fields(client, record, fields, values)
    except AIError as error:
        result.errors.append(f"辅助提取失败\uff1a{error}")
        return mapped, []
    accepted = {key: value for key, value in replacement.items() if value}
    return {**mapped, **accepted}, sorted(accepted)


def collect(source, folder, client=None):
    if "browser" in source:
        browser = IsolatedBrowser(source, folder)
        try:
            result = collect_pages(source, folder, client, browser)
        finally:
            close_error = browser.close()
        if close_error:
            result.errors.append(f"关闭浏览器\uff1a{close_error}")
        return result
    return collect_pages(source, folder, client)


def collect_pages(source, folder, client, browser=None, *, web=None):
    result = Collection()
    folder.mkdir(parents=True, exist_ok=True)
    kind = source["adapter"]
    extension = "html" if kind == "html_http" or source.get("json_script") else "json"
    base = source.get("url") or source.get("careers_url", "")
    pagination = source.get("pagination") or {}
    form = source.get("query_form")
    url = base
    visited = set()
    expected_total = None
    buttons = source.get("browser", {}).get("pagination")
    verified_pagination = bool(buttons or form)
    seen_links = set()
    if web is None and kind != "json_file":
        web = WebSource(source)
    fields = source.get("fields", {})
    recovery = source.get("recoverable_fields", [])
    details = source.get("detail_fields", {})
    browser_interrupted = False
    for number in range(source.get("max_pages", 20)):
        expected_page = None
        try:
            if not web:
                data = Path(source["path"]).read_bytes()
                (folder / "input.json").write_bytes(data)
                records, next_url, total = page_records(data, source)
            else:
                if pagination and not form:
                    url = pagination_url(base, pagination, number)
                if not buttons and not form:
                    if url in visited:
                        raise ValueError("分页 URL 重复")
                    visited.add(url)
                path = folder / f"page-{number + 1:03}.{extension}"
                if buttons and number:
                    remaining = min(buttons["page_size"], expected_total - len(result.jobs))
                    response = browser.advance(path, number + 1, remaining)
                elif browser:
                    response = browser.get(web, url, path)
                elif form:
                    params = pagination_params(pagination, number)
                    expected_page = params[pagination["page_param"]]
                    response = web.post_query(url, path, {**form["fields"], **params}, source["careers_url"])
                else:
                    response = web.get(url, path)
                records, next_url, total = page_records(response, source, expected_page)
            if expected_total is None:
                expected_total = total
                result.declared_total = total
            if total != expected_total:
                raise ValueError("翻页期间岗位总数变化")
            if form and len(records) != min(pagination["page_size"], total - len(result.jobs)):
                raise ValueError(f"第 {number + 1} 页岗位数与声明范围不一致")
            if buttons:
                tree = parse_html(response)
                if total == 0 and not records and number == 0:
                    if "response_success_path" not in buttons:
                        raise ValueError("未配置查询业务成功条件，不能确认动态零结果")
                else:
                    current = nonnegative_integer(tree.xpath(f'string({buttons["current_page_xpath"]})'), "current_page_xpath")
                    if current != number + 1:
                        raise ValueError(f"页码未切换到第 {number + 1} 页")
                    expected = min(buttons["page_size"], total - len(result.jobs))
                    if len(records) != expected or expected <= 0:
                        raise ValueError(f"第 {current} 页岗位数与声明范围不一致")
        except (ValueError, OSError, etree.LxmlError) as error:
            browser_interrupted = isinstance(error, BrowserInterrupted)
            result.errors.append(f"列表\uff1a{error}")
            break

        previous_count = len(result.jobs)
        page_jobs = {}
        page_links = set()
        page_recovered = {}
        for index, record in enumerate(records, 1):
            try:
                mapped = map_fields(record, source, fields)
                missing_fields = missing(mapped, [key for key in recovery
                                                   if key in fields or (kind == "json_file" and not fields)])
                mapped, assisted = recover(mapped, record, missing_fields, client, result)
                unresolved = missing(mapped, missing_fields)
                if unresolved and client:
                    result.errors.append("列表字段未恢复\uff1a" + ", ".join(unresolved))
                job = normalize(mapped, source, url)
                if template := source.get("job_url_template"):
                    job["url"] = templated_job_url(source, template, job["id"])
                elif source.get("json_script") and source.get("detail_url_template") and not job["url"]:
                    job["url"] = detail_url(source, job)
            except (ValueError, etree.LxmlError) as error:
                result.errors.append(f"第 {number + 1} 页第 {index} 条\uff1a{error}")
                continue
            if verified_pagination:
                if (job["id"] in result.jobs or job["id"] in page_jobs or
                        (job["url"] and (job["url"] in seen_links or job["url"] in page_links))):
                    result.errors.append(f"第 {number + 1} 页岗位 ID 或详情链接重复")
                    continue
                page_jobs[job["id"]] = job
                if job["url"]:
                    page_links.add(job["url"])
                if assisted:
                    page_recovered[job["id"]] = assisted
                continue
            if job["id"] in result.jobs:
                if result.jobs[job["id"]] != job:
                    result.errors.append("同一岗位 ID 有不同内容")
                continue
            result.jobs[job["id"]] = job
            if assisted:
                result.recovered_fields[job["id"]] = assisted
        if verified_pagination:
            if len(page_jobs) != len(records) or result.errors:
                break
            result.jobs.update(page_jobs)
            seen_links.update(page_links)
            result.recovered_fields.update(page_recovered)
            result.pages += 1
            if len(result.jobs) == total:
                result.list_complete = True
                break
            continue
        result.pages += 1
        if total is not None and len(result.jobs) > total:
            result.errors.append("岗位数超过声明总数")
            break
        if (pagination or next_url) and records and len(result.jobs) == previous_count:
            result.errors.append("本页没有新增岗位 ID，分页可能重复或结构变化")
            break
        if not next_url and (not pagination or not records or
                             (total is not None and len(result.jobs) == total)):
            if browser and records and total is None and not source.get("next_xpath") and not source.get("single_page"):
                result.errors.append("动态页面缺少分页规则或单页声明，结果可能不完整")
            else:
                result.list_complete = total is None or len(result.jobs) == total
                if not result.list_complete:
                    result.errors.append("列表结束时岗位数与声明总数不一致")
            break
        if next_url:
            try:
                url = web_url(next_url, url)
            except ValueError as error:
                result.errors.append(f"下一页\uff1a{error}")
                break
    if not result.list_complete and not result.errors:
        result.errors.append(PAGE_CAP)
    if details:
        listed_jobs = list(result.jobs.values())
        detail_limit = 0 if browser_interrupted else source.get("max_details", 0) or len(listed_jobs)
        result.details = dict(success=0, failed=0, skipped=max(0, len(listed_jobs) - detail_limit))
        if result.details["skipped"] and not browser_interrupted:
            result.errors.append(DETAIL_CAP)
        for index, job in enumerate(listed_jobs[:detail_limit], 1):
            try:
                address = detail_url(source, job)
                path = folder / f"detail-{index:05}.{extension}"
                if browser:
                    raw = browser.get(web, address, path, detail=True)
                else:
                    raw = web.get(address, path)
                node = parse_html(raw) if kind == "html_http" else parse_json(raw, source)
                if id_path := source.get("detail_id_path"):
                    detail_id = lookup(node, id_path)
                    if type(detail_id) not in (str, int) or str(detail_id).strip() != job["id"]:
                        raise ValueError("详情记录 ID 与列表不一致")
                if source.get("detail_title_xpath"):
                    titles = xpath_value(node, source["detail_title_xpath"])
                    if isinstance(titles, list):
                        titles = titles[0] if len(titles) == 1 else ""
                    if normalized_proof(text(titles)) != normalized_proof(job["title"]):
                        raise ValueError("详情页标题与列表不一致")
                mapped = map_fields(node, source, details)
                requested = missing(mapped, [key for key in recovery if key in details])
                recovery_node = node
                if requested and client and kind == "html_http":
                    selector = source.get("detail_recovery_xpath")
                    candidates = node.xpath(selector) if selector else []
                    if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], html.HtmlElement):
                        raise ValueError("HTML 详情 AI 提取需要一个明确的详情区域")
                    recovery_node = candidates[0]
                mapped, assisted = recover(mapped, recovery_node, requested, client, result)
                if client and missing(mapped, requested):
                    raise ValueError("详情字段未恢复")
                if not any(text(value) for value in mapped.values()):
                    raise ValueError("详情字段均未命中")
                normalized = normalize({**mapped, "id": job["id"], "title": job["title"]}, source)
                if source.get("detail_profile") == "ats_job_detail" and not (
                    normalized["duties"] and normalized["requirements"]
                ):
                    raise ValueError("详情正文不完整，缺少职位描述或职位要求")
                for key in ("duties", "requirements", "bonus", "status", "location"):
                    if normalized[key] and (key != "status" or mapped.get(key)):
                        job[key] = normalized[key]
                if assisted:
                    saved = result.recovered_fields.get(job["id"], [])
                    result.recovered_fields[job["id"]] = sorted(set(saved + assisted))
                result.details["success"] += 1
            except (ValueError, OSError, etree.LxmlError) as error:
                result.details["failed"] += 1
                result.errors.append(f"详情 {index}\uff08岗位 {job['id']}\uff09\uff1a{error}")
                if isinstance(error, BrowserInterrupted):
                    result.details["skipped"] = len(listed_jobs) - index
                    break
    return result
