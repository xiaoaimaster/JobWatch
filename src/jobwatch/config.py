"""Validate local task and source definitions without network calls."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree

from .ai import AI_CLIENTS, Responses
from .cleaning import FIELDS, web_url
from .common import read_json

SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
ADAPTERS = {"json_file", "json_http", "html_http"}
AI_DEFAULTS = dict(provider="openai", base_url=Responses.default_base,
                   api_key_env=Responses.default_key_env, batch_size=8, max_requests=20,
                   max_input_chars=24000, max_output_tokens=3500, timeout_seconds=45,
                   retries=2, recover_fields=False, max_recovery_requests=4)
SEARCH_DEFAULTS = dict(fresh_hours=24, max_refresh=5, priority_sources=[])


ATS_DETAIL = "//*[contains(concat(' ', normalize-space(@class), ' '), ' jobDetail ')]"
ATS_TITLE = ATS_DETAIL + "//*[contains(concat(' ', normalize-space(@class), ' '), ' job-title ')]"


def ats_section(label):
    return (f"*[contains(concat(' ', normalize-space(@class), ' '), ' block-title ') "
            f"and normalize-space(.)='{label}']/following-sibling::*[1]"
            "[contains(concat(' ', normalize-space(@class), ' '), ' block-content ') "
            "and normalize-space(.) != '']")


ATS_SECTIONS = {"duties": ats_section("职位描述"), "requirements": ats_section("职位要求")}
ATS_READY = ATS_DETAIL + "".join(f"[{section}]" for section in ATS_SECTIONS.values())


def require(value, message):
    if not value:
        raise ValueError(message)


def strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def validate_fields(fields, xpath=False):
    require(isinstance(fields, dict) and set(fields) <= set(FIELDS), "字段映射包含未知字段")
    for expression in fields.values():
        require(isinstance(expression, str) and expression.strip(), "字段映射须为非空字符串")
        if xpath:
            etree.XPath(expression)


def job_path_template(value, allow_query=False):
   if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
       return False
   if allow_query and "?" in value:
        return value.count("?") == 1 and "#" not in value and "={id}" in value.partition("?")[2] and value.count("{id}") == 1 and not {".", ".."}.intersection(value.partition("?")[0].split("/"))
   if value.split("/").count("{id}") != 1 or {".", ".."}.intersection(value.split("/")):
        return False
    return not any(ch in "{}?#%" or ch == chr(92) or ch.isspace() for ch in value.replace("{id}", "", 1))

def validate_source(source, parent):
    require(isinstance(source, dict), "来源须为对象")
    source = dict(source)
    sid = source.get("id", "")
    kind = source.get("adapter")
    require(isinstance(sid, str) and SLUG.fullmatch(sid), "来源 id 须为小写字母、数字、下划线或连字符")
    require(isinstance(source.get("company"), str) and source["company"].strip(), f"{sid} 缺少企业名")
    require(kind in ADAPTERS, f"{sid} 适配器须为 json_file、json_http 或 html_http")
    for field in ("careers_url", "discovered_via"):
        if source.get(field):
            web_url(source[field])
    recovery = source.get("recoverable_fields", [])
    require(strings(recovery) and len(set(recovery)) == len(recovery) and
            set(recovery) <= {"title", "location", "description", "duties", "requirements", "bonus"},
            f"{sid} recoverable_fields 含不支持或重复的字段")
    browser = source.get("browser")
    if "detail_profile" in source:
        require(source["detail_profile"] == "ats_job_detail" and kind == "html_http" and isinstance(browser, dict),
                f"{sid} ats_job_detail 需要 HTML 浏览器来源")
        source["browser"] = browser = {"detail_ready_xpath": ATS_READY, **browser}
        details = source.get("detail_fields", {})
        require(isinstance(details, dict), f"{sid} detail_fields 须为对象")
        source["detail_fields"] = {
            **{key: f"{ATS_DETAIL}/{value}" for key, value in ATS_SECTIONS.items()},
            **details,
        }
        source.setdefault("detail_title_xpath", f"string({ATS_TITLE})")
    if browser is not None:
        require(kind == "html_http" and isinstance(browser, dict)
                and set(browser) <= {"ready_xpath", "detail_ready_xpath", "asset_origins", "timeout_seconds",
                                     "operation_timeout_seconds",
                                     "read_post_paths", "pagination"},
                f"{sid} browser 只适用于 HTML 来源")
        require(type(source.get("single_page", False)) is bool, f"{sid} single_page 须为布尔值")
        for field in ("ready_xpath", "detail_ready_xpath"):
            if field in browser:
                etree.XPath(browser[field])
        require(integer(browser.get("timeout_seconds", 18), 2, 45), f"{sid} 浏览器超时须在 2\u201345 秒")
        if "operation_timeout_seconds" in browser:
            require(integer(browser["operation_timeout_seconds"], 2, 300),
                    f"{sid} 浏览器操作硬超时须在 2\u2013300 秒")
        posts = browser.get("read_post_paths", [])
        require(strings(posts) and all(re.fullmatch(r"/[A-Za-z0-9/_.-]+", route) for route in posts),
                f"{sid} read_post_paths 须为已核实的同源只读请求路径")
        buttons = browser.get("pagination")
        if "pagination" in browser:
            required = {"next_button_xpath", "current_page_xpath", "page_size"}
            optional = {"response_path", "response_success_path", "response_success_value", "dismiss_overlay"}
            require(isinstance(buttons, dict) and required <= set(buttons) <= required | optional,
                    f"{sid} 缺少按钮、当前页或每页数配置")
            require(source.get("total_xpath") and not source.get("next_xpath") and not source.get("single_page"),
                    f"{sid} 点击分页须配置 total_xpath，且不能并用链接分页或单页声明")
            for field in ("next_button_xpath", "current_page_xpath"):
                require(isinstance(buttons[field], str) and buttons[field].strip(), f"{sid} {field} 须为 XPath")
                etree.XPath(buttons[field])
            if "dismiss_overlay" in buttons:
                overlay = buttons["dismiss_overlay"]
                require(isinstance(overlay, dict) and set(overlay) == {"overlay_xpath", "close_button_xpath"},
                        f"{sid} dismiss_overlay 须配置遮罩和官网关闭控件的 XPath")
                for field, expression in overlay.items():
                    require(isinstance(expression, str) and expression.strip(), f"{sid} {field} 须为 XPath")
                    etree.XPath(expression)
            require(integer(buttons["page_size"], 1, 200), f"{sid} page_size 须在 1\u2013200")
            if "response_path" in buttons:
                require(isinstance(buttons["response_path"], str) and
                        re.fullmatch(r"/[A-Za-z0-9/_.-]+", buttons["response_path"]),
                        f"{sid} response_path 须为已确认的同源岗位查询路径")
            if "response_success_path" in buttons or "response_success_value" in buttons:
                require("response_path" in buttons and isinstance(buttons.get("response_success_path"), str)
                        and buttons["response_success_path"].strip()
                        and type(buttons.get("response_success_value")) in (bool, int, str),
                        f"{sid} 须同时配置响应路径、成功字段和成功值")
        assets = browser.get("asset_origins", [])
        require(strings(assets) and len(assets) <= 8, f"{sid} asset_origins 须为地址列表，最多 8 个")
        for asset in assets:
            origin = urlsplit(web_url(asset))
            require(origin.scheme == "https" and origin.path in ("", "/") and not origin.query
                    and not origin.fragment, f"{sid} 资源域须填写 HTTPS 源站")
    fields = source.get("fields", {})
    validate_fields(fields, xpath=kind == "html_http")
    if "json_script" in source:
        script = source["json_script"]
        require(kind == "json_http" and isinstance(script, dict) and set(script) == {"xpath", "prefix"}
                and all(isinstance(value, str) and value.strip() for value in script.values()),
                f"{sid} json_script 须为 JSON 来源配置非空的 xpath 和 prefix")
        etree.XPath(script["xpath"])
    if "detail_url_template" in source:
        template = source["detail_url_template"]
        require(kind == "json_http" and source.get("detail_fields") and "id" in fields
                and job_path_template(template),
                f"{sid} detail_url_template 须为包含独立 {{id}} 路径段的站内绝对路径，并配置 id 和 detail_fields")
    if "job_url_template" in source:
        require(kind == "json_http" and "id" in fields and "url" not in fields
                and job_path_template(source["job_url_template"], allow_query=True),
                f"{sid} job_url_template 须使用岗位 id 生成同源路径，且不能并用 url 字段映射")
    if "query_form" in source:
        form = source["query_form"]
        pagination = source.get("pagination")
        require(kind == "json_http" and not source.get("json_script")
                and source.get("total_path") and isinstance(pagination, dict)
                and "page_param" in pagination and pagination.get("size_param")
                and isinstance(form, dict) and set(form) == {"fields", "success_path", "success_value", "page_path"}
                and isinstance(form["fields"], dict)
                and all(isinstance(k, str) and k.strip() and isinstance(v, str) for k, v in form["fields"].items())
                and strings([form["success_path"], form["page_path"]])
                and type(form["success_value"]) in (str, int, bool),
                f"{sid} 只读 JSON 查询表单须配置分页、总数、成功条件和响应页码")
        endpoint = urlsplit(web_url(source["url"]))
        referer = urlsplit(web_url(source.get("careers_url")))
        require((endpoint.scheme, endpoint.netloc) == (referer.scheme, referer.netloc),
                f"{sid} 只读 JSON 查询表单须配置同源的 careers_url")
    if "detail_id_path" in source:
        path = source["detail_id_path"]
        require(kind == "json_http" and source.get("detail_fields") and "id" in fields
                and isinstance(path, str) and all(part.strip() for part in path.split(".")),
                f"{sid} detail_id_path 须为 JSON 详情的非空 ID 字段路径，并配置列表 id 和 detail_fields")
    if "listed_path" in source:
        require(kind == "json_http" and isinstance(source["listed_path"], str) and source["listed_path"].strip()
                and not source.get("total_path"), f"{sid} listed_path 须为 JSON 来源的布尔字段路径，且不能并用 total_path")
    if kind == "json_file":
        require(not source.get("detail_fields") and not source.get("pagination"),
                f"{sid} 本地 JSON 须已含所需职位字段")
        for field in ("items_path", "total_path"):
            if field in source:
                require(isinstance(source[field], str) and source[field].strip(), f"{sid} {field} 须为非空字符串")
        require(isinstance(source.get("path"), str) and source["path"].strip(), f"{sid} 须指定本地 JSON 文件")
        source["path"] = str((parent / source["path"]).resolve())
        return source
    web_url(source["url"])
    require(source.get("access_reviewed") is True, f"{sid} 请先核实自动访问规则并设置 access_reviewed")
    limit = 2000 if browser and browser.get("pagination") else 200
    require(integer(source.get("max_pages", 20), 1, limit), f"{sid} max_pages 须在 1\u2013{limit}")
    require(integer(source.get("interval_seconds", 2), 1, 300), f"{sid} 请求间隔须在 1\u2013300 秒")
    require(integer(source.get("max_details", 0), 0, 100000), f"{sid} 详情上限须为非负整数")
    require("title" in fields and ("id" in fields or "url" in fields), f"{sid} 须映射标题和 ID 或链接")
    details = source.get("detail_fields", {})
    validate_fields(details, xpath=kind == "html_http")
    require(set(details) <= {"description", "duties", "requirements", "bonus", "status", "location"},
            f"{sid} 详情不能替换岗位身份、标题或招聘类型")
    require(not details or "url" in fields or "detail_url_template" in source or "job_url_template" in source,
            f"{sid} 读取详情需要岗位网址或 detail_url_template")
    if "detail_title_xpath" in source:
        require(kind == "html_http" and details and isinstance(source["detail_title_xpath"], str),
                f"{sid} detail_title_xpath 只用于 HTML 详情")
        etree.XPath(source["detail_title_xpath"])
    if kind == "html_http":
        require(not source.get("pagination"), f"{sid} HTML 分页请使用 next_xpath 或 browser.pagination")
        etree.XPath(source["items_xpath"])
        for key in ("next_xpath", "empty_xpath", "total_xpath", "detail_recovery_xpath"):
            if source.get(key):
                etree.XPath(source[key])
    else:
        for field in ("items_path", "total_path"):
            if field in source:
                require(isinstance(source[field], str) and source[field].strip(), f"{sid} {field} 须为非空字符串")
        pagination = source.get("pagination")
        if pagination is not None:
            require(isinstance(pagination, dict) and set(pagination) <=
                    {"page_param", "offset_param", "start", "size_param", "page_size"}
                    and len(set(pagination) & {"page_param", "offset_param"}) == 1,
                    f"{sid} 分页须指定 page_param 或 offset_param")
            key = "page_param" if "page_param" in pagination else "offset_param"
            require(isinstance(pagination[key], str) and pagination[key].strip(), f"{sid} 分页参数无效")
            require(integer(pagination.get("start", 1 if key == "page_param" else 0), 0, 1000),
                    f"{sid} 起始位置不合法")
            size_param = pagination.get("size_param")
            if key == "offset_param":
                require(size_param is not None, f"{sid} offset 分页须指定 size_param 和 page_size")
            if size_param is not None:
                require(isinstance(size_param, str) and size_param.strip()
                        and size_param != pagination[key]
                        and integer(pagination.get("page_size"), 1, 1000), f"{sid} 页大小配置无效")
    return source


def load_settings(path):
    path = Path(path).resolve()
    cfg = read_json(path)
    require(isinstance(cfg, dict), "配置须为 JSON 对象")
    output = (path.parent / cfg.get("output", "output")).resolve()
    registry_file = (path.parent / cfg["registry"]).resolve()
    registry = read_json(registry_file)
    require(isinstance(registry, list) and registry, "来源清单必须为非空数组")
    sources = {}
    for entry in registry:
        source = validate_source(entry, registry_file.parent)
        require(source["id"] not in sources, "来源 id 重复")
        sources[source["id"]] = source
    tasks = cfg.get("tasks")
    require(isinstance(tasks, dict) and tasks, "tasks 必须是非空对象")
    normalized = {}
    for name, task in tasks.items():
        require(SLUG.fullmatch(name) and isinstance(task, dict), f"任务名或配置无效\uff1a{name}")
        mode = task.get("mode")
        require(mode in ("company", "role"), f"{name} mode 须为 company 或 role")
        ids = task.get("sources")
        if ids == "all" and mode == "role":
            ids = list(sources)
        require(strings(ids) and ids and len(ids) == len(set(ids)) and set(ids) <= set(sources),
                f"{name} 须列出有效且不重复的来源")
        if mode == "company":
            require(len({sources[sid]["company"] for sid in ids}) == 1, f"{name} 单企业任务只能有一家企业")
        direction = task.get("direction", "")
        require(isinstance(direction, str) and (direction.strip() or mode == "company"), f"{name} 缺少方向")
        terms = {field: task.get(field, []) for field in ("keywords", "candidate_keywords", "cities", "types", "pinned_urls")}
        require(all(strings(values) for values in terms.values()), f"{name} 筛选值须为字符串数组")
        if mode == "role" and not terms["keywords"]:
            terms["keywords"] = [direction]
        terms["pinned_urls"] = [web_url(value) for value in terms["pinned_urls"]]
        rules = task.get("body_candidate_rules", [])
        require(isinstance(rules, list) and all(
            isinstance(rule, dict) and set(rule) == {"terms", "context"}
            and all(strings(values) and values for values in rule.values()) for rule in rules
        ), f"{name} body_candidate_rules 的每条规则须包含非空字符串数组 terms 和 context")
        require(not rules or mode == "role", f"{name} body_candidate_rules 只适用于 role 任务")
        normalized[name] = dict(mode=mode, direction=direction, sources=ids, **terms)
        if "ai_scope" in task:
            require(task["ai_scope"] in ("candidates", "all"), f"{name} ai_scope 须为 candidates 或 all")
            normalized[name]["ai_scope"] = task["ai_scope"]
        if "ai_clean" in task:
            require(type(task["ai_clean"]) is bool, f"{name} ai_clean 须为 true 或 false")
            normalized[name]["ai_clean"] = task["ai_clean"]
        if rules:
            normalized[name]["body_candidate_rules"] = rules
        if "search" in task:
            search = task["search"]
            require(mode == "role" and isinstance(search, dict) and set(search) <= set(SEARCH_DEFAULTS),
                    f"{name} search 只适用于 role 任务")
            search = {**SEARCH_DEFAULTS, **search}
            require(integer(search["fresh_hours"], 0, 8760) and integer(search["max_refresh"], 0, 1000),
                    f"{name} fresh_hours 须在 0\u20138760\uff0cmax_refresh 须在 0\u20131000")
            priority = search["priority_sources"]
            require(strings(priority) and len(set(priority)) == len(priority) and set(priority) <= set(ids),
                    f"{name} priority_sources 须列出不重复的任务来源")
            normalized[name]["search"] = search
    ai = cfg.get("ai")
    if ai is not None:
        require(isinstance(ai, dict) and set(ai) <= set(AI_DEFAULTS) | {"model"}, "AI 配置包含未知字段")
        provider = ai.get("provider", "openai")
        require(isinstance(provider, str) and provider in AI_CLIENTS, "AI provider 须为 openai 或 deepseek")
        client = AI_CLIENTS[provider]
        ai = {**AI_DEFAULTS, "base_url": client.default_base, "api_key_env": client.default_key_env, **ai}
        require(isinstance(ai.get("model"), str) and ai["model"].strip(), "AI 须填写 model")
        endpoint = urlsplit(web_url(ai["base_url"]))
        require(endpoint.scheme == "https" and not endpoint.query and not endpoint.fragment,
                "AI base_url 须为 HTTPS API 基址")
        require(isinstance(ai["api_key_env"], str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", ai["api_key_env"]),
                "AI api_key_env 须为环境变量名")
        require(type(ai["recover_fields"]) is bool, "AI recover_fields 须为布尔值")
        for field, low, high in (("batch_size", 1, 100), ("max_requests", 1, 10000),
                                 ("max_input_chars", 200, 1000000), ("max_output_tokens", 1, 100000),
                                 ("timeout_seconds", 1, 300), ("retries", 0, 5), ("max_recovery_requests", 0, 1000)):
            require(integer(ai[field], low, high), f"AI {field} 须在 {low}\u2013{high}")
    return output, sources, normalized, ai
