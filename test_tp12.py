#!/usr/bin/env python3
"""tp12.py v0.4 的 mock 单元测试：不碰真实网络。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
from decimal import Decimal

sys.path.insert(0, "/home/hatch/workspace/binance-tp12")
import tp12

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def T(i, t, qty, price, buyer, sym="BTCUSDT"):
    return {"id": i, "time": t, "symbol": sym, "qty": str(qty),
            "quoteQty": str(Decimal(str(qty)) * Decimal(str(price))),
            "price": str(price), "isBuyer": buyer}


def reset_state_dir():
    d = tempfile.mkdtemp()
    tp12.STATE_DIR = d
    return d


# --- 1. 分页：从 fromId=1 向前翻，拿全历史 + 截断标记 ---
calls = []


def mock_paged(method, path, params=None, signed=False, _retried=False):
    if path == "/api/v3/myTrades":
        fid = params.get("fromId", 1)
        calls.append(fid)
        all_t = [T(100 + i, i, "0.01", 200, True) for i in range(1500)]
        return [t for t in all_t if t["id"] >= fid][:1000]
    if path == "/api/v3/exchangeInfo":
        return {"symbols": []}
    raise AssertionError(path)


tp12._request = mock_paged
tp12._exchange_symbols = None
trades, trunc = tp12.get_my_trades("BTCUSDT")
check("分页拿全 1500 条", len(trades) == 1500)
check("分页从 fromId=1 开始", calls[0] == 1)
check("未截断", trunc is False)


def mock_huge(method, path, params=None, signed=False, _retried=False):
    if path == "/api/v3/myTrades":
        fid = params.get("fromId", 1)
        if fid > 200000:
            return []
        return [T(fid + i, fid + i, "0.01", 200, True) for i in range(1000)]
    raise AssertionError(path)


tp12._request = mock_huge
trades, trunc = tp12.get_my_trades("X")
check("超上限标记 truncated", trunc is True and len(trades) == 100000)

# --- 2. 跨计价对按全局时间合并 ---
def mock_cross(method, path, params=None, signed=False, _retried=False):
    if path == "/api/v3/myTrades":
        s = params["symbol"]
        if s == "BTCUSDC":
            return [T(1, 1, 1, 100, True, s), T(2, 2, 1, 100, False, s)]
        if s == "BTCUSDT":
            return [T(3, 3, 1, 200, True, s)]
        return []
    if path == "/api/v3/exchangeInfo":
        return {"symbols": [
            {"symbol": "BTCUSDC", "status": "TRADING",
             "isSpotTradingAllowed": True, "baseAsset": "BTC",
             "quoteAsset": "USDC"},
            {"symbol": "BTCUSDT", "status": "TRADING",
             "isSpotTradingAllowed": True, "baseAsset": "BTC",
             "quoteAsset": "USDT"}]}
    raise AssertionError(path)


tp12._request = mock_cross
tp12._exchange_symbols = None
tp12._symbols_refreshed = False
avg, qty = tp12.avg_cost_from_trades("BTC")
check("跨计价对成本=200", avg == Decimal("200"))
check("跨计价对持仓=1", qty == Decimal("1"))

# --- 3. 非美元计价对有成交 -> 成本不可用 ---
def mock_nostable(method, path, params=None, signed=False, _retried=False):
    if path == "/api/v3/myTrades":
        s = params["symbol"]
        if s == "ETHUSDT":
            return [T(1, 1, 10, 100, True, s)]
        if s == "ETHBTC":
            return [T(2, 2, 1, 0.01, True, s)]
        return []
    if path == "/api/v3/exchangeInfo":
        return {"symbols": [
            {"symbol": "ETHUSDT", "status": "TRADING",
             "isSpotTradingAllowed": True, "baseAsset": "ETH",
             "quoteAsset": "USDT"},
            {"symbol": "ETHBTC", "status": "TRADING",
             "isSpotTradingAllowed": True, "baseAsset": "ETH",
             "quoteAsset": "BTC"}]}
    raise AssertionError(path)


tp12._request = mock_nostable
tp12._exchange_symbols = None
tp12._symbols_refreshed = False
avg, qty = tp12.avg_cost_from_trades("ETH")
check("非美元计价有成交则成本不可用", avg is None)

# --- 4. cost_basis.json：拒绝 bool ---
d = reset_state_dir()
with open(os.path.join(d, "cost_basis.json"), "w") as f:
    json.dump({"BTC": True, "ETH": 3000}, f)
ov = tp12.load_cost_overrides()
check("bool 成本被拒绝", ov == {"ETH": 3000.0})

# --- 5. 严格环境变量解析 ---
os.environ["TP12_TEST_F"] = "abc"
try:
    tp12._parse_float_strict("TP12_TEST_F", 0.12)
    check("非法浮点拒绝启动", False)
except SystemExit:
    check("非法浮点拒绝启动", True)
del os.environ["TP12_TEST_F"]

# --- 5b. 非法环境变量：进程干净退出码 2，不抛 NameError（v0.3 回归） ---
r = subprocess.run([sys.executable, "-c", "import tp12"],
                   cwd="/home/hatch/workspace/binance-tp12",
                   env={**os.environ, "TP_PCT": "abc"},
                   capture_output=True, text=True, timeout=30)
check("非法 TP_PCT 退出码为 2", r.returncode == 2)
check("无 NameError", "NameError" not in r.stderr)

# --- 6. POLL_SEC 上限 ---
old = tp12.POLL_SEC
tp12.POLL_SEC = 10 ** 12
try:
    tp12.validate_config()
    check("过大 POLL_SEC 拒绝", False)
except SystemExit:
    check("过大 POLL_SEC 拒绝", True)
tp12.POLL_SEC = old

# --- 7. FDUSD 估值 ---
check("FDUSD 计价可估值",
      tp12.price_of("ABC", {"ABCFDUSD": 1.5}) == (1.5, "ABCFDUSD"))
check("优先 USDC",
      tp12.price_of("ABC", {"ABCUSDT": 1.0, "ABCUSDC": 2.0}) == (2.0, "ABCUSDC"))

# --- 8. quantize_down ---
check("向下量化 8 位",
      tp12.quantize_down(Decimal("0.123456789")) == Decimal("0.12345678"))
check("fmt 无科学计数法",
      tp12.fmt_amount(Decimal("1E-8")) == "0.00000001")

# --- 9. 状态机分块卖出（超过单次上限） ---
def mock_chunk(method, path, params=None, signed=False, _retried=False):
    if path == "/sapi/v1/convert/exchangeInfo":
        return [{"fromAsset": "ABC", "toAsset": "USDC",
                 "fromAssetMinAmount": "1", "fromAssetMaxAmount": "10"}]
    if path == "/sapi/v1/convert/getQuote":
        return {"quoteId": "q" + str(params["fromAmount"]),
                "toAmount": "100", "ratio": "10"}
    if path == "/sapi/v1/convert/acceptQuote":
        return {"orderId": "o" + str(params["quoteId"]), "createTime": 1}
    if path == "/sapi/v1/convert/orderStatus":
        return {"orderId": "1", "orderStatus": "SUCCESS", "toAmount": "100"}
    raise AssertionError(path)


tp12._request = mock_chunk
tp12._convert_limits_cache = {}
d = reset_state_dir()
state = tp12._empty_state()
rec = tp12._new_sell_op("ABC", "USDC", Decimal("25"), time.time())
outcome = tp12._sell_chunks("ABC", rec, state)
check("25 按上限 10 拆 3 块卖完", outcome == "sold")
check("3 块订单都记录", len(rec["chunksDone"]) == 3)
result = tp12._settle("ABC", rec, state, time.time(), outcome)
check("卖完后 pending 清理", result == "sold" and "ABC" not in state["pending"])

# --- 10. 结果未知恢复：acceptQuote 响应丢失 -> 凭 quoteId 找回 ---
d = reset_state_dir()
q_calls = []


def mock_halt_then_recover(method, path, params=None, signed=False,
                           _retried=False):
    if path == "/sapi/v1/convert/exchangeInfo":
        return [{"fromAsset": "BTC", "toAsset": "USDC",
                 "fromAssetMinAmount": "0.0001", "fromAssetMaxAmount": "100"}]
    if path == "/sapi/v1/convert/getQuote":
        return {"quoteId": "q-lost", "toAmount": "50000", "ratio": "50000"}
    if path == "/sapi/v1/convert/acceptQuote":
        raise urllib.error.URLError("connection reset")  # 响应丢失
    if path == "/sapi/v1/convert/orderStatus":
        q_calls.append(dict(params))
        if "quoteId" in params:
            return {"orderId": "ord999", "orderStatus": "PROCESS"}
        return {"orderId": "ord999", "orderStatus": "SUCCESS",
                "toAmount": "50000"}
    raise AssertionError(path)


tp12._request = mock_halt_then_recover
tp12._convert_limits_cache = {}
state = tp12._empty_state()
rec = tp12._new_sell_op("BTC", "USDC", Decimal("1"), time.time())
outcome = tp12._sell_chunks("BTC", rec, state)
check("accept 响应丢失 -> pending 冻结", outcome == "pending")
cur = state["pending"]["BTC"]["current"]
check("pending 为 result_unknown 且有 quoteId 无 orderId",
      cur["state"] == "result_unknown" and cur["quoteId"] == "q-lost"
      and cur["orderId"] is None)
outcomes = tp12.recover_pending(state, time.time())
check("凭 quoteId 找回订单并成功卖出", outcomes.get("BTC") == "sold")
check("恢复时用 quoteId 查过 orderStatus",
      any("quoteId" in c for c in q_calls))
check("卖出已记录且设冷却",
      len(state["sells"]) == 1
      and state["sells"][0]["order_ids"] == ["ord999"]
      and "BTC" not in state["pending"]
      and state["cooldown"]["BTC"] > time.time())

# --- 11. state.json 损坏备份 ---
with open(os.path.join(d, "state.json"), "w") as f:
    f.write("{bad json")
st = tp12.load_state()
check("损坏重置为空",
      st == {"sells": [], "cooldown": {}, "pending": {}})
check("损坏文件被备份", os.path.exists(os.path.join(d, "state.json.corrupt")))

# --- 12. 分块中途结果未知：已成功块持久化，恢复后只卖剩余 ---
d = reset_state_dir()


def mock_chunk2():
    n = [0]

    def _mock(method, path, params=None, signed=False, _retried=False):
        if path == "/sapi/v1/convert/exchangeInfo":
            return [{"fromAsset": "ABC", "toAsset": "USDC",
                     "fromAssetMinAmount": "1", "fromAssetMaxAmount": "10"}]
        if path == "/sapi/v1/convert/getQuote":
            n[0] += 1
            return {"quoteId": f"q{n[0]}", "toAmount": "100", "ratio": "10"}
        if path == "/sapi/v1/convert/acceptQuote":
            if params["quoteId"] == "q2":
                raise urllib.error.URLError("lost")  # 第二块响应丢失
            return {"orderId": "o" + params["quoteId"], "createTime": 1}
        if path == "/sapi/v1/convert/orderStatus":
            if "quoteId" in params:
                return {"orderId": "o2x", "orderStatus": "PROCESS"}
            return {"orderId": params["orderId"], "orderStatus": "SUCCESS",
                    "toAmount": "100"}
        raise AssertionError(path)
    return _mock


tp12._request = mock_chunk2()
tp12._convert_limits_cache = {}
state = tp12._empty_state()
rec = tp12._new_sell_op("ABC", "USDC", Decimal("25"), time.time())
outcome = tp12._sell_chunks("ABC", rec, state)
check("第二块丢失 -> pending", outcome == "pending")
check("第一块已持久化", len(state["pending"]["ABC"]["chunksDone"]) == 1)
check("剩余 15 待卖", state["pending"]["ABC"]["remaining"] == "15")
outcomes = tp12.recover_pending(state, time.time())
check("恢复后卖完剩余", outcomes.get("ABC") == "sold")
check("一次卖出记 3 个订单", len(state["sells"]) == 1
      and len(state["sells"][0]["order_ids"]) == 3)
check("卖出总量 25", state["sells"][0]["qty"] == 25.0)

# --- 13. 恢复查询网络异常：该资产冻结，其他资产继续 ---
d = reset_state_dir()


def mock_netfail(method, path, params=None, signed=False, _retried=False):
    if path == "/sapi/v1/convert/exchangeInfo":
        return [{"fromAsset": "AAA", "toAsset": "USDC",
                 "fromAssetMinAmount": "0.0001", "fromAssetMaxAmount": "100"}]
    if path == "/sapi/v1/convert/orderStatus":
        if params.get("orderId") == "bad":
            raise urllib.error.URLError("timeout")
        return {"orderId": params["orderId"], "orderStatus": "SUCCESS",
                "toAmount": "1"}
    raise AssertionError(path)


tp12._request = mock_netfail
state = tp12._empty_state()
now = time.time()
for a, oid in (("AAA", "bad"), ("BBB", "good")):
    rec = tp12._new_sell_op(a, "USDC", Decimal("1"), now)
    rec["current"] = {"quoteId": "q", "orderId": oid,
                      "fromAmount": "1", "state": "processing"}
    rec["remaining"] = "1"
    state["pending"][a] = rec
outcomes = tp12.recover_pending(state, now)
check("网络异常资产保持冻结",
      outcomes.get("AAA") == "blocked" and "AAA" in state["pending"])
check("其他资产照常恢复卖出",
      outcomes.get("BBB") == "sold" and "BBB" not in state["pending"])

# --- 14. 成本账本：Convert 卖出按时间扣减（r3 反例） ---
def mock_ledger(method, path, params=None, signed=False, _retried=False):
    if path == "/api/v3/myTrades":
        s = params["symbol"]
        if s == "BTCUSDT":
            return [T(1, 1791763200000, 1, 50000, True, s),
                    T(3, 1791763203000, 0.1, 100000, True, s)]
        return []
    if path == "/api/v3/exchangeInfo":
        return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING",
                             "isSpotTradingAllowed": True, "baseAsset": "BTC",
                             "quoteAsset": "USDT"}]}
    raise AssertionError(path)


tp12._request = mock_ledger
tp12._exchange_symbols = None
tp12._symbols_refreshed = False
d = reset_state_dir()
state = tp12._empty_state()
state["sells"].append({"asset": "BTC", "qty": 1.0, "proceeds": 60000.0,
                       "proceeds_asset": "USDC", "order_ids": ["o1"],
                       "time": "2026-10-12T00:00:02+00:00"})
avg, qty, uv, reason = tp12.position_state(
    "BTC", Decimal("0.1"), 100000, state, {}, [])
check("账本扣减 convert 卖出后成本=100000", avg == Decimal("100000"))
check("账本扣减后数量=0.1", qty == Decimal("0.1"))

# --- 15. 未知成本隔离：转入部分超阈值则整单跳过 ---
def mock_q(method, path, params=None, signed=False, _retried=False):
    if path == "/api/v3/myTrades":
        s = params["symbol"]
        if s == "ETHUSDT":
            return [T(1, 1, 1, 100, True, s)]
        return []
    if path == "/api/v3/exchangeInfo":
        return {"symbols": [{"symbol": "ETHUSDT", "status": "TRADING",
                             "isSpotTradingAllowed": True, "baseAsset": "ETH",
                             "quoteAsset": "USDT"}]}
    raise AssertionError(path)


tp12._request = mock_q
tp12._exchange_symbols = None
tp12._symbols_refreshed = False
state = tp12._empty_state()
avg, qty, uv, reason = tp12.position_state(
    "ETH", Decimal("1.1"), 112, state, {}, [])
check("未知成本 11.2U 超阈值则成本不可用",
      avg is None and reason == "未知成本部分超阈值")
avg2, qty2, uv2, _ = tp12.position_state(
    "ETH", Decimal("1.0001"), 112, state, {}, [])
check("灰尘未知部分被容忍", avg2 == Decimal("100"))

# --- 16. 未知订单状态连续超限 -> 转人工 ---
d = reset_state_dir()


def mock_unknown(method, path, params=None, signed=False, _retried=False):
    if path == "/sapi/v1/convert/orderStatus":
        return {"orderId": "x", "orderStatus": "WEIRD"}
    raise AssertionError(path)


tp12._request = mock_unknown
state = tp12._empty_state()
rec = tp12._new_sell_op("BTC", "USDC", Decimal("1"), time.time())
rec["current"] = {"quoteId": "q", "orderId": "x",
                  "fromAmount": "1", "state": "processing"}
rec["unknownStreak"] = 9
state["pending"]["BTC"] = rec
outcomes = tp12.recover_pending(state, time.time())
check("未知状态第 10 次转人工",
      outcomes.get("BTC") == "manual"
      and state["pending"]["BTC"].get("needs_manual") is True)

# --- 17. quoteId 连续查无且超报价有效期 -> 判定未执行解冻 ---
def mock_notfound(method, path, params=None, signed=False, _retried=False):
    if path == "/sapi/v1/convert/orderStatus":
        raise tp12.BinanceAPIError(-1, "not found")
    raise AssertionError(path)


tp12._request = mock_notfound
state = tp12._empty_state()
now = time.time()
rec = tp12._new_sell_op("BTC", "USDC", Decimal("1"), now - 400)
rec["current"] = {"quoteId": "q", "orderId": None,
                  "fromAmount": "1", "state": "result_unknown"}
rec["notFoundStreak"] = 9
state["pending"]["BTC"] = rec
outcomes = tp12.recover_pending(state, now)
check("查无订单超限且超龄 -> 解冻",
      outcomes.get("BTC") == "unblocked" and "BTC" not in state["pending"])

# --- 18. v0.3 遗留字符串 pending 升级 ---
d = reset_state_dir()
with open(os.path.join(d, "state.json"), "w") as f:
    json.dump({"sells": [], "cooldown": {}, "pending": {"BTC": "ord123"}}, f)


def mock_leg(method, path, params=None, signed=False, _retried=False):
    if path == "/sapi/v1/convert/exchangeInfo":
        return [{"fromAsset": "BTC", "toAsset": "USDC",
                 "fromAssetMinAmount": "0.0001", "fromAssetMaxAmount": "100"}]
    if path == "/sapi/v1/convert/orderStatus":
        return {"orderId": "ord123", "orderStatus": "SUCCESS", "toAmount": "5"}
    raise AssertionError(path)


tp12._request = mock_leg
st = tp12.load_state()
check("遗留字符串 pending 被加载保留", st["pending"]["BTC"] == "ord123")
outcomes = tp12.recover_pending(st, time.time())
check("遗留 pending 升级并恢复成功", outcomes.get("BTC") == "sold")

# --- 19. check_once 全链路：触发 -> 状态机卖出 -> 记 sell + 冷却 ---
d = reset_state_dir()


def mock_full(method, path, params=None, signed=False, _retried=False):
    if path == "/api/v3/account":
        return {"balances": [{"asset": "BTC", "free": "1.0", "locked": "0"},
                             {"asset": "USDC", "free": "10", "locked": "0"}]}
    if path == "/api/v3/ticker/price":
        return [{"symbol": "BTCUSDC", "price": "56000"}]
    if path == "/api/v3/myTrades":
        s = params["symbol"]
        if s == "BTCUSDC":
            return [T(1, 1791763200000, 1, 50000, True, s)]
        return []
    if path == "/api/v3/exchangeInfo":
        return {"symbols": [{"symbol": "BTCUSDC", "status": "TRADING",
                             "isSpotTradingAllowed": True, "baseAsset": "BTC",
                             "quoteAsset": "USDC"}]}
    if path == "/sapi/v1/convert/exchangeInfo":
        return [{"fromAsset": "BTC", "toAsset": "USDC",
                 "fromAssetMinAmount": "0.0001", "fromAssetMaxAmount": "100"}]
    if path == "/sapi/v1/convert/getQuote":
        return {"quoteId": "q1", "toAmount": "55900", "ratio": "55900"}
    if path == "/sapi/v1/convert/acceptQuote":
        return {"orderId": "o1", "createTime": 1}
    if path == "/sapi/v1/convert/orderStatus":
        return {"orderId": "o1", "orderStatus": "SUCCESS", "toAmount": "55900"}
    if path == "/sapi/v1/convert/tradeFlow":
        return []
    raise AssertionError(path)


tp12._request = mock_full
tp12._exchange_symbols = None
tp12._symbols_refreshed = False
tp12._convert_limits_cache = {}
tp12.mem_cooldown.clear()  # 防止前面测试的进程内冷却污染
old_dry = tp12.DRY_RUN
tp12.DRY_RUN = False
trig = tp12.check_once()
tp12.DRY_RUN = old_dry
state = tp12.load_state()
check("全链路触发卖出", trig == ["BTC"])
check("sell 已记录", len(state["sells"]) == 1
      and state["sells"][0]["qty"] == 1.0
      and state["sells"][0]["order_ids"] == ["o1"])
check("冷却已设置", state["cooldown"].get("BTC", 0) > time.time())
check("无残留 pending", "BTC" not in state["pending"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)