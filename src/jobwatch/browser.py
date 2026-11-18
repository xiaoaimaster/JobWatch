"""Render public recruitment HTML in an isolated browser session."""
from contextlib import ExitStack
import os
import shutil
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

from .common import write_json
from .sources import WebSource, lookup, origin

BROWSER_AGENT = "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36 JobWatch/0.3"
CARD_KEYS = """(xpath) => {
    const nodes = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE);
    return Array.from({length: nodes.snapshotLength}, (_, i) => {
        const node = nodes.snapshotItem(i);
        const link = node.matches('a[href]') ? node : node.querySelector('a[href]');
        return node.getAttribute('data-id') || link?.getAttribute('href') || node.textContent.trim();
    });
}"""
PAGE_NUMBER = """(xpath) => document.evaluate(
    'string(' + xpath + ')', document, null, XPathResult.STRING_TYPE
).stringValue.trim()"""


def launch_chromium(chromium, error_type, proxy=None):
    override = os.environ.get("JOBWATCH_CHROMIUM")
    options = {"proxy": proxy, "timeout": 12000}
    if override:
        options["executable_path"] = override
    try:
        try:
            return chromium.launch(**options)
        except error_type as error:
            missing = "Executable doesn't exist" in str(error)
            if override or not missing:
                raise
        system = shutil.which("chromium") or shutil.which("chromium-browser")
        if not system:
            raise ValueError("Playwright 无头 Chromium 未安装。运行 python -m playwright install chromium --only-shell") from None
        return chromium.launch(executable_path=system, **options)
    except error_type as error:
        message = str(error)
        if "socket() failed: Operation not permitted" in message:
            raise ValueError("运行环境禁止 Chromium 创建 Unix 套接字\uff08Operation not permitted\uff09") from None
        if override and "Executable doesn't exist" in message:
            raise ValueError("JOBWATCH_CHROMIUM 指向的可执行文件不存在") from None
        raise ValueError("Chromium 启动失败。请检查浏览器安装、可执行文件与系统依赖") from None


def browser_check():
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise ValueError('请先安装浏览器支持\uff1apip install ".[browser]"') from None
    with sync_playwright() as playwright:
        with launch_chromium(playwright.chromium, Error) as browser:
            page = browser.new_page()
            try:
                page.goto("data:text/html,<body><script>document.body.dataset.ready='yes'</script>",
                          timeout=5000)
                page.locator("body[data-ready=yes]").wait_for(timeout=5000)
            except Error:
                raise ValueError("Chromium 已启动，JavaScript 测试页渲染失败") from None
            return {"chromium": browser.version, "javascript": "ok"}


class BrowserPages:
    def __init__(self, source):
        self.source = source
        self.options = source["browser"]
        self.stack = ExitStack()
        self.page = None
        self.pagination_trace = None
        self.progress = lambda phase: None

    def __enter__(self):
        return self

    def __exit__(self, *error):
        return self.stack.__exit__(*error)

    def start(self, url):
        try:
            from playwright.sync_api import Error, TimeoutError, sync_playwright
        except ImportError as error:
            raise ValueError('请先安装浏览器支持\uff1apip install ".[browser]"') from error
        self.error = Error
        self.timeout_error = TimeoutError
        hostname = urlsplit(url).hostname
        proxy_url = getproxies().get(urlsplit(url).scheme) if not proxy_bypass(hostname) else None
        proxy = None
        if proxy_url:
            parsed = urlsplit(proxy_url)
            if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname:
                raise ValueError("无法识别浏览器代理地址")
            server = parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1], path="", query="", fragment="")
            proxy = {"server": server.geturl()}
            if parsed.username:
                proxy.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
        try:
            p = self.stack.enter_context(sync_playwright())
            browser = launch_chromium(p.chromium, Error, proxy=proxy)
            self.stack.callback(browser.close)
            context = browser.new_context(service_workers="block", user_agent=BROWSER_AGENT,
                                          accept_downloads=False)
            self.stack.callback(context.close)
            context.route("**/*", self.route)
            context.route_web_socket("**/*", lambda socket: socket.close())
            self.page = context.new_page()
        except Error as error:
            raise ValueError("Chromium 会话初始化失败，请检查系统依赖") from error

    def route(self, route):
        request = route.request
        resource = request.resource_type
        url = request.url
        label = resource
        checking_rules = False
        try:
            parts = urlsplit(url)
            label = f"{resource} {parts.hostname or '未知域'}"
            key = origin(url)
            same = key == origin(self.source["url"])
            tracking = (self.pagination_trace is not None and same and resource in ("fetch", "xhr")
                        and parts.path == self.pagination_trace["response_path"])
            if tracking:
                self.pagination_trace["requests_seen"] += 1
            if not same and (key not in self.assets or resource not in ("script", "stylesheet")):
                route.abort()
                return
            allowed_post = (same and resource in ("fetch", "xhr") and
                            parts.path in self.options.get("read_post_paths", []))
            if request.method not in ("GET", "HEAD") and not (request.method == "POST" and allowed_post):
                if tracking:
                    self.failures.append(f"{label} 的 {request.method} 请求未获配置允许")
                route.abort()
                return
            if resource in {"image", "media", "font", "ping"}:
                route.abort()
                return
            if resource == "document" and (not request.is_navigation_request() or
                                           request.frame != self.page.main_frame):
                route.abort()
                return
            site = self.site if same else self.assets[key]
            directory = self.folder if same else self.folder / "assets" / parts.hostname
            directory.mkdir(parents=True, exist_ok=True)
            checking_rules = True
            site.check_access(url, directory)
            checking_rules = False
            if resource in ("document", "fetch", "xhr"):
                site.throttle()
            response = route.fetch(max_redirects=0, max_retries=0, timeout=self.timeout)
            if tracking:
                self.pagination_trace["http_statuses"].add(response.status)
            if 300 <= response.status < 400 and response.status != 304:
                if resource == "document":
                    self.failures.append(f"主页面 HTTP {response.status} 跳转，请配置核实后的最终网址")
                route.abort()
                return
            if resource in ("fetch", "xhr", "script", "stylesheet") and response.status >= 400:
                self.failures.append(f"{label} HTTP {response.status}")
            try:
                route.fulfill(response=response)
            finally:
                response.dispose()
        except (OSError, ValueError, self.error) as error:
            if checking_rules:
                reason = f"HTTP {error.code}" if isinstance(error, HTTPError) else "规则禁止或无法读取规则"
                self.failures.append(f"{label} 的 robots.txt 检查失败\uff08{reason}\uff09")
            else:
                self.failures.append(f"{label} 请求失败")
            try:
                route.abort()
            except self.error:
                pass

    def expect_search(self, pagination):
        return self.page.expect_response(
            lambda response: (origin(response.url) == origin(self.source["url"]) and
                              urlsplit(response.url).path == pagination["response_path"] and
                              response.request.resource_type in ("fetch", "xhr")),
            timeout=self.timeout,
        )

    def verify_search(self, response, pagination):
        if not 200 <= response.status < 300:
            raise ValueError(f"岗位查询 HTTP {response.status}")
        if "response_success_path" in pagination:
            try:
                value = lookup(response.json(), pagination["response_success_path"])
            except (ValueError, self.error):
                raise ValueError("岗位查询未返回可校验 JSON") from None
            expected = pagination["response_success_value"]
            if type(value) is not type(expected) or value != expected:
                raise ValueError("岗位查询返回业务失败")

    def save_page(self, path):
        self.progress("保存渲染页面")
        if self.page.url == "about:blank":
            return
        try:
            rendered = self.page.content().encode()
            if len(rendered) <= 8_000_000:
                pending = path.with_suffix(path.suffix + ".tmp")
                pending.write_bytes(rendered)
                pending.replace(path)
        except self.error:
            pass

    def dismiss_pagination_overlay(self, options):
        overlay = self.page.locator("xpath=" + options["overlay_xpath"]).filter(visible=True)
        if overlay.count() == 0:
            return False
        close = self.page.locator("xpath=" + options["close_button_xpath"]).filter(visible=True)
        close.first.wait_for(state="visible", timeout=self.timeout)
        if close.count() != 1:
            raise ValueError("引导关闭控件未唯一匹配")
        close.click(timeout=self.timeout)
        overlay.first.wait_for(state="hidden", timeout=self.timeout)
        return True

    def advance(self, path, next_page, expected_rows):
        self.progress("浏览器翻页")
        pagination = self.options["pagination"]
        self.failures = []
        trace = dict(target_page=next_page, phase="定位下一页按钮", response_path=pagination.get("response_path"),
                     requests_seen=0, http_statuses=set(), click_returned=False, expected_rows=expected_rows,
                     overlay_dismissed=False)
        before = []
        button = self.page.locator("xpath=" + pagination["next_button_xpath"])
        try:
            button.first.wait_for(state="visible", timeout=self.timeout)
            if overlay := pagination.get("dismiss_overlay"):
                trace["phase"] = "关闭翻页引导"
                trace["overlay_dismissed"] = self.dismiss_pagination_overlay(overlay)
                trace["phase"] = "定位下一页按钮"
            if button.count() != 1:
                raise ValueError("下一页按钮未找到或匹配多个元素")
            if button.is_disabled(timeout=self.timeout):
                raise ValueError("列表尚未采全，但下一页按钮已禁用")
            items_xpath = self.source["items_xpath"]
            trace["page_before"] = self.page.evaluate(PAGE_NUMBER, pagination["current_page_xpath"])
            before = self.page.evaluate(CARD_KEYS, items_xpath)
            trace["phase"] = "点击下一页按钮"
            self.pagination_trace = trace
            if trace["response_path"]:
                with self.expect_search(pagination) as pending:
                    button.click(timeout=self.timeout)
                    trace.update(click_returned=True, phase="等待岗位查询响应")
                trace["phase"] = "验证岗位查询响应"
                self.verify_search(pending.value, pagination)
            else:
                button.click(timeout=self.timeout)
                trace["click_returned"] = True
            trace["phase"] = "等待页码和岗位更新"
            self.page.wait_for_function(
                f"""(opts) => {{
                    const page = ({PAGE_NUMBER})(opts.pageXPath);
                    const cards = ({CARD_KEYS})(opts.itemsXPath);
                    return page === String(opts.nextPage) && cards.length === opts.expectedRows
                        && cards.every(key => key && !opts.before.includes(key));
                }}""",
                arg=dict(pageXPath=pagination["current_page_xpath"], itemsXPath=items_xpath,
                         nextPage=next_page, expectedRows=expected_rows, before=before),
                timeout=self.timeout,
            ).dispose()
        except self.timeout_error:
            raise ValueError(f"第 {next_page} 页\uff1a{trace['phase']}超时，见 {path.stem}.pagination.json"
                             + self.diagnostics()) from None
        except self.error:
            raise ValueError(f"第 {next_page} 页\uff1a{trace['phase']}失败，见 {path.stem}.pagination.json"
                             + self.diagnostics()) from None
        except ValueError as error:
            raise ValueError(f"第 {next_page} 页\uff1a{error}，见 {path.stem}.pagination.json"
                             + self.diagnostics()) from error
        else:
            trace["phase"] = "通过"
        finally:
            self.pagination_trace = None
            self.save_page(path)
            self.progress("生成翻页诊断")
            try:
                trace["page_after"] = self.page.evaluate(PAGE_NUMBER, pagination["current_page_xpath"])
                cards = self.page.evaluate(CARD_KEYS, self.source["items_xpath"])
                trace.update(rows_after=len(cards), rows_repeated=sum(key in before for key in cards))
            except self.error:
                pass
            trace.update(http_statuses=sorted(trace["http_statuses"]), resource_failures=list(dict.fromkeys(self.failures)))
            write_json(path.with_suffix(".pagination.json"), trace)
        if not path.is_file():
            raise ValueError("渲染页面未保存或超过 8 MB")
        return path.read_bytes()

    def get(self, site, url, path, detail=False):
        self.progress("检查页面访问规则")
        site.check_access(url, path.parent)
        if self.page is None:
            self.progress("启动 Chromium 会话")
            self.start(url)
            self.assets = {origin(base): WebSource({"url": base, "interval_seconds": site.interval})
                           for base in self.options.get("asset_origins", [])}
        self.site, self.folder = site, path.parent
        self.timeout = self.options.get("timeout_seconds", 18) * 1000
        self.failures = []
        selector = self.options.get("detail_ready_xpath" if detail else "ready_xpath")
        if not selector:
            if detail:
                selector = "//body"
            else:
                selector = self.source["items_xpath"]
                if self.source.get("empty_xpath"):
                    selector = f"({selector}) | ({self.source['empty_xpath']})"
        try:
            self.progress("加载浏览器页面")
            pagination = self.options.get("pagination", {}) if not detail else {}
            if "response_path" in pagination:
                with self.expect_search(pagination) as pending:
                    response = self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
                self.verify_search(pending.value, pagination)
            else:
                response = self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            if response is None or response.status >= 400:
                raise ValueError(f"浏览器主页面 HTTP {response.status if response else '无响应'}")
            self.progress("等待岗位节点")
            self.page.locator("xpath=" + selector).first.wait_for(state="attached", timeout=self.timeout)
        except self.timeout_error:
            raise ValueError("浏览器等待岗位节点超时" + self.diagnostics()) from None
        except self.error:
            raise ValueError("浏览器加载招聘页面失败" + self.diagnostics()) from None
        finally:
            self.save_page(path)
        if not path.is_file():
            raise ValueError("渲染页面未保存或超过 8 MB")
        return path.read_bytes()

    def diagnostics(self):
        return "\uff08" + "\u3001".join(dict.fromkeys(self.failures))[:300] + "\uff09" if self.failures else ""
