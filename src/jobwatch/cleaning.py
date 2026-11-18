"""Normalize mapped fields into one representation for all sources."""
import html
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

FIELDS = ("id", "title", "location", "type", "url", "status", "description",
          "duties", "requirements", "bonus")
SECTIONS = {
    "工作职责": "duties", "岗位职责": "duties", "职位描述": "duties",
    "任职要求": "requirements", "岗位要求": "requirements", "职位要求": "requirements",
    "加分项": "bonus", "优先条件": "bonus",
}
SECTION_PATTERN = re.compile("|".join(SECTIONS))


def text(value):
    if isinstance(value, list):
        return " / ".join(filter(None, map(text, value)))
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    value = str(value)
    if re.search(r"&(?:lt|#0*60|#x0*3c);\s*/?[A-Za-z]", value, re.I):
        value = html.unescape(value)
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", value, flags=re.I | re.S)
    value = re.sub(r"<(?:br\b[^>]*|/(?:p|div|li|h[1-6]|tr)\s*)>", "\n", value, flags=re.I)
    value = html.unescape(re.sub(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^>]*)?/?>", "", value))
    return "\n".join(line for raw in value.splitlines() if (line := " ".join(raw.split())))


def split_sections(value):
    sections = dict.fromkeys(("duties", "requirements", "bonus"), "")
    matches = list(SECTION_PATTERN.finditer(value))
    if not matches:
        sections["duties"] = value
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(value)
        key = SECTIONS[match.group()]
        chunk = value[match.end():end].strip(" \n:\uff1a")
        sections[key] = (sections[key] + "\n" + chunk).strip()
    return sections


def web_url(value, base=""):
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value):
        raise ValueError("缺少有效的 HTTP(S) 地址")
    url = urlsplit(urljoin(base, value))
    if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
        raise ValueError("缺少有效的 HTTP(S) 地址")
    return urlunsplit(url)


def normalize(mapped, source, base=""):
    row = {field: text(mapped.get(field)) for field in FIELDS if field not in ("id", "url")}
    raw_id = mapped.get("id")
    row["id"] = str(raw_id).strip() if type(raw_id) in (str, int) else ""
    row["url"] = web_url(mapped["url"], base) if mapped.get("url") else ""
    row["id"] = row["id"] or row["url"]
    if not row["title"] or not row["id"]:
        raise ValueError("缺少岗位标题、稳定 ID 或链接")
    for field, value in split_sections(row.pop("description")).items():
        row[field] = row[field] or value
    row.update(company=source["company"], source=source["id"],
               status=row["status"] or "受理状态未知")
    return row
