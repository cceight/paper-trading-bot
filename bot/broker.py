"""注文を受けて約定させる部分。

いまは PaperBroker(仮想口座)だけ。将来インタラクティブ・ブローカーズ等に移るときは、
同じメソッド(buy / sell / position / cash)を持つクラスを作って差し替える。
"""
from __future__ import annotations

import math


class PaperBroker:
    def __init__(self, cash: float, costs: dict, positions: dict | None = None):
        self.cash = float(cash)
        self.positions: dict[str, int] = dict(positions or {})
        self.per_share = float(costs.get("commission_per_share", 0.005))
        self.min_fee = float(costs.get("commission_min", 1.0))
        self.slip = float(costs.get("slippage_bps", 5)) / 10000

    # --- 手数料と価格ズレ -------------------------------------------------
    def fee(self, qty: int) -> float:
        return round(max(self.min_fee, qty * self.per_share), 4)

    def buy_price(self, ref: float) -> float:
        return ref * (1 + self.slip)

    def sell_price(self, ref: float) -> float:
        return ref * (1 - self.slip)

    # --- 注文 -------------------------------------------------------------
    def max_affordable(self, ref: float, budget: float) -> int:
        price = self.buy_price(ref)
        qty = int(math.floor(budget / price))
        while qty > 0 and qty * price + self.fee(qty) > min(budget, self.cash):
            qty -= 1
        return max(qty, 0)

    def buy(self, symbol: str, qty: int, ref: float) -> dict | None:
        price = self.buy_price(ref)
        # 約定時点で資金が足りなければ株数を減らす(本番でも起きうる)
        while qty > 0 and qty * price + self.fee(qty) > self.cash:
            qty -= 1
        if qty <= 0:
            return None
        fee = self.fee(qty)
        self.cash -= qty * price + fee
        self.positions[symbol] = self.positions.get(symbol, 0) + qty
        return {"side": "BUY", "symbol": symbol, "qty": qty, "price": round(price, 4), "fee": fee,
                "slip": qty * (price - ref)}

    def sell(self, symbol: str, qty: int, ref: float) -> dict | None:
        qty = min(qty, self.positions.get(symbol, 0))
        if qty <= 0:
            return None
        price = self.sell_price(ref)
        fee = self.fee(qty)
        self.cash += qty * price - fee
        left = self.positions.get(symbol, 0) - qty
        if left:
            self.positions[symbol] = left
        else:
            self.positions.pop(symbol, None)
        return {"side": "SELL", "symbol": symbol, "qty": qty, "price": round(price, 4), "fee": fee,
                "slip": qty * (ref - price)}

    def position(self, symbol: str) -> int:
        return self.positions.get(symbol, 0)
