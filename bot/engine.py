"""時間を1本ずつ進めて、約定→判断→記録を行う心臓部。

大事なルール:
- 判断は「その足の終値が確定した時点」で行い、約定は「次の足の始値」。
- 処理済みの時刻(last_ts)を保存しているので、ボットの起動がいくら遅れても、
  まとめて処理しても分割して処理しても、結果は同じになる。

成績の比較用に、毎足あわせて記録するもの:
- gross : 手数料と価格ズレを払わなかった場合の資産(実際の資産 + これまで払ったコスト)
- bench : 運用開始時に全額をベンチマーク(SPY)で買って放置した場合の資産
"""
from __future__ import annotations

import pandas as pd

from .broker import PaperBroker


def new_state(cash: float) -> dict:
    return {
        "cash": cash,
        "starting_cash": cash,
        "positions": {},
        "pending": [],
        "last_close": {},
        "last_ts": None,
        "started_at": None,
        "costs_paid": 0.0,
        "bench_base": None,
        "bench_last": None,
    }


def initialize(state: dict, bars: dict[str, pd.DataFrame], bench: pd.DataFrame | None,
               start: pd.Timestamp) -> dict:
    """start 時点から運用を始める。過去データでは売買しない。"""
    state["last_ts"] = _iso(start)
    state["started_at"] = state["last_ts"]
    for s, df in bars.items():
        upto = df[df.index <= start]
        if not upto.empty:
            state["last_close"][s] = float(upto["close"].iloc[-1])
    if bench is not None:
        upto = bench[bench.index <= start]
        if not upto.empty:
            state["bench_base"] = state["bench_last"] = float(upto["close"].iloc[-1])
    cash = state["cash"]
    return {"ts": state["last_ts"], "equity": cash, "cash": cash, "gross": cash, "bench": cash}


def processing_cutoff(bars: dict[str, pd.DataFrame]) -> pd.Timestamp | None:
    """全銘柄の足が揃っている最新時刻。売買停止などで1時間以上止まっている銘柄は待たない。"""
    latest = [df.index.max() for df in bars.values() if not df.empty]
    if not latest:
        return None
    newest = max(latest)
    alive = [t for t in latest if newest - t <= pd.Timedelta(hours=1)]
    return min(alive)


def step(state: dict, bars: dict[str, pd.DataFrame], strategy, costs: dict,
         cash_buffer: float, cutoff: pd.Timestamp,
         bench: pd.DataFrame | None = None) -> tuple[list, list]:
    """last_ts の次から cutoff までを処理。(約定リスト, 資産推移リスト) を返す。"""
    broker = PaperBroker(state["cash"], costs, state["positions"])
    pending: list[dict] = state["pending"]
    last_close: dict = state["last_close"]
    costs_paid = float(state.get("costs_paid", 0.0))
    bench_base = state.get("bench_base")
    bench_last = state.get("bench_last")
    start_cash = float(state["starting_cash"])
    symbols = list(bars.keys())
    signals = {s: strategy.signals(df) for s, df in bars.items() if not df.empty}

    last_ts = pd.Timestamp(state["last_ts"]) if state["last_ts"] else None
    stamps = set()
    for df in bars.values():
        idx = df.index[df.index <= cutoff]
        if last_ts is not None:
            idx = idx[idx > last_ts]
        stamps.update(idx)
    timeline = sorted(stamps)

    fills, equity_points = [], []
    for ts in timeline:
        here = [s for s in symbols if ts in bars[s].index]

        # 1) 前の足で決めた注文を、この足の始値で約定
        still = []
        for order in pending:
            if order["symbol"] not in here:
                still.append(order)
                continue
            ref = float(bars[order["symbol"]].at[ts, "open"])
            if order["side"] == "BUY":
                res = broker.buy(order["symbol"], order["qty"], ref)
            else:
                res = broker.sell(order["symbol"], order["qty"], ref)
            if res:
                costs_paid += res["fee"] + res["slip"]
                res.update({"ts": _iso(ts), "decided_at": order["decided_at"]})
                fills.append(res)
        pending = still

        # 2) 終値を更新して資産を評価
        for s in here:
            last_close[s] = float(bars[s].at[ts, "close"])
        equity = broker.cash + sum(q * last_close.get(s, 0.0) for s, q in broker.positions.items())
        if bench is not None and ts in bench.index:
            bench_last = float(bench.at[ts, "close"])
            if bench_base is None:
                bench_base = bench_last

        # 3) この足の終値で判断して、次の足用の注文を作る
        reserved = sum(o["est_cost"] for o in pending if o["side"] == "BUY")
        for s in here:
            if any(o["symbol"] == s for o in pending):
                continue
            want = bool(signals[s].get(ts, False))
            pos = broker.position(s)
            close = last_close[s]
            if want and pos == 0:
                budget = equity * (1 - cash_buffer) / len(symbols)
                budget = min(budget, broker.cash - reserved)
                qty = broker.max_affordable(close, budget)
                if qty > 0:
                    est = qty * broker.buy_price(close) + broker.fee(qty)
                    reserved += est
                    pending.append({"symbol": s, "side": "BUY", "qty": qty,
                                    "decided_at": _iso(ts), "est_cost": round(est, 4)})
            elif not want and pos > 0:
                pending.append({"symbol": s, "side": "SELL", "qty": pos,
                                "decided_at": _iso(ts), "est_cost": 0.0})

        bench_eq = start_cash * bench_last / bench_base if bench_base and bench_last else start_cash
        equity_points.append({"ts": _iso(ts), "equity": round(equity, 2), "cash": round(broker.cash, 2),
                              "gross": round(equity + costs_paid, 2), "bench": round(bench_eq, 2)})

    if timeline:
        state["last_ts"] = _iso(timeline[-1])
    state["cash"] = broker.cash  # 丸めない(分割実行でも誤差を溜めないため)
    state["positions"] = broker.positions
    state["pending"] = pending
    state["last_close"] = last_close
    state["costs_paid"] = costs_paid
    state["bench_base"] = bench_base
    state["bench_last"] = bench_last
    return fills, equity_points


def _iso(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
