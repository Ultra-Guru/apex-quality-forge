# ═══════════════════════════════════════════════════════════════════════════
# test_forge_audit.py — 审计器自测（GitHub Actions self-test 跑本套）
#
# 策略：tmp_path 造微型假仓库，monkeypatch 预算常量到小值，逐门禁验证
# 「应红必红 / 应绿必绿」，最后全量绿树回归。
# ═══════════════════════════════════════════════════════════════════════════
import sys
from pathlib import Path

import forge_audit as fa  # noqa: E402  (conftest 已装路径)
import pytest


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def small_budgets(monkeypatch):
    """预算缩到 10 行，让夹具无需巨型文件即可触发超预算。"""
    monkeypatch.setattr(fa, "MAX_MAIN_LINES", 10)
    monkeypatch.setattr(fa, "MAX_TEST_LINES", 14)


def test_clean_repo_passes(tmp_path):
    """绿树：小文件 + 平衡括号 + i18n 双镜像齐全 → 零硬红灯。"""
    root = make_repo(tmp_path, {
        "app/src/main/kotlin/Foo.kt": "class Foo {\n    fun bar(): Int = 1\n}\n",
        "app/src/test/kotlin/FooTest.kt": "class FooTest {\n    fun t() = assert(1 == 1)\n}\n",
        "app/src/main/res/values/strings.xml": '<resources>\n    <string name="hello">Hello %1$s</string>\n    <string name="app_name">Forge</string>\n</resources>\n',
        "app/src/main/res/values-zh/strings.xml": '<resources>\n    <string name="hello">你好 %1$s</string>\n    <string name="app_name">Forge</string>\n</resources>\n',
    })
    res = fa.audit(root)
    assert res.hard_failures == [], res.hard_failures


def test_file_size_budget(tmp_path):
    """主文件超预算必红；测试文件预算独立（14 > 10 不误伤主预算文件）。"""
    big = "class Big {\n" + "".join(f"    val v{i} = {i}\n" for i in range(20)) + "}\n"
    root = make_repo(tmp_path, {"app/src/main/kotlin/Big.kt": big})
    res = fa.audit(root)
    assert any("Big.kt" in f and "超预算" in f for f in res.hard_failures)


def test_empty_catch_variants(tmp_path):
    """空 catch 四变体必红：具名 / 弃名(_) / 花括号带空格 / 注释行豁免。"""
    root = make_repo(tmp_path, {
        "app/src/main/kotlin/Catches.kt": (
            "object Catches {\n"
            "    // 旧实现 catch (_: Exception) {} —— 注释豁免\n"
            "    fun a() { try { x() } catch (e: IllegalStateException) { } }\n"
            "    fun b() { try { x() } catch (_: Throwable) {} }\n"
            "    fun c() { try { x() } catch (err: Exception) {   } }\n"
            "}\n"
        ),
    })
    res = fa.audit(root)
    hits = [f for f in res.hard_failures if "空 catch" in f]
    # 注释行豁免；a/b/c 三处实义违规
    assert len(hits) == 3, hits


def test_anti_patterns(tmp_path):
    """反射分发 / printStackTrace / 裸 collectAsState 必红。"""
    root = make_repo(tmp_path, {
        "app/src/main/kotlin/Bad.kt": (
            "object Bad {\n"
            "    fun r() = javaClass.getMethod(\"x\")\n"
            "    fun p(e: Throwable) = e.printStackTrace()\n"
            "    fun c() = state.collectAsState()\n"
            "}\n"
        ),
    })
    res = fa.audit(root)
    assert any("javaClass.getMethod" in f for f in res.hard_failures)
    assert any("printStackTrace" in f for f in res.hard_failures)
    assert any("collectAsState" in f for f in res.hard_failures)


def test_bracket_balance(tmp_path):
    """括号失衡必红；字符串/注释里的花括号不误报。"""
    root = make_repo(tmp_path, {
        "app/src/main/kotlin/Unbalanced.kt": (
            "class Unbalanced {\n"
            "    val s = \"{\"  // 字符串里的花括号不算\n"
            "    /* } 注释里也不算 */\n"
            "    fun a() {\n"
            "}\n"  # ← 少一层闭合
        ),
    })
    res = fa.audit(root)
    assert any("括号失衡" in f for f in res.hard_failures)


def test_i18n_mirror(tmp_path):
    """i18n：缺键 / 占位符不匹配 / 多余键 必红；app_name 豁免。"""
    root = make_repo(tmp_path, {
        "app/src/main/res/values/strings.xml": (
            '<resources>\n    <string name="ok">OK</string>\n'
            '    <string name="fmt">a %1$s b</string>\n'
            '    <string name="app_name">Forge</string>\n</resources>\n'
        ),
        # zh：缺 fmt（→ 缺键）；带多余 extra；ok 完好
        "app/src/main/res/values-zh/strings.xml": (
            '<resources>\n    <string name="ok">好</string>\n'
            '    <string name="extra">多余</string>\n</resources>\n'
        ),
    })
    res = fa.audit(root)
    joined = "\n".join(res.hard_failures)
    assert "缺键" in joined and "`fmt`" in joined
    assert "多余键" in joined and "extra" in joined
    assert "app_name" not in joined


def test_build_dirs_excluded(tmp_path):
    """build/.git 目录不扫（生成物误报防线）。"""
    root = make_repo(tmp_path, {
        "app/src/main/kotlin/Ok.kt": "class Ok\n",
        "app/build/tmp/kotlin/Generated.kt": "class Generated {\n" + "x" * 999 + "\n",
        "vendor/.git/keep.kt": "class Keep {\n",
    })
    res = fa.audit(root)
    assert res.hard_failures == []


def test_metrics_depth(tmp_path):
    """深度指标：技术债 / !! / runBlocking / Thread.sleep 计数正确。"""
    root = make_repo(tmp_path, {
        "app/src/main/kotlin/Debt.kt": (
            "object Debt {\n"          # 1
            "    // TODO fix\n"        # 2
            "    val a = map!!\n"      # 3
            "    // FIXME now\n"       # 4
            "    val b = set!!\n"      # 5
            "}\n"                      # 6
        ),
        "app/src/test/kotlin/Blocking.kt": (
            "class Blocking {\n"                    # 1
            "    fun t() {\n"                        # 2
            "        runBlocking { }\n"              # 3
            "        Thread.sleep(1)\n"              # 4
            "    }\n"                               # 5
            "}\n"                                   # 6
        ),
    })
    res = fa.audit(root)
    m = {Path(m_.path).name: m_ for m_ in res.metrics}
    assert m["Debt.kt"].tech_debt == 2
    assert m["Debt.kt"].bang_bang == 2
    assert m["Blocking.kt"].run_blocking == 1
    assert m["Blocking.kt"].thread_sleep == 1


def test_long_function_detection(tmp_path):
    """超长函数探测：>80 行的函数被点名（深扫阈值 400 行）。"""
    body = "\n".join(f"        val filler{i} = {i}" for i in range(90))
    padding = "\n".join(f"// 凑行数到深扫阈值（{i}）" for i in range(330))
    root = make_repo(tmp_path, {
        "app/src/main/kotlin/Long.kt": (
            f"class Long {{\n{padding}\n    fun giant() {{\n" + body + "\n    }\n}\n"
        ),
    })
    res = fa.audit(root)
    m = next(m_ for m_ in res.metrics if m_.path.name == "Long.kt")
    assert m.long_functions and "giant" in m.long_functions[0]


def test_nesting_depth(tmp_path):
    """深嵌套探测：8 层花括号被点名（深扫阈值 400 行）。"""
    src = "class Deep {\n    fun d() {\n"
    depth = 8
    src += "".join("    " * (2 + i) + "if (true) {\n" for i in range(depth))
    src += "".join("    " * (1 + depth - i) + "}\n" for i in range(depth))
    src += "    }\n}\n"
    padding = "\n".join(f"// 凑行数到深扫阈值（{i}）" for i in range(400))
    root = make_repo(tmp_path, {"app/src/main/kotlin/Deep.kt": src + padding + "\n"})
    res = fa.audit(root)
    m = next(m_ for m_ in res.metrics if m_.path.name == "Deep.kt")
    assert m.deep_nesting, m.deep_nesting


def test_report_renders(tmp_path):
    """报告渲染：红/绿两种结论都能出 Markdown。"""
    root = make_repo(tmp_path, {"app/src/main/kotlin/Ok.kt": "class Ok\n"})
    report = fa.render_report(root, fa.audit(root))
    assert "硬门禁全绿" in report

    big = "class Big {\n" + "".join(f"    val v{i} = {i}\n" for i in range(20)) + "}\n"
    root2 = make_repo(tmp_path / "r2", {"app/src/main/kotlin/Big.kt": big})
    report2 = fa.render_report(root2, fa.audit(root2))
    assert "硬红灯" in report2


def test_exit_code_contract(tmp_path, capsys):
    """CLI 契约：绿 0 / 红 1；报告落盘 audit-report.md。"""
    import os
    import subprocess
    root = make_repo(tmp_path, {"app/src/main/kotlin/Ok.kt": "class Ok\n"})
    audits_dir = Path(fa.__file__).parent
    env = {**os.environ, "PYTHONPATH": f"{audits_dir}{os.pathsep}{audits_dir.parent}"}
    out = subprocess.run(
        [sys.executable, str(audits_dir / "forge_audit.py"), str(root)],
        cwd=tmp_path, capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "audit-report.md").exists()
