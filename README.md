# tp12 — 币安现货止盈脚本（v0.3）

每 30 秒检查现货持仓；某币种相对平均成本涨幅 ≥ 12%（可配）时，
经币安闪兑 (Convert) 把可用余额全部换成 USDC。

## 为什么走闪兑

现货交易对有 MIN_NOTIONAL（约 5 USDT 等值）门槛，小额定投的尾巴仓位
可能下不了单；闪兑最低兑换额极低（通常 0.01 USDT 等值起），一口价成交。

## 快速开始

```bash
export BINANCE_API_KEY="你的key"
export BINANCE_API_SECRET="你的secret"

# 第一步：dry-run 观察（默认 DRY_RUN=1，不下单）
python3 tp12.py --once

# 第二步：dry-run 常驻观察几轮
python3 tp12.py

# 第三步：确认行为符合预期后，开真实交易
DRY_RUN=0 python3 tp12.py
```

建议用 systemd / tmux / nohup 常驻：
`nohup python3 tp12.py > tp12.log 2>&1 &`

## 成本价口径

1. `~/.config/binance-tp12/cost_basis.json` 手动覆盖（优先）：
   ```json
   {"BTC": 95000.0, "ETH": 3200.0}
   ```
   适用于：币是从闪兑/空投/站外转入、成交历史不全的情况。
2. 否则按现货成交历史（myTrades，USDT/USDC/FDUSD 计价对）用平均成本法推算。
3. 两者都没有 → 跳过该币种并告警，绝不瞎猜成本下单。
4. 若成交历史推算的持仓量 < 实际持仓 90%（说明有相当一部分币
   不是现货买来的），同样跳过并告警。

USDT/USDC/FDUSD 按 1:1 近似（相对 12% 止盈线可忽略）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| BINANCE_API_KEY / BINANCE_API_SECRET | 必填 | 只从环境变量读，不打日志 |
| TP_PCT | 0.12 | 止盈线 |
| POLL_SEC | 30 | 轮询间隔（秒），(0, 86400] |
| DRY_RUN | 1 | 1=只记录不下单；0=真实交易 |
| QUOTE_ASSET | USDC | 兑换目标币种 |
| EXCLUDE_ASSETS | USDT,USDC,FDUSD,DAI,TUSD,USDP,AEUR | 不监控的币种 |
| MIN_VALUE_USD | 1.0 | 估值低于此跳过（灰尘） |
| COOLDOWN_SEC | 3600 | 卖出成功后该币种冷却 |
| STATE_DIR | ~/.config/binance-tp12 | 状态目录（卖出记录 state.json） |
| LOG_FILE | 空 | 可选，日志同时写文件 |

## 安全提醒

- API Key 只需开「现货交易」权限；**不要开提现权限**。
- 先 dry-run 跑至少几轮，确认触发逻辑符合预期再开真实交易。
- 本金小、实验性质：脚本按"能用、好读"写，未做高可用/多机/告警。
- 密钥不要写进脚本、不要提交到 git。

## 当前状态

- v0.1：初稿 → 交叉审查 r1（3 轮）发现 11 个确认问题
- v0.2：按 r1 结论逐条修复，19 个 mock 单元测试全过 →
  交叉审查 r2（3 轮）发现 12 个确认问题，verdict「仍在发散」
- v0.4（当前）：按"先做状态机设计"实施，46 个 mock 单元测试全过
  （含全链路 check_once 测试）
  - 设计文档：`DESIGN_v04.md`
  - 订单状态机：询价后先落盘 quoteId 再 accept；`acceptQuote` 响应丢失/
    `orderStatus` 查询异常统一收敛为 `SellHalted`（必带 quoteId），资产冻结，
    下轮凭 quoteId（官方支持 quoteId 查 orderStatus）或 orderId 恢复；
    分块进度持久化，恢复后继续卖剩余；未知订单状态连续 10 次转人工冻结
  - 成本账本：现货 myTrades + Convert 卖出（脚本自有记录精确合并 +
    tradeFlow 近 90 天 best-effort 合并）按时间合并回放，平均成本法；
    未知成本隔离替代 0.9 容差（未知部分价值超 MIN_VALUE_USD 则整单跳过）
  - 附带修复：log 初始化移到环境变量解析之前（v0.3 的 NameError）；
    恢复查询异常只冻结该资产不中止整轮；恢复独立于余额
- v0.3：22 个 mock 测试全过 → r3 verdict 3.5/10 仍在发散（见上）；
  r3 的 9 个问题是 v0.4 的输入，见 `cross-review/runs/r3/FINAL.md`

### v0.4 状态机要点（给部署者）

- `state.json` 的 `pending` 结构变了（v0.3 的纯 orderId 字符串会自动升级，
  无需手工处理）。
- `needs_manual` 的资产会被永久冻结，每轮打 CRITICAL 日志；人工核对币安
  账单后，删除 `state.json` 里对应 `pending.<ASSET>` 条目即解冻。
- 90 天前的手工闪兑不在 tradeFlow 合并窗口内：做过的话用
  `cost_basis.json` 手动覆盖成本。
- 本轮没有开 r4：v0.4 是否达到可 dry-run 标准，由你定。

### v0.3 修复清单（对应 r2 的 12 个问题 + 1 个分歧裁决）

1. myTrades 分页触及 10 万条硬上限 → 标记 truncated，该币种成本标为
   不可用并跳过（不再用截断成本误判止盈）
2. `cost_basis.json` 显式拒绝布尔值（`float(True)==1.0` 会绕过校验）
3. 发现非美元稳定币计价对有成交 → 该币种成本直接标不可用并跳过，
   不再用部分成本（可用 cost_basis.json 手动覆盖）
4. 估值价格查询覆盖 FDUSD（USDC → USDT → FDUSD 顺序）
5. 环境变量已设置但解析失败（如 `TP_PCT=abc`）→ 直接拒绝启动，
   不再静默回退默认值
6. 超过闪兑单次上限的余额按上限自动分块兑换；剩余低于最小额的灰尘
   停止拆分、留待下轮或人工
7. 兑换结果未知时按订单号持久化「待确认订单」，下轮先恢复查询；
   状态保存失败时用进程内冷却兜底；损坏的 state.json 备份后重置
8. 启动时校验 `QUOTE_ASSET` 有闪兑路径，否则拒绝启动
9. `exchangeInfo` 不再过滤交易对状态，已下架交易对的历史成交也能查到
10. 资产不在交易对缓存中时刷新一次 `exchangeInfo`（防新上线漏检）
11. `POLL_SEC` 上限 86400 秒（防 `time.sleep` OverflowError）
12. 分歧裁决（数量精度）：防御性向下量化到 8 位小数——接口若限精度
    则修好，若不限制则无害；残余不确定性已注明

### v0.2 修复清单（对应 r1 的 11 个问题）

1. 跨计价对成交按全局时间合并排序（USDT/USDC/FDUSD 按 1:1 折算，
   其他计价对明确告警并排除）
2. myTrades 从 `fromId=1` 向前翻页拿全历史（官方默认只返回最近成交）
3. `cost_basis.json` 非法值（0/负数/NaN/非对象）告警跳过，不中断整轮
4. `state.json` 损坏时备份并重置为空状态，不中断整轮
5. 启动校验 `TP_PCT/POLL_SEC/MIN_VALUE_USD/COOLDOWN_SEC`，非法拒绝启动
6. Convert 终态按官方枚举处理：`SUCCESS` 成功 / `FAIL` 失败 /
   `PROCESS,ACCEPT_SUCCESS` 进行中（之前误写 `"FAILED"`）
7. 金额全程用 `Decimal`，发送定点字符串（杜绝 `1e-08` 这类科学计数法）
8. `free<=0` 直接跳过，不发起零数量询价
9. 遇到 `-1021` 时间戳错误自动重同步时钟并重试一次
10. 启动时拉 `exchangeInfo` 发现该币种全部现货交易对
11. Ctrl+C 干净退出
