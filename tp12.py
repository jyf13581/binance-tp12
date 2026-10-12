#!/usr/bin/env python3
"""
tp12.py — 币安现货止盈脚本（v0.4）

功能：每隔 POLL_SEC 秒检查现货账户持仓；当某币种相对平均成本价的
涨幅 >= TP_PCT（默认 12%）时，通过币安闪兑 (Convert) 把该币种的
可用余额全部卖出，换成 USDC。

成本账本（二选一，优先顺序）：
  1. cost_basis.json 手动覆盖：{"BTC": 95000.0, ...}（USDT/USDC 计价）
  2. 事件回放：现货成交历史 (myTrades) + Convert 卖出（脚本自有记录精确
     合并 + tradeFlow 近 90 天 best-effort 合并）按时间排序后用平均成本法
     回放；只有 USDT/USDC/FDUSD 计价按 1:1 折美元参与。
  未知成本隔离：账面解释不了的余额，若未知部分价值超过 MIN_VALUE_USD，
  整单跳过（覆盖转入/空投/已下架对缺口），可用 cost_basis.json 手动覆盖。
  成本不可用 -> 跳过该币种并告警，绝不瞎猜成本价下单。

为什么走闪兑而不是现货市价单：
  现货交易对有 MIN_NOTIONAL（约 5 USDT 等值）门槛，小额定投的
  尾巴仓位可能下不了单；闪兑最低兑换额极低（通常 0.01 USDT 等值
  起），且是一口价成交、无传统手续费（成本含在报价价差里）。

安全设计：
  - 默认 DRY_RUN=1：只记录"会做什么"，不发任何交易请求。
    确认行为符合预期后，设 DRY_RUN=0 才真正交易。
  - API Key/Secret 只从环境变量读取，绝不打印到日志。
  - 启动时严格校验配置：非法值直接拒绝启动（不静默回退）。
  - 主循环内任何异常只记日志，不中断循环（30 秒后重试）。
  - 卖出走订单状态机：询价后先落盘 quoteId 再 accept；结果未知时冻结
    资产，下轮凭 quoteId/orderId 恢复（orderStatus 支持 quoteId 查询），
    绝不直接重试。分块卖出的进度持久化，恢复后继续卖剩余。
  - 卖出成功后该币种进入冷却。

依赖：仅 Python 标准库。Python 3.8+。

环境变量：
  BINANCE_API_KEY / BINANCE_API_SECRET  必填
  TP_PCT          止盈线，默认 0.12（12%），必须为有限非负数
  POLL_SEC        轮询间隔秒，默认 30，必须在 (0, 86400] 内
  DRY_RUN         默认 1；设为 0 开启真实交易
  QUOTE_ASSET     兑换目标，默认 USDC（启动时校验闪兑路径存在）
  EXCLUDE_ASSETS  逗号分隔，不监控的币种，默认 USDT,USDC,FDUSD,DAI,TUSD,USDP,AEUR
  MIN_VALUE_USD   持仓估值低于此值跳过，默认 1.0，必须 >= 0
  COOLDOWN_SEC    卖出后冷却秒数，默认 3600，必须 >= 0
  STATE_DIR       状态目录，默认 ~/.config/binance-tp12
  LOG_FILE        可选，日志同时写文件

用法：
  DRY_RUN=1 python3 tp12.py            # 先 dry-run 观察一轮
  DRY_RUN=1 python3 tp12.py --once      # 只跑一轮就退出（测试用）
  BINANCE_API_KEY=... BINANCE_API_SECRET=... DRY_RUN=0 python3 tp12.py
"""

import hashlib
import hmac
import json
import logging
import math
import os
import sys
import time
import urllib.error  # 显式导入：_request 的 HTTPError 分支依赖它
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN

# 日志必须在环境变量解析之前初始化：解析失败要记日志并干净退出。
# （v0.3 曾把 log 建在解析之后，非法环境变量触发 NameError）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("tp12")

# ---------------------------------------------------------------- 配置

API_BASE = "https://api.binance.com"
# 视为 1:1 折美元的稳定币计价（成本合并用）
USD_STABLE_QUOTES = ("USDT", "USDC", "FDUSD")
# myTrades 单交易对分页硬上限（防死循环）；达到即标成本不可用
MAX_TRADE_PAGES = 100
MAX_POLL_SEC = 86400


def _parse_float_strict(name, default):
    """环境变量已设置但解析失败 -> 直接拒绝启动（不静默回退）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.error("环境变量 %s=%r 无法解析为数字，拒绝启动", name, raw)
        sys.exit(2)


def _parse_int_strict(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        log.error("环境变量 %s=%r 无法解析为整数，拒绝启动", name, raw)
        sys.exit(2)


API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
TP_PCT = _parse_float_strict("TP_PCT", 0.12)
POLL_SEC = _parse_int_strict("POLL_SEC", 30)
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
QUOTE_ASSET = os.environ.get("QUOTE_ASSET", "USDC").upper()
EXCLUDE_ASSETS = {a.strip().upper() for a in os.environ.get(
    "EXCLUDE_ASSETS", "USDT,USDC,FDUSD,DAI,TUSD,USDP,AEUR").split(",") if a.strip()}
MIN_VALUE_USD = _parse_float_strict("MIN_VALUE_USD", 1.0)
COOLDOWN_SEC = _parse_int_strict("COOLDOWN_SEC", 3600)
STATE_DIR = os.path.expanduser(os.environ.get("STATE_DIR", "~/.config/binance-tp12"))
LOG_FILE = os.environ.get("LOG_FILE", "")
if LOG_FILE:
    log.addHandler(logging.FileHandler(LOG_FILE))
RECV_WINDOW = 10000


def validate_config():
    """启动时校验数值配置，非法直接拒绝启动（防配错导致误卖/退出）。"""
    problems = []
    if not (math.isfinite(TP_PCT) and TP_PCT >= 0):
        problems.append(f"TP_PCT={TP_PCT!r} 非法：必须为有限非负数")
    if not (math.isfinite(POLL_SEC) and 0 < POLL_SEC <= MAX_POLL_SEC):
        problems.append(f"POLL_SEC={POLL_SEC!r} 非法：必须在 (0, {MAX_POLL_SEC}] 内")
    if not (math.isfinite(MIN_VALUE_USD) and MIN_VALUE_USD >= 0):
        problems.append(f"MIN_VALUE_USD={MIN_VALUE_USD!r} 非法：必须 >= 0")
    if not (math.isfinite(COOLDOWN_SEC) and COOLDOWN_SEC >= 0):
        problems.append(f"COOLDOWN_SEC={COOLDOWN_SEC!r} 非法：必须 >= 0")
    if not QUOTE_ASSET:
        problems.append("QUOTE_ASSET 为空")
    if problems:
        for p in problems:
            log.error("配置错误：%s", p)
        sys.exit(2)


# ---------------------------------------------------------------- 异常


class BinanceAPIError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"Binance error {code}: {msg}")
        self.code = code
        self.msg = msg
        self.order_id = None  # Convert 场景下可附订单号，供恢复查询


# ---------------------------------------------------------------- HTTP 与签名

_clock_offset_ms = 0  # server_time - local_time


def _now_ms():
    return int(time.time() * 1000) + _clock_offset_ms


def _sign(params):
    query = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), query.encode(),
                   hashlib.sha256).hexdigest()
    return query + "&signature=" + sig


def _do_request(method, path, params, signed):
    params = dict(params or {})
    url = API_BASE + path
    data = None
    headers = {"X-MBX-APIKEY": API_KEY}
    if signed:
        params["timestamp"] = _now_ms()
        params["recvWindow"] = RECV_WINDOW
        url += "?" + _sign(params)
        if method == "POST":
            data = b""  # 参数全在 query string 里
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body)
            raise BinanceAPIError(err.get("code"), err.get("msg", body))
        except (ValueError, AttributeError):
            raise BinanceAPIError(e.code, body[:300])


def _request(method, path, params=None, signed=False, _retried=False):
    """发请求。遇到 -1021（时间戳）时重同步时钟并重试一次。"""
    try:
        return _do_request(method, path, params, signed)
    except BinanceAPIError as e:
        if e.code == -1021 and signed and not _retried:
            log.warning("时间戳被拒(-1021)，重同步时钟后重试一次")
            sync_clock()
            return _request(method, path, params, signed, _retried=True)
        raise


def sync_clock():
    """对一次服务器时间，消除本机时钟偏差导致的 -1021 签名错误。"""
    global _clock_offset_ms
    try:
        server = _do_request("GET", "/api/v3/time", None, False)["serverTime"]
        _clock_offset_ms = server - int(time.time() * 1000)
        log.info("时钟同步完成，偏差 %+d ms", _clock_offset_ms)
    except Exception as e:  # 对时失败不致命：调用方会重试/下一轮继续
        log.warning("时钟同步失败（%s），继续用本机时间", e)


# ---------------------------------------------------------------- 现货数据

_exchange_symbols = None  # {baseAsset: [symbol, ...]}，启动时拉一次
_symbols_refreshed = False


def get_exchange_symbols():
    """建 baseAsset -> symbols 映射。

    注意：这里不过滤交易对状态——成本计算需要的是"历史上用过"的
    交易对，已下架的一样要能查到成交（查不到时 myTrades 会报 -1121，
    调用方按空处理）。
    """
    global _exchange_symbols
    if _exchange_symbols is None:
        info = _request("GET", "/api/v3/exchangeInfo")
        mapping = {}
        for s in info.get("symbols", []):
            mapping.setdefault(s["baseAsset"], []).append(s["symbol"])
        _exchange_symbols = mapping
        log.info("exchangeInfo 加载：%d 个交易对", sum(len(v) for v in mapping.values()))
    return _exchange_symbols


def get_balances():
    """返回 {asset: {'free': Decimal, 'locked': Decimal}}（仅现货账户）。"""
    acct = _request("GET", "/api/v3/account", signed=True)
    out = {}
    for b in acct.get("balances", []):
        free = Decimal(b["free"])
        locked = Decimal(b["locked"])
        if free + locked > 0:
            out[b["asset"].upper()] = {"free": free, "locked": locked}
    return out


def get_all_prices():
    """返回 {symbol: price(float)} 全量现货价格表（一次请求）。"""
    return {t["symbol"]: float(t["price"])
            for t in _request("GET", "/api/v3/ticker/price")}


def price_of(asset, prices):
    """按 USDC -> USDT -> FDUSD 顺序取估值价；都没有返回 (None, None)。"""
    for q in ("USDC", "USDT", "FDUSD"):
        sym = asset + q
        if sym in prices:
            return prices[sym], sym
    return None, None


def get_my_trades(symbol):
    """拉取某交易对全部历史成交，返回 (trades, truncated)。

    注意 myTrades 默认返回"最近"的成交，fromId 语义是 id >= fromId，
    所以必须从 fromId=1 开始向前翻页才能拿全历史（不能先拿最近再往后翻）。
    truncated=True 表示触及分页硬上限，历史不全，调用方不得使用该成本。
    """
    trades = []
    seen = set()
    from_id = 1
    pages = 0
    truncated = False
    while True:
        try:
            batch = _request("GET", "/api/v3/myTrades",
                             {"symbol": symbol, "fromId": from_id, "limit": 1000},
                             signed=True)
        except BinanceAPIError as e:
            if e.code == -1121:  # Invalid symbol：该交易对不存在
                return [], False
            raise
        new = [t for t in batch if t["id"] not in seen]
        if not new:
            break
        seen.update(t["id"] for t in new)
        trades.extend(new)
        pages += 1
        if len(batch) < 1000:
            break
        if pages >= MAX_TRADE_PAGES:
            truncated = True
            break
        from_id = max(t["id"] for t in batch) + 1
    trades.sort(key=lambda t: (t["time"], t["id"]))
    return trades, truncated


def _spot_trade_events(asset):
    """拉取现货成交事件（按时间排序）。

    返回 (trades, reason)：口径完整时 trades 为列表（可能为空）；
    口径不完整（非美元计价对有成交/历史截断/无交易对）时返回 (None, reason)。
    """
    global _exchange_symbols, _symbols_refreshed
    symbols = get_exchange_symbols().get(asset)
    if not symbols and not _symbols_refreshed:
        _exchange_symbols = None  # 资产不在缓存里时刷新一次（防新交易对漏检）
        _symbols_refreshed = True
        symbols = get_exchange_symbols().get(asset)
    if not symbols:
        return None, "无现货交易对"
    usd_symbols, other_symbols = [], []
    for s in symbols:
        quote = s[len(asset):]
        (usd_symbols if quote in USD_STABLE_QUOTES else other_symbols).append(s)

    for s in other_symbols:
        tr, trunc = get_my_trades(s)
        if tr or trunc:
            log.warning("跳过 %s：%s 有成交记录，成本口径不完整；"
                        "可用 cost_basis.json 手动覆盖成本", asset, s)
            return None, "非美元计价对有成交"

    trades = []
    for s in usd_symbols:
        tr, trunc = get_my_trades(s)
        if trunc:
            log.warning("跳过 %s：%s 历史成交超过分页上限，成本不可用",
                        asset, s)
            return None, "历史成交截断"
        trades.extend(tr)
    # 防重：同一成交不会出现在两个交易对，无需去重
    trades.sort(key=lambda t: (t["time"], t["id"]))
    return trades, ""


def avg_cost_from_trades(asset):
    """用 myTrades 按平均成本法算持仓均价（Decimal）。

    注意：手续费未计入成本（会轻微低估成本、略微提前触发，
    12% 的止盈线足以覆盖这点偏差）。
    v0.4：Convert 卖出的扣减移到 position_state 做时间合并回放，
    本函数保持"纯现货回放"语义不变。
    """
    trades, _ = _spot_trade_events(asset)
    if not trades:
        return None, Decimal(0)
    qty, cost = Decimal(0), Decimal(0)
    for t in trades:
        q = Decimal(str(t["qty"]))
        if t["isBuyer"]:
            qty += q
            cost += Decimal(str(t["quoteQty"]))  # 用成交额，比 qty*price 精确
        elif qty > 0:
            avg = cost / qty
            sold = min(q, qty)
            cost -= sold * avg
            qty -= sold
    if qty <= 0:
        return None, Decimal(0)
    return cost / qty, qty


def get_convert_trades(known_order_ids):
    """近 90 天闪兑历史（tradeFlow，30 天分窗），best-effort。

    只收 orderStatus==SUCCESS 的行；去重掉 known_order_ids（脚本自有卖出）。
    任一窗口失败 -> 返回 None，调用方降级为只用脚本自有卖出记录
    （窗口不全会有偏差，不做部分合并）。
    """
    out = []
    now_ms = int(time.time() * 1000)
    try:
        for i in range(3):
            end = now_ms - i * 30 * 86400 * 1000
            start = end - 30 * 86400 * 1000
            rows = _request("GET", "/sapi/v1/convert/tradeFlow",
                            {"startTime": start, "endTime": end, "limit": 1000},
                            signed=True)
            for r in rows or []:
                if r.get("orderStatus") != "SUCCESS":
                    continue
                if str(r.get("orderId")) in known_order_ids:
                    continue
                out.append(r)
    except Exception as e:
        log.warning("tradeFlow 查询失败（%s），本轮仅用脚本自有卖出记录", e)
        return None
    return out


def position_state(asset, free, price, state, overrides, convert_rows):
    """成本账本：现货成交 + Convert 卖出按时间合并回放（DESIGN_v04.md §2）。

    返回 (avg_cost|None, accountable_qty, unknown_value_usd, reason)。
    avg_cost=None 表示成本不可用，调用方必须跳过该资产。
    """
    if asset in overrides:
        return Decimal(str(overrides[asset])), free, Decimal(0), "手动覆盖"
    trades, reason = _spot_trade_events(asset)
    if trades is None:
        return None, Decimal(0), Decimal(0), reason

    # 事件流：(time_ms, seq, kind, qty, amount, is_buyer)
    # kind: spot / convert_sell / convert_buy；amount 对买入是成本额
    events = []
    for t in trades:
        events.append((t["time"], t["id"], "spot", Decimal(str(t["qty"])),
                       Decimal(str(t["quoteQty"])), bool(t["isBuyer"])))
    # 来源 A（精确）：本脚本的卖出记录
    for s in state.get("sells", []):
        if str(s.get("asset")).upper() != asset:
            continue
        try:
            ts = int(datetime.fromisoformat(s["time"]).timestamp() * 1000)
        except Exception:
            ts = 0
        events.append((ts, 10 ** 18, "convert_sell",
                       Decimal(str(s["qty"])), None, False))
    # 来源 B（best-effort）：tradeFlow 手工闪兑
    if convert_rows:
        for r in convert_rows:
            cts = int(r.get("createTime", 0) or 0)
            if str(r.get("fromAsset")).upper() == asset:
                events.append((cts, 10 ** 18 + 1, "convert_sell",
                               Decimal(str(r["fromAmount"])), None, False))
            elif str(r.get("toAsset")).upper() == asset:
                fa = str(r.get("fromAsset")).upper()
                if fa in USD_STABLE_QUOTES:
                    events.append((cts, 10 ** 18 + 2, "convert_buy",
                                   Decimal(str(r["toAmount"])),
                                   Decimal(str(r["fromAmount"])), True))
                else:
                    log.warning("跳过 %s：手工闪兑买入计价 %s 非稳定币，"
                                "成本不可用", asset, fa)
                    return None, Decimal(0), Decimal(0), "手工闪兑买入非稳定币计价"
    events.sort(key=lambda e: (e[0], e[1]))

    qty, cost = Decimal(0), Decimal(0)
    for _, _, kind, q, amount, is_buyer in events:
        if kind == "convert_sell":
            if qty > 0:  # 平均成本法：卖出只减数量，不改单位成本
                avg = cost / qty
                sold = min(q, qty)
                cost -= sold * avg
                qty -= sold
            # 卖出超过账面：多余部分视为未知来源的卖出，不影响剩余成本
        else:  # spot / convert_buy 的买入；spot 的卖出走 qty>0 分支同逻辑
            if is_buyer:
                qty += q
                cost += amount
            elif qty > 0:
                avg = cost / qty
                sold = min(q, qty)
                cost -= sold * avg
                qty -= sold
    if qty <= 0:
        return None, Decimal(0), Decimal(0), "账面持仓为零"
    avg_cost = cost / qty

    # 未知成本隔离：账面解释不了的余额，未知部分价值超阈值则整单跳过
    # （替代 v0.3 的 0.9 容差；覆盖转入/空投/已下架对缺口）
    accountable = min(qty, free)
    unknown_qty = free - accountable
    unknown_value = unknown_qty * Decimal(str(price)) \
        if unknown_qty > 0 else Decimal(0)
    if unknown_value > Decimal(str(MIN_VALUE_USD)):
        log.warning("跳过 %s：账面可解释 %s，实际 %s，未知成本部分价值约 %s USD "
                    "超过阈值；可用 cost_basis.json 手动覆盖", asset,
                    fmt_amount(qty), fmt_amount(free),
                    fmt_amount(unknown_value))
        return None, accountable, unknown_value, "未知成本部分超阈值"
    return avg_cost, accountable, unknown_value, "账本回放"


# ---------------------------------------------------------------- 闪兑

def fmt_amount(d):
    """Decimal -> 定点十进制字符串（杜绝科学计数法）。"""
    s = format(Decimal(d).normalize(), "f")
    return s if s != "-0" else "0"


def quantize_down(d, places=8):
    """向下量化到指定小数位（防御性：无论接口精度如何都不超发）。"""
    q = Decimal(1).scaleb(-places)
    return Decimal(d).quantize(q, rounding=ROUND_DOWN)


_convert_limits_cache = {}


def get_convert_limits(from_asset, to_asset):
    """返回 (min_amount: Decimal, max_amount: Decimal|None)。结果缓存。"""
    key = (from_asset, to_asset)
    if key not in _convert_limits_cache:
        info = _request("GET", "/sapi/v1/convert/exchangeInfo",
                        {"fromAsset": from_asset, "toAsset": to_asset},
                        signed=True)
        pairs = info if isinstance(info, list) else []
        if not pairs:
            raise BinanceAPIError("NO_PAIR",
                                  f"闪兑不支持 {from_asset}->{to_asset}")
        lim = pairs[0]
        min_amt = Decimal(str(lim.get("fromAssetMinAmount") or 0))
        max_raw = lim.get("fromAssetMaxAmount") or 0
        max_amt = Decimal(str(max_raw)) if Decimal(str(max_raw)) > 0 else None
        _convert_limits_cache[key] = (min_amt, max_amt)
    return _convert_limits_cache[key]


def convert_quote(from_asset, to_asset, from_amount):
    """询价。返回 (quote_id, to_amount: Decimal, ratio: Decimal)。"""
    min_amt, max_amt = get_convert_limits(from_asset, to_asset)
    if from_amount < min_amt or (max_amt is not None and from_amount > max_amt):
        raise BinanceAPIError(
            "AMOUNT_OUT_OF_RANGE",
            f"数量 {fmt_amount(from_amount)} 超出闪兑限额")
    q = _request("POST", "/sapi/v1/convert/getQuote",
                 {"fromAsset": from_asset, "toAsset": to_asset,
                  "fromAmount": fmt_amount(from_amount), "walletType": "SPOT"},
                 signed=True)
    return q["quoteId"], Decimal(str(q["toAmount"])), Decimal(str(q["ratio"]))


# 官方枚举：PROCESS / ACCEPT_SUCCESS / SUCCESS / FAIL
_CONVERT_DONE_OK = {"SUCCESS"}
_CONVERT_DONE_FAIL = {"FAIL"}
_CONVERT_PENDING = {"PROCESS", "ACCEPT_SUCCESS"}


class SellHalted(Exception):
    """兑换中断：请求可能已发出但结果未知。

    必带 quote_id（恢复凭据），可能带 order_id。
    调用方必须把该资产记入 pending 并冻结，下轮凭 quoteId/orderId 恢复，
    绝不能当作"没发出去"直接重试。
    """
    def __init__(self, quote_id, order_id=None, reason=""):
        super().__init__(reason or "兑换结果未知")
        self.quote_id = quote_id
        self.order_id = order_id
        self.reason = reason


class ChunkFailed(Exception):
    """兑换块已知失败（FAIL 终态）：可安全重试，不冻结资产。"""
    pass


def convert_accept(quote_id, timeout_sec=30):
    """确认兑换并轮询到终态。返回 (to_amount: Decimal, order_id)。

    结果未知时抛 SellHalted（必带 quote_id）；已知失败抛 ChunkFailed。
    任何其他异常都不允许穿透为"未知"——v0.3 只抓 BinanceAPIError，
    URLError 直接穿透导致无 pending、无冻结，是重复兑换的根因。
    """
    try:
        r = _request("POST", "/sapi/v1/convert/acceptQuote",
                     {"quoteId": quote_id}, signed=True)
    except Exception as e:
        # accept 请求可能已送达但响应丢失：是否执行未知，且无 orderId
        raise SellHalted(quote_id, None,
                         f"acceptQuote 异常，结果未知：{e}")
    order_id = r["orderId"]
    deadline = time.time() + timeout_sec
    try:
        while time.time() < deadline:
            st = _request("GET", "/sapi/v1/convert/orderStatus",
                          {"orderId": order_id}, signed=True)
            status = st.get("orderStatus")
            if status in _CONVERT_DONE_OK:
                return Decimal(str(st["toAmount"])), order_id
            if status in _CONVERT_DONE_FAIL:
                raise ChunkFailed(f"闪兑订单 {order_id} 已失败({status})")
            if status not in _CONVERT_PENDING:
                raise SellHalted(quote_id, order_id,
                                 f"闪兑订单 {order_id} 未知状态 {status!r}")
            time.sleep(2)
        raise SellHalted(quote_id, order_id,
                         f"闪兑订单 {order_id} 轮询超时未终结")
    except (SellHalted, ChunkFailed):
        raise
    except Exception as e:
        # orderStatus 查询的业务/网络异常：订单结果未知
        raise SellHalted(quote_id, order_id, f"orderStatus 查询异常：{e}")


# ---------------------------------------------------------------- 卖出状态机
# 设计见 DESIGN_v04.md §1。核心不变量：
#  - 拿到 quoteId 后、调用 acceptQuote 前，必须先落盘（崩溃点覆盖）。
#  - 结果未知 => 资产冻结，下轮凭 quoteId/orderId 恢复，绝不直接重试。
#  - 分块卖出是一个 op：chunksDone/remaining 持久化，恢复后继续卖剩余。

# 恢复尝试上限：按 30 秒一轮，60 次 ≈ 30 分钟后转人工
MAX_RECOVER_ATTEMPTS = 60
# 未知订单状态连续出现上限：10 次后转人工（不再无限当处理中）
MAX_UNKNOWN_STREAK = 10
# quoteId 查无订单：连续 10 次且超过报价有效期后判定为未执行
MAX_NOTFOUND_STREAK = 10
NOTFOUND_MIN_AGE_SEC = 300


def _new_sell_op(asset, to_asset, amount, now):
    return {
        "op": "sell_all",
        "asset": asset,
        "toAsset": to_asset,
        "chunksDone": [],    # [{fromAmount, toAmount, orderId}]
        "current": None,     # {quoteId, orderId, fromAmount, state}
        "remaining": fmt_amount(quantize_down(amount)),
        "attempts": 0,
        "unknownStreak": 0,
        "notFoundStreak": 0,
        "firstSeen": now,
        "lastTry": now,
        "note": "",
    }


def _sell_chunks(asset, rec, state):
    """执行 rec["remaining"] 的分块卖出。

    返回 "sold"（全卖完）/ "partial"（某块已知失败）/ "pending"（某块
    结果未知，已落盘冻结）。调用方对 "pending" 不得再动该资产。
    """
    to_asset = rec["toAsset"]
    min_amt, max_amt = get_convert_limits(asset, to_asset)
    while True:
        remaining = Decimal(rec["remaining"])
        if remaining <= 0:
            break
        chunk = min(remaining, max_amt) if max_amt else remaining
        chunk = quantize_down(chunk)
        if chunk <= 0 or chunk < min_amt:
            log.warning("%s 剩余 %s 低于闪兑最小额，停止拆分（留待下轮或人工）",
                        asset, fmt_amount(remaining))
            break
        quote_id, _, _ = convert_quote(asset, to_asset, chunk)
        # 先落盘后 accept：崩溃点覆盖
        state["pending"][asset] = rec
        rec["current"] = {"quoteId": quote_id, "orderId": None,
                          "fromAmount": fmt_amount(chunk),
                          "state": "result_unknown"}
        rec["lastTry"] = time.time()
        save_state_quiet(state)
        try:
            to_amount, order_id = convert_accept(quote_id)
        except ChunkFailed as e:
            log.error("%s 分块 %s 已知失败（%s），本轮停止", asset,
                      fmt_amount(chunk), e)
            rec["current"] = None
            save_state_quiet(state)
            return "partial"
        except SellHalted as e:
            log.error("%s 分块 %s 结果未知（%s），已冻结待恢复",
                      asset, fmt_amount(chunk), e.reason)
            cur = rec["current"]
            cur["orderId"] = str(e.order_id) if e.order_id else None
            if cur["orderId"]:
                cur["state"] = "processing"
            rec["attempts"] = 0
            rec["unknownStreak"] = 0
            rec["notFoundStreak"] = 0
            save_state_quiet(state)
            return "pending"
        rec["chunksDone"].append({"fromAmount": fmt_amount(chunk),
                                  "toAmount": fmt_amount(to_amount),
                                  "orderId": str(order_id)})
        rec["remaining"] = fmt_amount(remaining - chunk)
        rec["current"] = None
        save_state_quiet(state)
        log.info("%s 分块成交 %s -> %s %s（订单 %s），剩余 %s",
                 asset, fmt_amount(chunk), fmt_amount(to_amount), to_asset,
                 order_id, rec["remaining"])
    return "sold"


def _finalize_sale(asset, rec, state, now, full):
    """结算一次卖出 op：记 sell、full 时设冷却、删 pending。"""
    chunks = rec["chunksDone"]
    sold_qty = sum(Decimal(c["fromAmount"]) for c in chunks)
    proceeds = sum(Decimal(c["toAmount"]) for c in chunks)
    order_ids = [c["orderId"] for c in chunks]
    if sold_qty > 0:
        entry = {
            "asset": asset, "qty": float(sold_qty),
            "proceeds": float(proceeds),
            "proceeds_asset": rec["toAsset"], "order_ids": order_ids,
            "time": datetime.now(timezone.utc).isoformat(),
            "partial": not full,
        }
        if rec.get("profitPct") is not None:
            entry["profit_pct"] = rec["profitPct"]
        state["sells"].append(entry)
        log.warning("已止盈 %s：卖出 %s，实得 %s %s（订单 %s）%s",
                    asset, fmt_amount(sold_qty), fmt_amount(proceeds),
                    rec["toAsset"], ",".join(order_ids),
                    "" if full else "（部分卖出，未设冷却）")
    if full:
        state["cooldown"][asset] = now + COOLDOWN_SEC
        mem_cooldown[asset] = now + COOLDOWN_SEC  # 文件保存失败时的兜底
    state["pending"].pop(asset, None)
    save_state_quiet(state)


def _settle(asset, rec, state, now, outcome):
    """把 _sell_chunks 的 outcome 落到最终状态。返回 "sold"/"partial"/"blocked"。"""
    if outcome == "pending":
        return "blocked"  # rec 已在 pending 中冻结
    _finalize_sale(asset, rec, state, now, full=(outcome == "sold"))
    return "sold" if outcome == "sold" else "partial"


def _to_manual(asset, rec, state, note):
    """转人工接管：终态，资产永久冻结，每轮 CRITICAL 提醒。"""
    rec["needs_manual"] = True
    rec["note"] = note
    save_state_quiet(state)
    log.critical("%s 卖出进入人工接管：%s。资产已冻结；核对后删除 "
                 "state.json 中 pending.%s 解冻", asset, note, asset)
    return "manual"


def _recover_unknown(asset, rec, state, now):
    """result_unknown 恢复：凭 quoteId 查 orderStatus（官方支持）。"""
    cur = rec["current"]
    try:
        st = _request("GET", "/sapi/v1/convert/orderStatus",
                      {"quoteId": cur["quoteId"]}, signed=True)
    except BinanceAPIError as e:
        rec["notFoundStreak"] = rec.get("notFoundStreak", 0) + 1
        rec["lastTry"] = now
        save_state_quiet(state)
        age = now - rec.get("firstSeen", now)
        if rec["notFoundStreak"] >= MAX_NOTFOUND_STREAK and \
                age > NOTFOUND_MIN_AGE_SEC:
            # 报价有效期最长 1 分钟；连续查无且远超有效期 => 判定未执行。
            # 即使误判：真执行过则余额已变 0，下轮 free=0 会跳过，不会重复卖出。
            log.warning("%s quoteId %s 连续 %d 次查无订单且已超报价有效期，"
                        "判定为未执行，解冻资产", asset, cur["quoteId"],
                        rec["notFoundStreak"])
            state["pending"].pop(asset, None)
            save_state_quiet(state)
            return "unblocked"
        log.info("%s quoteId %s 恢复查询业务报错（%s），保持冻结（%d 次）",
                 asset, cur["quoteId"], e, rec["notFoundStreak"])
        return "blocked"
    except Exception as e:
        rec["attempts"] = rec.get("attempts", 0) + 1
        rec["lastTry"] = now
        save_state_quiet(state)
        if rec["attempts"] >= MAX_RECOVER_ATTEMPTS:
            return _to_manual(asset, rec, state, f"恢复查询网络异常超限：{e}")
        log.error("%s quoteId %s 恢复查询网络异常（%s），保持冻结",
                  asset, cur["quoteId"], e)
        return "blocked"
    rec["notFoundStreak"] = 0
    oid = st.get("orderId")
    if not oid:
        rec["attempts"] = rec.get("attempts", 0) + 1
        rec["lastTry"] = now
        save_state_quiet(state)
        log.warning("%s quoteId %s 查询返回无 orderId，保持冻结",
                    asset, cur["quoteId"])
        return "blocked"
    cur["orderId"] = str(oid)
    cur["state"] = "processing"
    save_state_quiet(state)
    log.info("%s quoteId %s 找回订单 %s，转入终态查询",
             asset, cur["quoteId"], oid)
    return _recover_processing(asset, rec, state, now)


def _recover_processing(asset, rec, state, now):
    """processing 恢复：凭 orderId 查终态。"""
    cur = rec["current"]
    try:
        st = _request("GET", "/sapi/v1/convert/orderStatus",
                      {"orderId": cur["orderId"]}, signed=True)
    except Exception as e:
        rec["attempts"] = rec.get("attempts", 0) + 1
        rec["lastTry"] = now
        save_state_quiet(state)
        if rec["attempts"] >= MAX_RECOVER_ATTEMPTS:
            return _to_manual(asset, rec, state, f"终态查询异常超限：{e}")
        log.error("%s 订单 %s 终态查询异常（%s），保持冻结",
                  asset, cur["orderId"], e)
        return "blocked"
    status = st.get("orderStatus")
    if status in _CONVERT_DONE_OK:
        chunk = Decimal(cur["fromAmount"])
        got = Decimal(str(st.get("toAmount", cur["fromAmount"])))
        rec["chunksDone"].append({"fromAmount": fmt_amount(chunk),
                                  "toAmount": fmt_amount(got),
                                  "orderId": str(cur["orderId"])})
        rec["remaining"] = fmt_amount(Decimal(rec["remaining"]) - chunk)
        rec["current"] = None
        rec["attempts"] = 0
        rec["unknownStreak"] = 0
        save_state_quiet(state)
        log.warning("%s 在途块恢复确认为成功（订单 %s），继续卖剩余 %s",
                    asset, cur["orderId"], rec["remaining"])
        try:
            outcome = _sell_chunks(asset, rec, state)
        except Exception as e:
            log.error("%s 恢复后继续卖出异常（%s），保持冻结", asset, e)
            return "blocked"
        return _settle(asset, rec, state, now, outcome)
    if status in _CONVERT_DONE_FAIL:
        log.warning("%s 在途块已失败（订单 %s），按部分卖出结算",
                    asset, cur["orderId"])
        rec["current"] = None
        return _settle(asset, rec, state, now, "partial")
    if status in _CONVERT_PENDING:
        rec["attempts"] = rec.get("attempts", 0) + 1
        rec["lastTry"] = now
        save_state_quiet(state)
        if rec["attempts"] >= MAX_RECOVER_ATTEMPTS:
            return _to_manual(asset, rec, state,
                              f"订单 {cur['orderId']} 长时间未终结")
        log.info("%s 订单 %s 仍在处理中（%s），保持冻结",
                 asset, cur["orderId"], status)
        return "blocked"
    rec["unknownStreak"] = rec.get("unknownStreak", 0) + 1
    rec["lastTry"] = now
    save_state_quiet(state)
    if rec["unknownStreak"] >= MAX_UNKNOWN_STREAK:
        return _to_manual(asset, rec, state,
                          f"订单 {cur['orderId']} 未知状态 {status!r} 连续超限")
    log.warning("%s 订单 %s 未知状态 %r（%d 次），保持冻结",
                asset, cur["orderId"], status, rec["unknownStreak"])
    return "blocked"


def _recover_one(asset, rec, state, now):
    """恢复单个 pending 卖出。返回 sold/partial/blocked/unblocked/manual。"""
    if rec.get("needs_manual"):
        log.critical("%s 卖出 pending 需人工处理（%s），保持冻结；"
                     "核对后删除 state.json 中 pending.%s 解冻",
                     asset, rec.get("note"), asset)
        return "manual"
    cur = rec.get("current")
    if not cur:
        # 中断时无在途块：直接继续卖剩余
        log.info("%s pending 无在途块，继续卖剩余 %s", asset, rec.get("remaining"))
        try:
            outcome = _sell_chunks(asset, rec, state)
        except Exception as e:
            log.error("%s pending 继续卖出异常（%s），保持冻结", asset, e)
            return "blocked"
        return _settle(asset, rec, state, now, outcome)
    if cur.get("state") == "result_unknown":
        return _recover_unknown(asset, rec, state, now)
    return _recover_processing(asset, rec, state, now)


def _migrate_legacy_pending(asset, rec, state, now):
    """v0.3 遗留的纯字符串 pending（orderId）升级为 op 记录；非法则丢弃。"""
    if isinstance(rec, str) and rec:
        new = _new_sell_op(asset, QUOTE_ASSET, 0, now)
        new["current"] = {"quoteId": None, "orderId": rec,
                          "fromAmount": "0", "state": "processing"}
        new["remaining"] = "0"
        new["note"] = "v0.3 遗留 pending，已升级"
        state["pending"][asset] = new
        save_state_quiet(state)
        log.info("%s v0.3 遗留 pending 已升级为状态机记录", asset)
        return new
    log.warning("pending 记录结构非法已丢弃：%s=%r", asset, rec)
    state["pending"].pop(asset, None)
    return None


def recover_pending(state, now):
    """恢复所有 pending 卖出。独立于当前余额（余额归零也恢复）。

    单个资产恢复查询异常只冻结该资产、记 error，绝不中止整轮。
    返回 {asset: outcome}。
    """
    outcomes = {}
    for asset in sorted(state.get("pending", {})):
        rec = state["pending"][asset]
        if not isinstance(rec, dict) or rec.get("op") != "sell_all":
            rec = _migrate_legacy_pending(asset, rec, state, now)
            if rec is None:
                continue
        try:
            outcomes[asset] = _recover_one(asset, rec, state, now)
        except Exception as e:
            log.error("%s pending 恢复发生未预期异常（%s），保持冻结",
                      asset, e)
            outcomes[asset] = "blocked"
    return outcomes


# ---------------------------------------------------------------- 状态文件

# 进程内冷却（状态文件损坏/保存失败时的兜底，不依赖文件）
mem_cooldown = {}


def _state_path():
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, "state.json")


def _empty_state():
    return {"sells": [], "cooldown": {}, "pending": {}}


def load_state():
    """加载状态；文件损坏时备份并重置为空状态（绝不因此中止整轮）。"""
    p = _state_path()
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError) as e:
        log.error("state.json 损坏（%s），已备份并重置为空状态", e)
        try:
            os.replace(p, p + ".corrupt")
        except OSError:
            pass
        return _empty_state()
    # 结构校验
    if not isinstance(data, dict) or \
            not isinstance(data.get("sells"), list) or \
            not isinstance(data.get("cooldown"), dict) or \
            not isinstance(data.get("pending"), dict):
        log.error("state.json 结构非法，已备份并重置为空状态")
        try:
            os.replace(p, p + ".corrupt")
        except OSError:
            pass
        return _empty_state()
    now = time.time()
    clean_cd = {}
    for k, v in data["cooldown"].items():
        try:
            ts = float(v)
            if math.isfinite(ts) and ts > now:  # 只保留未过期的
                clean_cd[str(k)] = ts
        except (TypeError, ValueError):
            log.warning("冷却值非法已丢弃：%s=%r", k, v)
    data["cooldown"] = clean_cd
    clean_pending = {}
    for k, v in data["pending"].items():
        if isinstance(v, str) and v:
            clean_pending[str(k)] = v  # v0.3 遗留纯 orderId，恢复时升级
        elif isinstance(v, dict) and v.get("op") == "sell_all":
            clean_pending[str(k)] = v
        else:
            log.warning("待确认订单非法已丢弃：%s=%r", k, v)
    data["pending"] = clean_pending
    return data


def save_state(state):
    tmp = _state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _state_path())


def save_state_quiet(state):
    """保存失败只记 critical，不抛异常（调用方用 mem_cooldown 兜底）。"""
    try:
        save_state(state)
    except OSError as e:
        log.critical("state.json 保存失败（%s），冷却改由进程内记忆兜底", e)


def load_cost_overrides():
    """cost_basis.json 手动成本覆盖。值必须为有限正数（拒绝 bool），
    非法条目告警并跳过。"""
    p = os.path.join(STATE_DIR, "cost_basis.json")
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("cost_basis.json 读取失败（%s），视为无覆盖", e)
        return {}
    if not isinstance(data, dict):
        log.error("cost_basis.json 顶层不是对象，视为无覆盖")
        return {}
    out = {}
    for k, v in data.items():
        if isinstance(v, bool):
            log.warning("成本覆盖非法已跳过：%s=%r（布尔值不是合法成本）", k, v)
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            log.warning("成本覆盖非法已跳过：%s=%r（不是数字）", k, v)
            continue
        if not (math.isfinite(fv) and fv > 0):
            log.warning("成本覆盖非法已跳过：%s=%r（必须为有限正数）", k, v)
            continue
        out[str(k).upper()] = fv
    return out


# ---------------------------------------------------------------- 主逻辑

def check_once():
    """跑一轮检查。返回触发卖出的币种列表。

    顺序：先恢复所有 pending（独立于余额）-> 再逐资产用成本账本评估 ->
    触发则经卖出状态机执行。pending 冻结中的资产本轮跳过。
    """
    state = load_state()
    overrides = load_cost_overrides()
    now = time.time()

    # 1. 恢复所有 pending（独立于当前余额；单个资产异常不中止整轮）
    outcomes = recover_pending(state, now)
    recovered = [a for a, o in outcomes.items() if o in ("sold", "partial")]
    if recovered:
        log.warning("本轮 pending 恢复完成卖出：%s", ", ".join(recovered))

    balances = get_balances()
    prices = get_all_prices()

    # tradeFlow 本轮拉一次（best-effort），供成本账本合并手工闪兑
    known_oids = set()
    for s in state.get("sells", []):
        for oid in s.get("order_ids", []):
            known_oids.add(str(oid))
    convert_rows = get_convert_trades(known_oids)

    triggered = []
    assets = [a for a in balances if a not in EXCLUDE_ASSETS]
    log.info("本轮检查 %d 个币种（已排除稳定币 %d 个）",
             len(assets), len(balances) - len(assets))

    for asset in sorted(assets):
        bal = balances[asset]
        free = bal["free"]
        if free <= 0:
            continue  # 只有锁定余额，无可卖数量
        if bal["locked"] > 0:
            log.info("%s 有 %s 处于锁定（挂单中等），本次只处理可用 %s",
                     asset, fmt_amount(bal["locked"]), fmt_amount(free))

        # pending 冻结中（本轮已恢复过）：跳过
        if asset in state.get("pending", {}):
            log.info("%s 有待确认卖出，本轮跳过", asset)
            continue

        price, sym = price_of(asset, prices)
        if price is None:
            log.warning("跳过 %s：无 USDC/USDT/FDUSD 计价", asset)
            continue
        est_value = float(free) * price
        if est_value < MIN_VALUE_USD:
            continue  # 灰尘仓位，跳过不打扰

        # --- 成本账本（现货成交 + Convert 卖出合并回放 + 未知成本隔离）
        avg_cost, accountable, unknown_v, reason = position_state(
            asset, free, price, state, overrides, convert_rows)
        if avg_cost is None:
            log.warning("跳过 %s：成本不可用（%s），可用 cost_basis.json 手动覆盖",
                        asset, reason)
            continue

        profit = (Decimal(str(price)) - avg_cost) / avg_cost
        log.info("%s 现价 %s(%s) 成本 %s(%s) 收益 %+.2f%% 持仓 %s",
                 asset, price, sym, fmt_amount(avg_cost), reason,
                 float(profit) * 100, fmt_amount(free))

        if profit < Decimal(str(TP_PCT)):
            continue
        if mem_cooldown.get(asset, 0) > now or \
                state["cooldown"].get(asset, 0) > now:
            log.info("%s 在卖出冷却期内，跳过", asset)
            continue

        # --- 触发止盈：闪兑全部可用余额 -> QUOTE_ASSET（状态机执行）
        if DRY_RUN:
            try:
                quote_id, to_amount, ratio = convert_quote(
                    asset, QUOTE_ASSET, quantize_down(free))
            except BinanceAPIError as e:
                log.error("%s 闪兑询价失败，跳过：%s", asset, e)
                continue
            log.warning("[DRY-RUN] 会卖出 %s %s -> 约 %s %s "
                        "(收益 %+.2f%%，成本 %s)，未真实下单",
                        asset, fmt_amount(free), fmt_amount(to_amount),
                        QUOTE_ASSET, float(profit) * 100, fmt_amount(avg_cost))
            triggered.append(asset)
            continue

        rec = _new_sell_op(asset, QUOTE_ASSET, free, now)
        rec["profitPct"] = round(float(profit) * 100, 2)
        try:
            outcome = _sell_chunks(asset, rec, state)
        except Exception as e:
            if not rec["chunksDone"] and not rec.get("current"):
                log.error("%s 卖出启动失败（%s），未冻结，下轮重试", asset, e)
            else:
                state["pending"][asset] = rec
                save_state_quiet(state)
                log.error("%s 卖出异常（%s），已冻结待恢复", asset, e)
            continue
        result = _settle(asset, rec, state, now, outcome)
        if result in ("sold", "partial"):
            triggered.append(asset)

    return triggered


def validate_quote_asset():
    """启动时校验 QUOTE_ASSET 有闪兑路径；明确无路径则拒绝启动。"""
    try:
        info = _request("GET", "/sapi/v1/convert/exchangeInfo",
                        {"fromAsset": "BTC", "toAsset": QUOTE_ASSET},
                        signed=True)
    except BinanceAPIError as e:
        if e.code == "NO_PAIR":
            log.error("QUOTE_ASSET=%s 无闪兑路径，拒绝启动", QUOTE_ASSET)
            sys.exit(2)
        log.warning("QUOTE_ASSET 启动校验遇到接口错误（%s），继续运行", e)
        return
    except Exception as e:
        log.warning("QUOTE_ASSET 启动校验遇到网络错误（%s），继续运行", e)
        return
    pairs = info if isinstance(info, list) else []
    if not pairs:
        log.error("QUOTE_ASSET=%s 无闪兑路径，拒绝启动", QUOTE_ASSET)
        sys.exit(2)
    log.info("QUOTE_ASSET=%s 闪兑路径校验通过", QUOTE_ASSET)


def main():
    if not API_KEY or not API_SECRET:
        log.error("缺少 BINANCE_API_KEY / BINANCE_API_SECRET 环境变量，退出")
        sys.exit(2)
    validate_config()
    once = "--once" in sys.argv
    log.warning("启动 tp12 v0.4：止盈线 %.1f%%，目标 %s，轮询 %d 秒，%s",
                TP_PCT * 100, QUOTE_ASSET, POLL_SEC,
                "DRY-RUN（不下单）" if DRY_RUN else "真实交易模式！！")
    sync_clock()
    validate_quote_asset()
    try:
        while True:
            try:
                triggered = check_once()
                if triggered:
                    log.warning("本轮触发止盈：%s", ", ".join(triggered))
            except BinanceAPIError as e:
                log.error("币安接口错误：%s（%d 秒后重试）", e, POLL_SEC)
            except Exception:  # noqa: BLE001 — 主循环永不崩
                log.exception("本轮发生未预期异常（%d 秒后重试）", POLL_SEC)
            if once:
                break
            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        log.warning("收到中断信号，干净退出")
        sys.exit(0)


if __name__ == "__main__":
    main()
