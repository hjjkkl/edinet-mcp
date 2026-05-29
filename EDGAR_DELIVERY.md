# EDGAR MCP server — 交付说明（给 Jason）

> 配套阅读：`_discovery.md`（Step 0 勘探 + API 核对 + 偏差清单）。

## 一句话部署说明

`edgar` server 以**平行包 `src/edgar_mcp/`** 加入本仓库，完全复用 `edinet_mcp` 的
FastMCP / click / loguru / pydantic-settings 风格，封装 `edgartools`。安装
`pip install "edinet-mcp[edgar]"`，设置 `EDGAR_IDENTITY`，即可
`edgar-mcp serve`（stdio，Claude Desktop）或
`edgar-mcp serve --transport http --port 8000`（Streamable HTTP，给 mcp.ksinq.com 反代）。

## 交付物对照

| Spec 要求 | 产出 |
|---|---|
| 1. `_discovery.md`（勘探 + API 核对） | ✅ `_discovery.md` |
| 2. `edgar` server 源码 | ✅ `src/edgar_mcp/`（`_config.py` / `client.py` / `server.py` / `cli.py` / `__main__.py` / `__init__.py` / `py.typed`） |
| 3. `scripts/verify_api.py` + `scripts/smoke_test.py` + 运行结果 | ✅ 见下「自测结果」 |
| 4. 一句话部署说明 + 配置改动 diff | ✅ 见上 + 下方「配置改动」 |
| 5. 已知问题/偏差清单 | ✅ `_discovery.md` 第 4 节 + 下方 |

## 13 个 tool（全部注册成功，`tool list` 实测 13 个）

A 组（基本面）：`lookup_cik` `search_filings` `get_filing_text` `extract_8k_items`
`get_financials` `get_xbrl_concept`
B 组（投研）：`get_13f_holdings` `compare_13f_holdings` `get_13f_holding_history`
`get_insider_transactions` `get_ownership_filings`
C 组（监控）：`get_current_filings` `watch_filings`

## 横切要求落实（Step 4）

1. **响应大小**：`get_filing_text` 默认按 `EDGAR_MAX_TEXT_CHARS`（20000）截断 + `offset` 分页，返回 `truncated`/`next_offset`；8-K item snippet 截 800 字。
2. **限流/缓存**：复用 edgartools 内置限流（pyrate-limiter + httpxthrottlecache）与磁盘缓存（`EDGAR_LOCAL_DATA_DIR`）；另加进程内 accession→Filing 缓存，避免会话内重复拉同一 filing。`watch_filings` 文档提示别短间隔狂调。
3. **只读**：无任何写操作。
4. **错误即数据**：所有 tool 经 `_safe` 包装，异常返回 `{"error":true,"reason":...,"hint":...}`，不抛。
5. **时间戳齐全**：每条记录带 `filing_date` + `source_url`（取 `homepage_url`）+ `accession`。
6. **结构化优先**：所有 tool 返回结构化 JSON（dict/list）。

## 自测结果（Step 6）

环境说明：CI/单测全绿；**但本沙箱出口被 SEC 的 WAF 拦截（对 `data.sec.gov` 返回 HTTP 403）**，
所以**需要联网打 SEC 的验收项无法在本沙箱实跑**。已用以下方式覆盖：

| 验收项 | 状态 | 说明 |
|---|---|---|
| `set_identity` 生效 / 缺失 fail-fast | ✅ 实测 | `edgar-mcp test` 无 `EDGAR_IDENTITY` 时退出码 1 并打印 SEC 身份提示；`EdgarClient(identity="")` 抛 `EdgarError`（单测覆盖） |
| 任一错误输入 → `{"error":true}` 不崩 | ✅ 实测 | 对 `lookup_cik("AAPL")` 的上游 403 实测返回 error dict，未崩 |
| `tool list` 看到 13 个 tool | ✅ 实测 | `mcp.get_tools()` 返回 13 个 |
| MCP handshake（Step 5 健康检查） | ✅ 实测 | `--transport http` 启动，`POST /mcp` 返回 `initialize` 结果，`serverInfo.name="EDGAR"` |
| API 核对（verify_api.py） | ✅ 实测 | edgartools 5.32.0 全部预期 API 存在 |
| lookup_cik/search_filings/get_filing_text/extract_8k_items/get_financials/get_xbrl_concept/13F×3/insider/ownership/current/watch | ⏳ 待 Jason 在可联网环境跑 `scripts/smoke_test.py` | 沙箱 403 无法实跑；脚本已写好并通过结构/错误路径验证 |

> **请 Jason 在能正常访问 sec.gov 的环境跑一遍**（Mac Mini 本机通常可以）：
> ```bash
> export EDGAR_IDENTITY="Jason <jason@ksinq.com>"
> uv run --extra edgar python scripts/verify_api.py
> uv run --extra edgar python scripts/smoke_test.py
> ```

## 配置改动（diff 摘要）

- `pyproject.toml`：新增 optional extra `edgar = ["edgartools>=5.28"]`；entry point `edgar-mcp`；wheel 打包加 `src/edgar_mcp`；mypy `packages` 加 `edgar_mcp` + `edgar.*/pandas.*/numpy.*` override。
- `.github/workflows/ci.yml`：lint/test job 的 `uv sync` 加 `--extra edgar`。
- `uv.lock`：锁入 edgartools 及其依赖。
- `examples/claude_desktop_config.json`：新增 `edgar` server 条目
  （`uvx --from edinet-mcp[edgar] edgar-mcp serve` + `EDGAR_IDENTITY`）。
- `.env.sample` / `README.md`：补 EDGAR 段。

## 已知问题 / 偏差（详见 `_discovery.md` §4）

1. 本仓库是**单包 `edinet-mcp`**，不是 spec 假设的 `mcp.ksinq.com` 多 server 栈；`tdnet`、反代、systemd 都不在此仓库。`edgar` 以平行包加入。
2. **edgartools 设为 optional extra** 而非 core dep（避免给纯 EDINET 用户强装 pandas/pyarrow 全家桶）。`edgar-mcp` 需 `edinet-mcp[edgar]`。
3. **XBRL 用 `EntityFacts.time_series()`**，非 spec 的 `get_facts().to_pandas(concept)`（后者不存在）。
4. `Company.tickers` 是复数列表，`lookup_cik` 取首个为主 ticker。
5. `get_ownership_filings` 的 `pct_owned` 暂为 `None`（SC 13D/G 持股比例不在 metadata 中、难稳定机读）。
6. **沙箱无法联网打 SEC**，需 Jason 跑 live smoke_test 确认（见上）。

## 反代接入（Step 5，需 Jason 在部署机做，不在本仓库）

启动 `edgar-mcp serve --transport http --host 127.0.0.1 --port 8000`，
然后在 nginx/caddy 把 `https://mcp.ksinq.com/servers/edgar/mcp` 反代到
`http://127.0.0.1:8000/mcp`（注意 FastMCP 端点带 `/mcp`，无尾斜杠会 307 跳到 `/mcp`）。
进程管理沿用现有 tdnet/edinet 的方式（systemd/pm2/compose——本仓库看不到，按你部署机约定）。

## 范围边界

✅ 本次只做 MCP server 并跑通可离线验证项。
⛔ **skill（stock-analysis 的 US 路由）接入待 Jason 确认后单独进行**。
