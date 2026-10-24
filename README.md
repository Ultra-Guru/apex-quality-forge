# 🔥 apex-quality-forge

[Ultra-Guru/Android-Guru-Agent](https://github.com/Ultra-Guru/Android-Guru-Agent) 的**独立代码质量验证仓** —— 主仓 CI 红灯哨兵 + 深度质量审计站。

## 为什么需要这个仓库

**#427 事件复盘**：一次 PR 漏拦让 `CodeViewModel.kt` 达到 1205/1200 行，主仓 main 的 Quality Gate 红灯持续数日无人察觉 —— 主仓自己的 CI 红了，只能靠人盯着主仓才看得见。质量验证需要一条**独立于被审对象之外**的哨兵链路：本仓库每日克隆主仓、用与主仓同口径（但独立实现、独立托管）的审计器复检全部硬门禁，红灯即开 issue 主动告警。

## 审计能力

### A. 门禁镜像（硬拦截，与主仓 scripts/ 同口径独立实现）

| 门禁 | 对齐主仓 | 说明 |
|------|----------|------|
| 文件预算 | `check_file_size.sh` | main ≤1200 / test ≤1600 行 |
| 空 catch | `check_code_quality.sh` GATE4 | 含 `_` 弃名参数变体（主仓 2026-10 已堵此漏洞） |
| 反射分发 / printStackTrace / 裸 collectAsState | GATE1-3 | 注释行豁免 |
| 括号平衡 | `kotlin_balance.py` | 同型词法状态机：模板 `${}` 嵌套 / raw 串 / 反引号标识符 |
| i18n 双镜像 | `check_string_mirror.py` | 键双向差集 + 占位符多重集（豁免 `app_name`） |

### B. 深度指标（趋势观察，不拦截）

- **预算余量预警榜** —— 最接近 1200 的文件 Top12（"下一个要爆的"）
- 超长函数热点（>80 行）/ 深嵌套热点（>6 层，≥400 行文件深扫）
- 技术债标记 / `!!` 断言 / `runBlocking` / `Thread.sleep` 存量（对齐主仓棘轮口径）

## GitHub Actions

| Workflow | 触发 | 作用 |
|----------|------|------|
| [Self-Test](.github/workflows/self-test.yml) | push / PR | pytest 12 用例（审计器自身正确性 = 一切审计的前提） |
| [Audit Main Repo](.github/workflows/audit.yml) | 每日 UTC 21:00 / 手动 / `repository_dispatch: audit-request` | 浅克隆主仓 → 全量审计 → 报告 artifact + 红灯开 issue（指纹去重） |

## 使用

```bash
# 本地审计任意 Kotlin 仓库
python3 audits/forge_audit.py /path/to/repo   # exit 0 = 绿 / 1 = 红

# 跑自测
pip install pytest && python3 -m pytest tests/ -v
```

## 口径同步纪律

主仓门禁规则演进时，本仓镜像需同步更新并补对应用例（自测是契约）。两边口径漂移会在每日审计里以「主仓绿/哨兵红」的假阳性形态暴露 —— 看见假阳性先核主仓脚本差异，再同步本仓。

## 质量口径验证记录

- 2026-10-24：对主仓 main（90c97f2 前态）审计 → **4 项红灯**（CodeViewModel 1205 超预算 + 3 处 `catch (_:) {}` 空 catch）—— 与主仓 Quality Gate 红灯完全一致，零误报；
- 同日对修复分支（PR Ultra-Guru/Android-Guru-Agent#435）审计 → **全绿**；
- **哨兵首战**（2026-10-24 05:24Z）：audit 红灯 → 自动开 [issue #1](https://github.com/Ultra-Guru/apex-quality-forge/issues/1)（指纹 `1f0645fc`，4 项红灯明细）→ 修复合入（737a4f53）→ 05:32Z 复审**转绿** → issue 关闭。红灯发现 → 告警 → 修复 → 复绿全链路 8 分钟。
