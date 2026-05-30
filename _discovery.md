# `_discovery.md` — EDGAR MCP server 勘探 + API 核对

> Step 0 / Step 1 的产出。**先读这份再读代码**。给 Jason review 用。

---

## 0. 一个必须先说清楚的现实（重要）

施工方案假设存在一个 `mcp.ksinq.com` 多 server 栈（`tdnet` + `edinet` + 反代/gateway + systemd）。
**但本次 Claude Code 被授权的仓库只有 `hjjkkl/edinet-mcp` 这一个，里面只有 `edinet` 一个 server，
没有 `tdnet`、没有 nginx/caddy gateway 配置、没有 systemd unit。**

也就是说：**这个仓库不是「栈」，它是单个发布到 PyPI 的 Python 包 `edinet-mcp`**，
transport 是 **stdio**（给 Claude Desktop 用 `uvx edinet-mcp serve`），代码里另有 `sse` 选项。
`/servers/<name>/mcp` 这种 Streamable HTTP 对外路径、反代注册、进程管理，
**都不在这个仓库里**（它们应该在 Jason 本机 / 另一个部署仓库里，Code 看不到）。

**因此本次的诚实落地是**：在本仓库里按 `edinet_mcp` 包的全部约定，
**新增一个平行的 `edgar_mcp` 包**（同样的 FastMCP + pydantic-settings + click CLI + loguru 风格），
让它既能 `edgar-mcp serve`（stdio，给 Claude Desktop），也能 `edgar-mcp serve --transport http`
（Streamable HTTP，给 mcp.ksinq.com 反代）。**真正接进反代/进程管理那一步，需要 Jason 在部署机上做**
（见 Step 5 注记），因为那些配置不在这个仓库。

---

## 1. 现有 `edinet` server 约定（照抄对象）

| 维度 | 现状 | `edgar_mcp` 照抄 |
|---|---|---|
| **框架** | `fastmcp>=2.0,<3.0`（lock 锁 `2.14.5`），官方 `mcp` SDK 作为 fastmcp 依赖 | 同，复用 `from fastmcp import FastMCP` |
| **目录结构** | `src/edinet_mcp/`，一个包；tool 用 `@mcp.tool()` 装饰器注册在 `server.py`；`mcp = FastMCP(name=..., lifespan=..., instructions=...)` | `src/edgar_mcp/` 平行包，结构 1:1 对应 |
| **client 单例** | `server.py` 里 `_get_client()` 双检锁 + `asyncio.Lock`，`_lifespan` 收尾 close | 同 |
| **配置** | `_config.py` 用 `pydantic_settings.BaseSettings`，`get_settings()`；env 无前缀，读 `.env` | 同，新增 `EDGAR_*` 字段 |
| **CLI** | `cli.py` 用 `click.group()`，子命令含 `serve`（`--transport stdio/sse`）、`test`；loguru 接管 stdlib logging | 同，新增 `edgar-mcp` entry point，`serve` 增加 `http` 选项 |
| **日志** | `loguru`，写 `stderr`，格式 `{time:HH:mm:ss} | {level} | {message}` | 同 |
| **缓存** | `_cache.py` 自建 `DiskCache`（sha256 key，TTL，0o600 权限）；缓存目录默认 `~/.cache/edinet-mcp` | edgartools **自带磁盘缓存**（`EDGAR_LOCAL_DATA_DIR`），不再造 `DiskCache`；另加进程内「accession→解析对象」轻缓存（铁律2/Step4.2） |
| **限流** | `_rate_limiter.py` 自建 token-bucket（默认 0.5 rps） | edgartools **自带** rate-limit（`pyrate-limiter` + `httpxthrottlecache`），不再自造；只在 `watch_filings` 文档里提醒别短间隔狂调 |
| **依赖管理** | `uv` + `uv.lock`，`pyproject.toml`（hatchling 构建） | 同；新增 `edgartools` 依赖 + `uv lock` |
| **错误处理** | 部分 tool 直接 `raise ValueError`（edinet 风格） | **edgar 按 spec 铁律：错误即数据**，所有 tool 返回 `{"error":true,...}` 不抛异常 |
| **测试** | `tests/`，pytest + `asyncio_mode=auto`；tool 测试用 `.fn` 拿到原函数 + mock client | 同，新增 `tests/test_edgar_*.py`，全 mock，不打网络 |
| **CI** | `.github/workflows/ci.yml`：`ruff check` / `ruff format --check` / **`mypy src/`** / `pytest --cov=edinet_mcp`，py3.10-3.13 | 需让 `edgar_mcp` 通过 strict mypy + ruff；CI sync 加 `--extra edgar` |
| **transport / 部署** | stdio（`uvx edinet-mcp serve`）；`examples/claude_desktop_config.json` 注册；**无反代/systemd（不在本仓库）** | 同 stdio 为主；额外暴露 `--transport http`（streamable-http）供反代；部署到 mcp.ksinq.com 的反代/进程管理由 Jason 在部署机完成 |

### 依赖体系结论

`uv` + `uv.lock` + `pyproject.toml`（hatchling）。`edgartools` 加为 **optional extra `edgar`**
而非 core dependency——见下方「与 spec 的偏差」。

---

## 2. 依赖落地决策

- 新增：`[project.optional-dependencies] edgar = ["edgartools>=5.28"]`
  - 安装实测版本 **5.32.0**（`requires-python>=3.10`，与现有 3.10–3.13 矩阵兼容）。
  - 它自带 `lxml / pyarrow / pandas / httpx / beautifulsoup4 / rich / pyrate-limiter / httpxthrottlecache` 等。
- 新增 entry point：`edgar-mcp = "edgar_mcp.cli:cli"`。
- `uv lock` 重新锁定（PyPI 在本环境可达）。
- CI 的 `lint` 与 `test` job 改为 `uv sync --extra dev --extra edgar`（mypy 要 import edgar、edgar 测试要 edgartools）。
- mypy 加 override：`edgar.*` → `ignore_missing_imports=true`（edgartools 无 `py.typed`）。

---

## 3. API 核对（实测 edgartools 5.32.0，offline `hasattr`/`inspect`）

> **后续所有 tool 以这里核对的真实 API 为准，不以 spec 字面为准。** 打 ✅ = 实测存在；⚠️ = 与 spec 字面不同，已按实测调整。

### 顶层导出
| spec 写的 | 实测 | 备注 |
|---|---|---|
| `set_identity` | ✅ | |
| `Company` | ✅ | `Company(cik_or_ticker)`，接受 ticker 字符串或 int CIK |
| `get_filings` | ✅ | 签名 `(year, quarter, form, amendments, filing_date, index, priority_sorted_forms)` |
| `get_by_accession_number` | ✅ | `(accession_number, show_progress=False)` |
| `get_current_filings` | ✅ | `(form='', owner='include', page_size=40)` → `CurrentFilings`（有 `.filter`） |

### `Company`
- `.cik` ✅、`.name` ✅、`.display_name` ✅、`.tickers` ✅（注意是 **`tickers`** 复数，不是 `ticker`）、`.industry` ✅
- `.get_filings(*, year, quarter, form, accession_number, filing_date, date, amendments, is_xbrl, sort_by, ...)` ✅ → `EntityFilings`
- `.get_facts(period_type=None)` ✅ → `EntityFacts`

### `Filings` / `EntityFilings`
- `.filter(*, form, amendments, filing_date, date, cik, exchange, ticker, accession_number)` ✅
  - ⚠️ spec 写 `.filter(date=...)`；实测 date 范围用 `filing_date="YYYY-MM-DD:YYYY-MM-DD"` 或 `date=`，二者都收。
- `.latest(n=1)` ✅、`.head(n)` ✅、`.next()` ✅、`.__getitem__` ✅

### `Filing`（实例属性，class 上 `hasattr` 为 False 属正常——`__init__` 里赋值）
- `.cik / .company / .form / .filing_date / .accession_no` ✅（实例属性）
- `.accession_number` ✅（property，与 `accession_no` 同值）
- `.period_of_report` ✅、`.obj()` ✅、`.text()` ✅
- `.markdown(include_page_breaks=False, start_page_number=0)` ✅（⚠️ spec 写 `markdown(include_page_breaks=True)`，参数名一致，默认值不同）
- `.search("term")` ✅、`.exhibits` ✅、`.header` ✅
- URL 属性：`.url` ✅、`.homepage_url` ✅、`.filing_url` ✅、`.text_url` ✅
  - **`source_url` 取 `homepage_url`**（SEC 的 filing index 人类可读页），缺失回退 `filing_url`/`url`。

### `8-K` → `filing.obj()` 是 `EightK`
- `.items` ✅、`.financials` ✅、`.income_statement / .balance_sheet / .cash_flow_statement` ✅

### `10-K/10-Q` → `TenK`/`TenQ`
- `.financials` ✅、`.income_statement / .balance_sheet / .cash_flow_statement` ✅
  - ⚠️ spec 写 `tenk.financials.income_statement`；实测 `TenK` **直接**有 `.income_statement` 等便捷属性，
    `.financials` 是 `Financials` 对象，其上有 `.income_statement() / .balance_sheet() / .cashflow_statement()` 等。两条路都可用。

### `13F-HR` → `ThirteenF`
- `.holdings` ✅、`.infotable` ✅、`.compare_holdings()` ✅、`.holding_history(...)` ✅
- 另有 `.total_holdings` ✅、`.total_value` ✅、`.report_period` ✅、`.previous_holding_report` ✅、`.manager_name` ✅

### `Form 4` → `edgar.ownership.ownershipforms.Form4`
- `.to_dataframe()` ✅、`.to_html()` ✅、`.insider_name` ✅、`.market_trades` ✅
  - ⚠️ 无 `.get_transactions/.shares/.reporting_owners/.issuer` 这些 class 级属性；以 `to_dataframe()` 为主，
    辅以 `market_trades` / `common_stock_purchases` / `common_stock_sales` / `get_ownership_summary`。

### XBRL facts（⚠️ 与 spec 出入最大）
- spec 写 `Company(x).get_facts().to_pandas("us-gaap:Revenues")` — **实测无 `to_pandas(concept)` 这种用法**。
- 实测 `EntityFacts` 提供：
  - **`.time_series(concept, periods=20) -> pandas.DataFrame`** ✅ —— **`get_xbrl_concept` 用这个**。
    - 列：`[period_start, period_end, duration_days, numeric_value, fiscal_period, fiscal_year]`。
    - 内部 `by_concept(concept, exact=(":" in concept))`：传 `"us-gaap:Revenues"` 走精确 tag 匹配，传 `"Revenues"` 走同义词匹配。**spec 的 `us-gaap:Revenues` 形式可用。**
  - `.get_concept(concept_name, period=None, unit=None)` ✅（单值）、`.query()` ✅（FactQuery builder）、`.to_dataframe()` ✅

---

## 4. 与 spec 的偏差清单（已知问题 / 取舍）

1. **不是多 server 栈**：本仓库是单包 `edinet-mcp`。`edgar` 作为平行包 `edgar_mcp` 加入，而非独立部署目录。反代/systemd/gateway 注册不在本仓库范围（Step 5 由 Jason 在部署机做）。
2. **edgartools 设为 optional extra（`edinet-mcp[edgar]`）而非 core dependency**：避免给只用 EDINET 的用户强行装上 pandas/pyarrow/lxml 全家桶。spec 的「core dep」假设是 Jason 栈里 edgar 是一等公民；在这个发布包语境里，extra 更负责任。`edgar-mcp serve` 要求 `pip install "edinet-mcp[edgar]"`。
3. **XBRL concept API 改用 `EntityFacts.time_series()`**，不是 spec 的 `get_facts().to_pandas(concept)`（后者不存在）。
4. **`Company.tickers` 是复数列表**，`lookup_cik` 取第一个作为主 ticker。
5. **错误即数据**：edgar 所有 tool 返回 `{"error":true,...}`，与 edinet（部分 raise）风格不同——遵循 edgar spec 铁律。
6. **限流/缓存复用 edgartools 内置**，不复制 edinet 的 `_rate_limiter.py`/`DiskCache`；另加进程内 accession 解析缓存。
7. **edgartools 是同步库**：在 async tool 里用 `asyncio.to_thread(...)` 包裹，避免阻塞事件循环。

---

## 5. edgartools 自带 MCP server（交叉参考，Step「提醒1」）

edgartools 自带一个官方 MCP（`edgar.ai` / `to_agent_tools()` 等），暴露的能力与本封装高度重叠
（公司/filing 检索、财报、facts）。本次仍按 edinet 风格自建封装以统一路由/部署/风格、完全可控。
若 Jason 想省事，可直接跑其自带 MCP 作为替代。

---

## 6. 结论（一句话）

> `edgar` server 将完全复用 **FastMCP（fastmcp 2.x）框架** + **`src/<pkg>_mcp/` 单包目录结构 +
> click/loguru/pydantic-settings 风格** + **uv/pyproject/hatchling + stdio(`uvx`)/可选 http transport 的部署方式**，
> 以平行包 `src/edgar_mcp/` 落地，封装 `edgartools` 暴露 SEC EDGAR 一级源。
</content>
</invoke>
