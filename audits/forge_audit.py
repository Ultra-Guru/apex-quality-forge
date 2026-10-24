#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════
# forge_audit.py — 主仓（Android-Guru-Agent）代码质量独立审计器
#
# 定位：主仓 CI 红灯哨兵 + 深度质量审计。两类能力：
#   A. 门禁镜像 —— 与主仓 scripts/ 同口径的硬门禁（file_size / empty-catch
#      四反模式 / 括号平衡 / i18n 双镜像），独立实现，避免单点失明：
#      主仓 PR 漏拦（如 #427 事件：CodeViewModel 1205/1205→红灯三天无人知）
#      时，本审计每日独立复检并开 issue 告警。
#   B. 深度指标 —— 主仓没有的增量体检：文件预算余量预警榜（下一个要爆的
#      文件）、函数长度/嵌套深度热点、技术债标记、`!!` 断言密度、
#      runBlocking/Thread.sleep 存量（对齐主仓 ratchet 口径）。
#
# 输出：exit 0 = 硬门禁全绿（指标仅报告不拦截）；exit 1 = 有硬红灯。
# 报告：Markdown（stdout + GITHUB_STEP_SUMMARY）。
#
# 纯 stdlib 实现；Kotlin 词法剥离与主仓 kotlin_balance.py 同型（字符串/
# 字符/注释剥离后计数括号）。
# ═══════════════════════════════════════════════════════════════════════════
from __future__ import annotations

import io
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# ── 与主仓对齐的预算（check_file_size.sh）───────────────────────────────────
MAX_MAIN_LINES = 1200
MAX_TEST_LINES = 1600

# ── 与主仓对齐的反模式正则（check_code_quality.sh；GATE4 已含下划线堵漏）───
RE_EMPTY_CATCH = re.compile(r"catch \([a-zA-Z. :_]+\) \{ *\}")
RE_REFLECTIVE = re.compile(r"javaClass\.getMethod")
RE_PRINT_STACK = re.compile(r"printStackTrace")
RE_BARE_COLLECT = re.compile(r"collectAsState\( *\)")
RE_COMMENT_LINE = re.compile(r"^[ \t]*(?:\*|//)")

# ── i18n 镜像口径（check_string_mirror.py 同型）：仅 app/src/main/res；豁免键 ───
I18N_RES_DIR = Path("app/src/main/res")
I18N_EXEMPT_KEYS = {"app_name"}
PLACEHOLDER = re.compile(r"%\d+\$[sd]|%[sd]|%%")

TECH_DEBT = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
BANG_BANG = re.compile(r"!!")


def find_kt_files(root: Path) -> list[Path]:
    """主仓同型发现：*.kt，src/main 与 src/test|androidTest 分流，剔除 build/.git。"""
    main_files, test_files = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        parts = Path(dirpath).parts
        if any(seg in ("build", ".git") for seg in parts):
            dirnames[:] = []
            continue
        for fn in filenames:
            if not fn.endswith(".kt"):
                continue
            p = Path(dirpath) / fn
            in_main = any(seg == "main" and prev == "src" for prev, seg in zip(parts, parts[1:]))
            in_test = any(seg in ("test", "androidTest") and prev == "src" for prev, seg in zip(parts, parts[1:]))
            if in_main:
                main_files.append(p)
            elif in_test:
                test_files.append(p)
    return sorted(main_files) + sorted(test_files), main_files, test_files


# ═══ Kotlin 词法剥离（与主仓 kotlin_balance.py 同型状态机，独立转录）══════
# 为什么不用简化剥离器：字符串模板 "${...}"（可嵌套串/模板/lambda）、
# raw 三引号串、反引号标识符、字符字面量都会让简化版产生结构性括号
# 假阳性 —— 对主仓全仓实测简化版误报 22 个文件、同型版 0 误报。

def strip_kotlin(src: str, keep_all: bool = False) -> str:
    """Kotlin 词法剥离：默认只留结构性 (){}；keep_all=True 留全部代码文本
    （去注释/字符串内容，模板内真代码保留 —— 供函数探测找 fun 声明）。"""
    out: list[str] = []
    stack: list[tuple[str, int]] = []   # (外层模式, 外层模板花括号深度)
    mode = "code"                       # code | str | raw | tmpl
    depth = 0                           # 当前 tmpl 模式内的花括号嵌套深度
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if mode in ("code", "tmpl"):
            if c == "/" and nxt == "/":                 # 行注释
                j = src.find("\n", i)
                i = n if j == -1 else j
            elif c == "/" and nxt == "*":               # 块注释
                j = src.find("*/", i + 2)
                i = n if j == -1 else j + 2
            elif c == '"':                              # 引号连跑：raw(3+) 或普通
                j = i
                while j < n and src[j] == '"':
                    j += 1
                run = j - i
                stack.append((mode, depth))
                mode = "raw" if run >= 3 else "str"
                i += 3 if run >= 3 else 1
            elif c == "`":                              # 反引号标识符整段跳过
                j = src.find("`", i + 1)
                i = n if j == -1 else j + 1
            elif c == "'":                              # 字符字面量
                j = i + 1
                while j < n and src[j] != "'":
                    if src[j] == "\\":
                        j += 1
                    j += 1
                i = j + 1
            elif mode == "tmpl" and c == "{":           # 模板内块/lambda 开
                depth += 1
                out.append(c)
                i += 1
            elif mode == "tmpl" and c == "}":
                if depth > 0:                           # lambda 收尾 = 真代码
                    depth -= 1
                    out.append(c)
                else:                                   # 模板收尾 → 恢复外层
                    mode, depth = stack.pop() if stack else ("code", 0)
                i += 1
            else:
                if keep_all or c in "(){}":
                    out.append(c)
                i += 1
        elif mode == "str":
            if c == "\\":
                i += 2
            elif c == '"':
                mode, depth = stack.pop() if stack else ("code", 0)
                i += 1
            elif c == "$" and nxt == "{":               # 串内模板开（压栈）
                stack.append(("str", 0))
                mode, depth = "tmpl", 0
                i += 2
            elif c == "$":                              # $ident 简单引用
                i += 1
                while i < n and (src[i].isalnum() or src[i] == "_"):
                    i += 1
            else:
                i += 1
        elif mode == "raw":
            if c == '"':
                j = i
                while j < n and src[j] == '"':
                    j += 1
                if j - i >= 3:                          # 引号连跑 ≥3 = 关界
                    i = j
                    mode, depth = stack.pop() if stack else ("code", 0)
                else:                                   # 内容引号
                    i = j
            elif c == "$" and nxt == "{":               # raw 内模板开
                stack.append(("raw", 0))
                mode, depth = "tmpl", 0
                i += 2
            elif c == "$":
                i += 1
                while i < n and (src[i].isalnum() or src[i] == "_"):
                    i += 1
            else:
                i += 1
    return "".join(out)


def bracket_balance(stripped: str) -> tuple[int, int]:
    """返回 (花括号差, 圆括号差)；0/0 = 平衡。"""
    return stripped.count("{") - stripped.count("}"), stripped.count("(") - stripped.count(")")


# ═══ i18n 双镜像（与 check_string_mirror.py 同口径）════════════════════════

def parse_string_keys(xml_path: Path) -> dict[str, list[str]]:
    """strings*.xml → {name: [占位符…]}。解析失败记为 {__parse_error__: []}。"""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return {"__parse_error__": []}
    keys: dict[str, list[str]] = {}
    for el in tree.getroot().iter("string"):
        name = el.get("name")
        if name and name not in I18N_EXEMPT_KEYS:
            text = "".join(el.itertext())
            keys[name] = PLACEHOLDER.findall(text)
    return keys


def i18n_mirror(res_root: Path) -> list[str]:
    """values/strings*.xml ↔ values-zh/strings*.xml：键与占位符双向镜像（主仓同口径）。"""
    problems: list[str] = []
    base_dir, zh_dir = res_root / "values", res_root / "values-zh"
    if not base_dir.is_dir():
        return problems
    base_files = sorted(p.name for p in base_dir.glob("strings*.xml"))
    zh_files = set(p.name for p in zh_dir.glob("strings*.xml")) if zh_dir.is_dir() else set()

    for name in base_files:
        base_keys = parse_string_keys(base_dir / name)
        zh_keys = parse_string_keys(zh_dir / name) if name in zh_files else None
        if "__parse_error__" in base_keys:
            problems.append(f"values/{name}: XML 解析失败")
            continue
        if zh_keys is None:
            problems.append(f"values-zh/{name}: 缺失")
            continue
        if "__parse_error__" in zh_keys:
            problems.append(f"values-zh/{name}: XML 解析失败")
            continue
        for k, ph in base_keys.items():
            if k not in zh_keys:
                problems.append(f"values-zh/{name}: 缺键 `{k}`")
            elif sorted(zh_keys[k]) != sorted(ph):
                # 多重集比较（sorted）：中英文参数顺序可合法调换（%2$s 在前）
                problems.append(f"`{k}` 占位符不匹配: {ph} ↔ {zh_keys[k]}")
        for k in zh_keys:
            if k not in base_keys:
                problems.append(f"values-zh/{name}: 多余键 `{k}`")
    for name in sorted(zh_files - set(base_files)):
        problems.append(f"values-zh/{name}: 无基线对应文件")
    return problems


# ═══ 深度指标 ═════════════════════════════════════════════════════════════

@dataclass
class FileMetrics:
    path: Path
    lines: int
    budget: int
    tech_debt: int = 0
    bang_bang: int = 0
    run_blocking: int = 0
    thread_sleep: int = 0
    long_functions: list[str] = field(default_factory=list)
    deep_nesting: list[str] = field(default_factory=list)


def longest_function(src: str) -> tuple[str, int]:
    """粗粒度最长函数探测（fun 声明起，至配平右花括号）。

    行首锚定用 [ \\t]（严禁 \\s —— 它能吞掉任意多个空行，把匹配起点
    漂移到函数声明前几百行之外，测得伪 421 行 <anon> 巨函数）。
    """
    stripped = strip_kotlin(src, keep_all=True)
    best_name, best_len = "", 0
    pat = re.compile(
        r"^[ \t]*(?:private |internal |public |protected |open |override |"
        r"suspend |inline |operator |infix |tailrec |external |actual |"
        r"expect |composable |@[\w.]+(?:\([^)]*\))? )*[ \t]*fun[ \t]+[<(\w]",
        re.M,
    )
    for m in pat.finditer(stripped):
        start = m.start()
        nm = re.search(r"fun[ \t]+(?:[\w.<>?]+\.)?(\w+)", stripped[start:start + 120])
        name = nm.group(1) if nm else "<anon>"
        depth, j = 0, start
        opened = False
        while j < len(stripped):
            ch = stripped[j]
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth == 0:
                    break
            j += 1
        body = stripped[start:j]
        n_lines = body.count("\n")
        if n_lines > best_len:
            best_name, best_len = name, n_lines
    return best_name, best_len


def max_nesting(src: str) -> int:
    stripped = strip_kotlin(src)
    depth = best = 0
    for ch in stripped:
        if ch == "{":
            depth += 1
            best = max(best, depth)
        elif ch == "}":
            depth = max(0, depth - 1)
    return best


def scan_metrics(files: list[Path], is_main: set[Path]) -> list[FileMetrics]:
    out: list[FileMetrics] = []
    for p in files:
        src = p.read_text(encoding="utf-8", errors="replace")
        lines = src.count("\n") + (0 if src.endswith("\n") or not src else 1)
        budget = MAX_MAIN_LINES if p in is_main else MAX_TEST_LINES
        m = FileMetrics(p, lines, budget)
        m.tech_debt = len(TECH_DEBT.findall(src))
        m.bang_bang = len(BANG_BANG.findall(src))
        m.run_blocking = len(re.findall(r"\brunBlocking\b", src))
        m.thread_sleep = len(re.findall(r"\bThread\.sleep\b", src))
        if lines >= 400:  # 深扫仅对大文件（性能 + 信噪比）
            fname, flen = longest_function(src)
            if flen > 80:
                m.long_functions = [f"{fname}() {flen} 行"]
            if max_nesting(src) > 6:
                m.deep_nesting = [f"嵌套 {max_nesting(src)} 层"]
        out.append(m)
    return out


# ═══ 主流程 ════════════════════════════════════════════════════════════════

@dataclass
class AuditResult:
    hard_failures: list[str] = field(default_factory=list)
    metrics: list[FileMetrics] = field(default_factory=list)
    i18n_problems: list[str] = field(default_factory=list)


def audit(root: Path) -> AuditResult:
    res = AuditResult()
    files, main_files, test_files = find_kt_files(root)
    is_main = set(main_files)

    # A1 文件预算
    for p in files:
        lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        budget = MAX_MAIN_LINES if p in is_main else MAX_TEST_LINES
        if lines > budget:
            res.hard_failures.append(f"📐 {rel(root, p)} {lines}/{budget} 行超预算")

    # A2 四反模式 + A3 括号平衡（仅 main，与主仓口径一致）
    for p in main_files:
        src = p.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(src.splitlines(), 1):
            if RE_COMMENT_LINE.match(line):
                continue
            if RE_EMPTY_CATCH.search(line):
                res.hard_failures.append(f"🕳️ {rel(root, p)}:{lineno} 空 catch")
            if RE_REFLECTIVE.search(line):
                res.hard_failures.append(f"🪞 {rel(root, p)}:{lineno} javaClass.getMethod 反射分发")
            if RE_PRINT_STACK.search(line):
                res.hard_failures.append(f"🖨️ {rel(root, p)}:{lineno} printStackTrace")
            if RE_BARE_COLLECT.search(line):
                res.hard_failures.append(f"🔄 {rel(root, p)}:{lineno} 裸 collectAsState()")
        curly, paren = bracket_balance(strip_kotlin(src))
        if curly or paren:
            res.hard_failures.append(f"⚖️ {rel(root, p)} 括号失衡 {curly:+d}{{}}/{paren:+d}()")

    # A4 i18n 双镜像（与主仓同口径：仅 app/src/main/res）
    app_res = root / I18N_RES_DIR
    if app_res.is_dir():
        res.i18n_problems.extend(i18n_mirror(app_res))
    res.hard_failures.extend(f"🌐 {msg}" for msg in res.i18n_problems)

    # B 深度指标（不拦截）
    res.metrics = scan_metrics(files, is_main)
    return res


def rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def render_report(root: Path, res: AuditResult) -> str:
    lines = ["# 🔥 apex-quality-forge 审计报告", ""]
    verdict = "✅ **硬门禁全绿**" if not res.hard_failures else f"❌ **{len(res.hard_failures)} 项硬红灯**"
    lines += [f"**结论：{verdict}**", ""]

    if res.hard_failures:
        lines += ["## ❌ 硬红灯（门禁镜像）", ""]
        lines += [f"- {f}" for f in res.hard_failures[:60]]
        if len(res.hard_failures) > 60:
            lines.append(f"- …另有 {len(res.hard_failures) - 60} 项")
        lines.append("")

    ms = sorted(res.metrics, key=lambda m: (m.lines / m.budget), reverse=True)
    hot = [m for m in ms if m.budget == MAX_MAIN_LINES and m.lines >= 900][:12]
    if hot:
        lines += ["## 📐 预算余量预警（下一个要爆的文件）", "", "| 余量 | 行数/预算 | 文件 |", "|---:|---|---|"]
        lines += [f"| {m.budget - m.lines} | {m.lines}/{m.budget} | `{rel(root, m.path)}` |" for m in hot]
        lines.append("")

    lf = [m for m in ms if m.long_functions][:10]
    if lf:
        lines += ["## 📏 超长函数热点（>80 行）", ""] + [f"- `{rel(root, m.path)}` {m.long_functions[0]}" for m in lf] + [""]

    dn = [m for m in ms if m.deep_nesting][:10]
    if dn:
        lines += ["## 🌀 深嵌套热点（>6 层）", ""] + [f"- `{rel(root, m.path)}` {m.deep_nesting[0]}" for m in dn] + [""]

    td = sum(m.tech_debt for m in ms)
    bb = sum(m.bang_bang for m in ms)
    rb = sum(m.run_blocking for m in ms)
    ts = sum(m.thread_sleep for m in ms)
    lines += [
        "## 📊 存量指标（趋势观察，不拦截）", "",
        f"- 技术债标记（TODO/FIXME/HACK/XXX）：**{td}**",
        f"- `!!` 非空断言：**{bb}**",
        f"- `runBlocking`：**{rb}**（主仓棘轮基线 696）",
        f"- `Thread.sleep`：**{ts}**（主仓棘轮基线 33）",
        f"- Kotlin 文件：main **{sum(1 for m in ms if m.budget == MAX_MAIN_LINES)}** + "
        f"test **{sum(1 for m in ms if m.budget == MAX_TEST_LINES)}**",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    res = audit(root)
    report = render_report(root, res)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(report, encoding="utf-8")
    Path("audit-report.md").write_text(report, encoding="utf-8")
    return 1 if res.hard_failures else 0


if __name__ == "__main__":
    sys.exit(main())
