# JobWatch 岗位观察：项目介绍与使用说明

**JobWatch** 是一款面向求职者的本地命令行招聘追踪与岗位分析工具（需 Python 3.11+）。它支持持续监控**单家目标企业 (`company`)** 的招聘变动，也支持按**岗位方向 (`role`)** 在多家企业招聘站之间横向筛选机会，并结合本地规则与大模型（OpenAI / DeepSeek）输出带有**原文出处证据**的匹配理由与技能要求。

## 一、核心亮点

- **双模式追踪与增量对比**：支持单企业全量监控与跨企业按方向筛选，基于本地 SQLite 快照自动识别**新增**、**修改**与**本次未检出**岗位。
- **多源合规采集**：内置 `json_file`、`json_http`（GET/POST 分页、内嵌脚本 JSON）与 `html_http`（XPath 提取、Playwright 无头浏览器动态渲染与独立进程超时保护），自动遵守 `robots.txt` 与请求间隔。
- **牛客发现与自动接入**：支持通过 `discover` 从牛客提取企业站外招聘链接，并用 `onboard` 自动探测百度校招、Greenhouse、Lever、Ashby 等招聘板生成配置。
- **防幻觉 AI 证据核验**：支持按行号区间无损切分混合正文（`ai_clean`），AI 分析结论强制校验原文片段编号与引文，杜绝模型改写或编造要求。

## 二、快速上手与常用命令

进入项目根目录后创建虚拟环境并安装（如需采集动态渲染网页，将 `pip install .` 替换为 `pip install ".[browser]"` 并执行 `playwright install chromium --only-shell`）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m jobwatch demo --output demo-output
```

常用子命令速览：

| 命令 | 说明 |
| :--- | :--- |
| `jobwatch demo --output demo-output` | 使用内置合成企业数据运行两轮离线演示，无需联网或密钥。 |
| `jobwatch check --config <配置路径>` | 离线检查任务配置、来源映射与访问确认标志（`access_reviewed`）。 |
| `jobwatch run --config <配置路径> --task <任务名>` | 执行采集、清洗与对比（可加 `--scan quick/full` 或 `--ai`）。 |
| `jobwatch discover --company <企业名> --output <目录>` | 从牛客公开页面提取企业站外招聘官网候选链接。 |
| `jobwatch onboard <链接或leads.json> --access-reviewed --output <目录>` | 自动试采目标招聘站（最多 2 页 / 2 条详情）并导出可运行配置。 |

## 三、典型配置样例

实际使用时，可参考 `examples/` 下的模板创建自己的 `*.local.json` 配置文件（已被 `.gitignore` 忽略，不会误提交）。

### 样例 1：百度校招接口采集（单企业追踪 + 表单分页）

**来源配置 `examples/baidu.sources.local.json`**：

```json
[
  {
    "id": "baidu",
    "company": "百度",
    "adapter": "json_http",
    "url": "https://talent.baidu.com/httservice/getPostListNew",
    "careers_url": "https://talent.baidu.com/jobs/list",
    "access_reviewed": true,
    "interval_seconds": 3,
    "max_pages": 200,
    "items_path": "data.list",
    "total_path": "data.total",
    "pagination": {"page_param": "curPage", "size_param": "pageSize", "page_size": 10},
    "query_form": {
      "fields": {"recruitType": "GRADUATE", "keyWord": "", "projectType": ""},
      "success_path": "status",
      "success_value": "ok",
      "page_path": "data.pageNum"
    },
    "job_url_template": "/jobs/detail/GRADUATE/{id}",
    "fields": {
      "id": "postId",
      "title": "name",
      "location": "workPlace",
      "type": "projectType",
      "duties": "workContent",
      "requirements": "serviceCondition"
    }
  }
]
```

**任务配置 `examples/baidu.watch.local.json`** 与运行命令：

```json
{
  "output": "../output/baidu-campus",
  "registry": "baidu.sources.local.json",
  "tasks": {
    "baidu-campus": {"mode": "company", "sources": ["baidu"]}
  }
}
```

```powershell
.\.venv\Scripts\python.exe -m jobwatch run --config examples/baidu.watch.local.json --task baidu-campus --scan full
```

### 样例 2：跨企业方向筛选 + DeepSeek AI 岗位分析

**任务配置 `examples/watch.deepseek.local.json`** 与运行命令：

```json
{
  "output": "../output/deepseek-example",
  "registry": "sources.offline.json",
  "tasks": {
    "ai-apps": {
      "mode": "role",
      "sources": "all",
      "direction": "AI 应用开发实习",
      "keywords": ["AI 应用开发", "大模型应用开发", "Agent 开发"],
      "candidate_keywords": ["后端开发"],
      "types": ["实习"],
      "ai_clean": true
    }
  },
  "ai": {
    "provider": "deepseek",
    "model": "deepseek-flash",
    "batch_size": 2,
    "max_requests": 3
  }
}
```

```powershell
.\.venv\Scripts\python.exe -m jobwatch run --config examples/watch.deepseek.local.json --task ai-apps --ai
```

## 四、主要输出文件

每次运行结果保存在任务配置的 `output` 目录下：
- `cleaned.csv`：本轮采集并完成去重、正文章节切分后的全部标准岗位数据。
- `jobs.csv`：命中当前任务筛选条件或通过 AI 核验的候选岗位及匹配理由。
- `changes.csv` / `changes.json`：同条件下与上一轮快照的新增、修改与未检出对比。
- `report.md` / `summary.json`：可读的汇总报告与脚本可解析的运行状态摘要。
- `watch.sqlite3`：本地历史快照与已校验的 AI 缓存数据库。
