#!/usr/bin/env python3
"""Read-only health check for the local second brain.

The script never edits vault files. It uses only the Python standard library;
Ruby is invoked opportunistically to validate Obsidian Base YAML when available.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SECOND_BRAIN = SCRIPT_DIR.parent
VAULT = SECOND_BRAIN.parent
TODAY = date.today()

DAILY_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
ALLOWED_LOG_COMPONENTS = {
    "ingest",
    "query",
    "lint",
    "schema",
    "index",
    "correction",
    "bases",
    "lambic",
    "prompt",
}
REQUIRED_CARD_KEYS = {
    "title",
    "type",
    "relation_id",
    "status",
    "relation_type",
    "confidence",
    "confidence_score",
    "priority",
    "actionable",
    "source_signals",
    "target_outcomes",
    "time_window",
    "current_conclusion",
    "action_rule",
    "next_observation",
    "last_reviewed",
    "next_review",
}


def finding(level: str, code: str, path: str, detail: str) -> dict[str, str]:
    return {"level": level, "code": code, "path": path, "detail": detail}


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(VAULT))
    except ValueError:
        return str(path)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def frontmatter_keys(text: str) -> set[str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return set()
    keys: set[str] = set()
    for line in lines[1:]:
        if line.strip() == "---":
            return keys
        match = FRONTMATTER_KEY_RE.match(line)
        if match:
            keys.add(match.group(1))
    return set()


def unescaped_pipe_count(line: str) -> int:
    return len(re.findall(r"(?<!\\)\|", line))


def validate_tables(path: Path, text: str, findings: list[dict[str, str]]) -> None:
    lines = text.splitlines()
    index = 0
    while index + 1 < len(lines):
        if lines[index].lstrip().startswith("|") and TABLE_SEPARATOR_RE.match(lines[index + 1]):
            expected = unescaped_pipe_count(lines[index])
            row = index + 2
            while row < len(lines) and lines[row].lstrip().startswith("|"):
                actual = unescaped_pipe_count(lines[row])
                if actual != expected:
                    findings.append(
                        finding(
                            "P1",
                            "TABLE_SHAPE",
                            f"{rel(path)}:{row + 1}",
                            f"表头有 {expected} 个未转义竖线，本行有 {actual} 个。",
                        )
                    )
                row += 1
            index = row
        else:
            index += 1


def build_target_maps(files: list[Path]) -> tuple[dict[str, list[Path]], set[str]]:
    by_stem: dict[str, list[Path]] = defaultdict(list)
    exact: set[str] = set()
    for path in files:
        by_stem[path.stem].append(path)
        exact.add(rel(path))
        exact.add(str(path.relative_to(VAULT).with_suffix("")))
    return by_stem, exact


def resolve_link(
    source: Path,
    raw_target: str,
    by_stem: dict[str, list[Path]],
    exact: set[str],
) -> tuple[str, list[Path]]:
    target = raw_target.replace("\\|", "|").split("|", 1)[0].split("#", 1)[0].strip()
    if not target or target.startswith("#"):
        return "same-note", []
    normalized = target.removesuffix(".md")
    candidates: list[Path] = []
    if "/" in normalized:
        for suffix in ("", ".md", ".base"):
            candidate = VAULT / f"{normalized}{suffix}"
            if candidate.is_file():
                candidates.append(candidate)
        relative_target = source.parent / normalized
        for suffix in ("", ".md", ".base"):
            candidate = Path(f"{relative_target}{suffix}")
            if candidate.is_file():
                candidates.append(candidate)
        target_stem = Path(normalized).stem
        for candidate in by_stem.get(target_stem, []):
            candidate_relative = rel(candidate)
            candidate_without_suffix = str(Path(candidate_relative).with_suffix(""))
            if candidate_relative.endswith(normalized) or candidate_without_suffix.endswith(normalized):
                candidates.append(candidate)
    else:
        stem = Path(normalized).stem
        candidates.extend(by_stem.get(stem, []))
    unique = sorted(set(candidates))
    if not unique:
        return "unresolved", []
    if len(unique) > 1:
        return "ambiguous", unique
    return "resolved", unique


def walk_source_paths(value: object, location: str = "$") -> list[tuple[str, list[str]]]:
    result: list[tuple[str, list[str]]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}"
            if key in {"source_paths", "source_dashboard_summary_paths"} and isinstance(child, list):
                string_values = [item for item in child if isinstance(item, str)]
                result.append((child_location, string_values))
            result.extend(walk_source_paths(child, child_location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(walk_source_paths(child, f"{location}[{index}]"))
    return result


def parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def parse_recorded_date(value: str, path: Path, findings: list[dict[str, str]]) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        findings.append(finding("P1", "INVALID_DATE", rel(path), f"无效日期：{value}。"))
        return None


def expected_weekly_reviews(daily_dates: list[date]) -> list[str]:
    if not daily_dates:
        return []
    first = min(daily_dates)
    monday = first + timedelta(days=(7 - first.weekday()) % 7)
    if monday == first and first.weekday() != 0:
        monday += timedelta(days=7)
    expected: list[str] = []
    while monday + timedelta(days=6) < TODAY:
        sunday = monday + timedelta(days=6)
        expected.append(f"{monday.isoformat()}至{sunday.isoformat()}周复盘.md")
        monday += timedelta(days=7)
    return expected


def run_lint() -> tuple[list[dict[str, str]], dict[str, object]]:
    findings: list[dict[str, str]] = []
    markdown_files = sorted(SECOND_BRAIN.rglob("*.md"))
    json_files = sorted(SECOND_BRAIN.rglob("*.json"))
    base_files = sorted(SECOND_BRAIN.rglob("*.base"))
    all_vault_files = [path for path in VAULT.rglob("*") if path.is_file()]
    linkable = [path for path in all_vault_files if path.suffix in {".md", ".base"}]
    by_stem, exact = build_target_maps(linkable)

    for path in markdown_files + json_files + base_files:
        if path.stat().st_size == 0:
            findings.append(finding("P1", "EMPTY_FILE", rel(path), "文件为空。"))

    daily_files = sorted((SECOND_BRAIN / "每日记录").glob("*.md"))
    daily_dates: list[date] = []
    for path in daily_files:
        match = DAILY_NAME_RE.match(path.name)
        if not match:
            continue
        day = parse_recorded_date(match.group(1), path, findings)
        if day is None:
            continue
        daily_dates.append(day)
        text = read_text(path)
        first_heading = next((line for line in text.splitlines() if line.startswith("# ")), "")
        if day.isoformat() not in first_heading:
            findings.append(
                finding("P1", "DAILY_H1_DATE", rel(path), "文件名日期与首个 H1 日期不一致。")
            )
    if daily_dates:
        expected_dates = {
            min(daily_dates) + timedelta(days=offset)
            for offset in range((max(daily_dates) - min(daily_dates)).days + 1)
        }
        missing = sorted(expected_dates - set(daily_dates))
        if missing:
            findings.append(
                finding(
                    "P2",
                    "DAILY_GAPS",
                    rel(SECOND_BRAIN / "每日记录"),
                    "缺少日期：" + ", ".join(day.isoformat() for day in missing),
                )
            )

    no_frontmatter = sum(1 for path in daily_files if not frontmatter_keys(read_text(path)))
    if no_frontmatter:
        findings.append(
            finding(
                "P3",
                "DAILY_UNSTRUCTURED",
                rel(SECOND_BRAIN / "每日记录"),
                f"{no_frontmatter}/{len(daily_files)} 份每日记录没有 YAML 属性；当前通过文件名与关系回链导航。",
            )
        )

    for path in markdown_files:
        text = read_text(path)
        validate_tables(path, text, findings)
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in WIKILINK_RE.finditer(line):
                status, candidates = resolve_link(path, match.group(1), by_stem, exact)
                if status == "unresolved":
                    findings.append(
                        finding(
                            "P1",
                            "UNRESOLVED_LINK",
                            f"{rel(path)}:{line_number}",
                            f"无法解析 [[{match.group(1)}]]。",
                        )
                    )
                elif status == "ambiguous":
                    findings.append(
                        finding(
                            "P2",
                            "AMBIGUOUS_LINK",
                            f"{rel(path)}:{line_number}",
                            f"[[{match.group(1)}]] 同时命中：" + ", ".join(rel(item) for item in candidates),
                        )
                    )

    cards = sorted((SECOND_BRAIN / "关系卡").glob("REL-*.md"))
    card_ids: list[str] = []
    for path in cards:
        text = read_text(path)
        keys = frontmatter_keys(text)
        missing = sorted(REQUIRED_CARD_KEYS - keys)
        if missing:
            findings.append(
                finding("P1", "CARD_SCHEMA", rel(path), "缺少属性：" + ", ".join(missing))
            )
        id_match = re.search(r"^relation_id:\s*(\S+)\s*$", text, re.MULTILINE)
        if id_match:
            card_ids.append(id_match.group(1))
    duplicate_ids = [item for item, count in Counter(card_ids).items() if count > 1]
    if duplicate_ids:
        findings.append(
            finding("P1", "DUPLICATE_RELATION_ID", rel(SECOND_BRAIN / "关系卡"), ", ".join(duplicate_ids))
        )

    for path in json_files:
        try:
            data = json.loads(read_text(path))
        except json.JSONDecodeError as exc:
            findings.append(finding("P1", "INVALID_JSON", rel(path), str(exc)))
            continue
        for location, values in walk_source_paths(data):
            duplicates = [item for item, count in Counter(values).items() if count > 1]
            if duplicates:
                findings.append(
                    finding(
                        "P2",
                        "DUPLICATE_SOURCE_PATH",
                        rel(path),
                        f"{location} 重复：" + ", ".join(duplicates),
                    )
                )
        generated = parse_iso_date(data.get("generated_at") if isinstance(data, dict) else None)
        if "dashboard-cache" in path.parts and generated:
            age = (TODAY - generated).days
            if age > 7:
                findings.append(
                    finding("P2", "STALE_DASHBOARD", rel(path), f"缓存已 {age} 天未刷新。")
                )

    if shutil.which("ruby"):
        ruby_code = (
            'require "yaml"; YAML.safe_load(File.read(ARGV[0]), permitted_classes: [], aliases: true)'
        )
        for path in base_files:
            result = subprocess.run(
                ["ruby", "-e", ruby_code, str(path)], capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                findings.append(
                    finding("P1", "INVALID_BASE_YAML", rel(path), result.stderr.strip() or "YAML 解析失败。")
                )
    elif base_files:
        findings.append(
            finding("P3", "BASE_YAML_SKIPPED", rel(SECOND_BRAIN), "未找到 Ruby，跳过 Base YAML 校验。")
        )

    index_path = SECOND_BRAIN / "wiki" / "index.md"
    if index_path.exists():
        outlinks = len(WIKILINK_RE.findall(read_text(index_path)))
        if outlinks == 0:
            findings.append(finding("P2", "INDEX_NO_LINKS", rel(index_path), "全局索引没有真实 wikilink。"))

    relation_rows = 0
    relation_rows_with_links = 0
    for path in (SECOND_BRAIN / "关系").glob("*.md"):
        for line in read_text(path).splitlines():
            if line.startswith("|") and DATE_RE.search(line):
                relation_rows += 1
                if "[[" in line:
                    relation_rows_with_links += 1
    if relation_rows and relation_rows_with_links < relation_rows:
        findings.append(
            finding(
                "P3",
                "LEGACY_RELATION_SOURCE_LINKS",
                rel(SECOND_BRAIN / "关系"),
                f"旧关系日志 {relation_rows_with_links}/{relation_rows} 条日期观察含 wikilink；新关系卡承担可追溯综合。",
            )
        )

    log_path = SECOND_BRAIN / "wiki" / "log.md"
    if log_path.exists():
        unknown_ops: Counter[str] = Counter()
        for line in read_text(log_path).splitlines():
            if not line.startswith("| 20"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                for component in cells[1].split("/"):
                    if component and component not in ALLOWED_LOG_COMPONENTS:
                        unknown_ops[component] += 1
        if unknown_ops:
            findings.append(
                finding(
                    "P2",
                    "LOG_ENUM",
                    rel(log_path),
                    "未知操作枚举：" + ", ".join(f"{key}({value})" for key, value in unknown_ops.items()),
                )
            )

    review_dir = SECOND_BRAIN / "复盘" / "每周复盘"
    existing_reviews = {path.name for path in review_dir.glob("*.md")}
    missing_reviews = [name for name in expected_weekly_reviews(daily_dates) if name not in existing_reviews]
    if missing_reviews:
        findings.append(
            finding(
                "P2",
                "MISSING_WEEKLY_REVIEWS",
                rel(review_dir),
                "缺少：" + ", ".join(missing_reviews),
            )
        )

    pending_path = SECOND_BRAIN / "索引" / "待确认问题.md"
    if pending_path.exists():
        text = read_text(pending_path)
        header_match = re.search(r"最后更新：(20\d{2}-\d{2}-\d{2})", text)
        parsed_dates = {
            value: parse_recorded_date(value, pending_path, findings)
            for value in dict.fromkeys(DATE_RE.findall(text))
        }
        row_dates = [day for day in parsed_dates.values() if day is not None and day <= TODAY]
        header_date = parsed_dates.get(header_match.group(1)) if header_match else None
        if header_date is not None and row_dates and header_date < max(row_dates):
            findings.append(
                finding(
                    "P2",
                    "PENDING_HEADER_STALE",
                    rel(pending_path),
                    f"最后更新为 {header_match.group(1)}，最新条目为 {max(row_dates).isoformat()}。",
                )
            )
        old_open = 0
        for line in text.splitlines():
            if not line.startswith("| Q-"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 4:
                continue
            source_match = DATE_RE.search(cells[2])
            closed = any(word in cells[3] for word in ("已确认", "已处理", "已闭合", "不再采用"))
            source_date = parsed_dates.get(source_match.group(1)) if source_match else None
            if source_date is not None and not closed:
                age = (TODAY - source_date).days
                if age > 21:
                    old_open += 1
        if old_open:
            findings.append(
                finding("P3", "OLD_PENDING_ITEMS", rel(pending_path), f"{old_open} 条未明确闭合项已超过 21 天。")
            )

    summary = {
        "markdown_files": len(markdown_files),
        "json_files": len(json_files),
        "base_files": len(base_files),
        "daily_files": len(daily_files),
        "relationship_cards": len(cards),
        "findings_by_level": dict(Counter(item["level"] for item in findings)),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "read_only": True,
    }
    return findings, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only second-brain health check")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="Return non-zero for P2 as well as P1")
    args = parser.parse_args()

    findings, summary = run_lint()
    if args.json:
        print(json.dumps({"summary": summary, "findings": findings}, ensure_ascii=False, indent=2))
    else:
        print("第二大脑只读 lint")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        for item in findings:
            print(f"[{item['level']}] {item['code']} | {item['path']} | {item['detail']}")

    levels = {item["level"] for item in findings}
    if "P1" in levels or (args.strict and "P2" in levels):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
