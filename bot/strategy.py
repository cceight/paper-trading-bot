"""売買ルール。

ルールは「各時点で、その銘柄を持ちたいか(True/False)」だけを返す。
注文の出し方やお金の計算はエンジン側の仕事なので、ルールを差し替えても他は変わらない。
"""
from __future__ import annotations

import pandas as pd


class SmaCross:
    """短期移動平均が長期移動平均より上なら保有、下なら手放す。"""

    def __init__(self, fast: int, slow: int):
        if fast >= slow:
            raise ValueError("fast は slow より小さくしてください")
        self.fast = fast
        self.slow = slow

    def signals(self, bars: pd.DataFrame) -> pd.Series:
        """各足の終値時点での「持ちたい」判定。過去データしか使わない。"""
        close = bars["close"]
        fast = close.rolling(self.fast).mean()
        slow = close.rolling(self.slow).mean()
        want = (fast > slow) & slow.notna()
        return want


def build(conf: dict):
    name = conf.get("name")
    if name == "sma_cross":
        return SmaCross(int(conf["fast"]), int(conf["slow"]))
    raise ValueError(f"未対応のルール: {name}")
