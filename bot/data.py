"""株価データの取得と保存。

取得元(Provider)は差し替え可能にしてある。
- YahooProvider : 本番用(無料・非公式。仕様変更で壊れる可能性あり)
- SyntheticProvider : テスト用の合成データ
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]


def empty_frame() -> pd.DataFrame:
    df = pd.DataFrame(columns=COLUMNS, dtype="float64")
    df.index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return df


class YahooProvider:
    """Yahoo Finance から5分足を取る。通常取引時間のみ。"""

    def fetch(self, symbol: str, interval_minutes: int, days: int) -> pd.DataFrame:
        import yfinance as yf

        period = "60d" if days > 5 else "5d"
        raw = yf.Ticker(symbol).history(
            period=period,
            interval=f"{interval_minutes}m",
            prepost=False,
            auto_adjust=False,
            actions=False,
        )
        if raw is None or raw.empty:
            return empty_frame()
        df = raw.rename(columns=str.lower)[COLUMNS].astype("float64")
        df.index = pd.DatetimeIndex(df.index).tz_convert("UTC")
        df.index.name = "ts"
        return df.dropna()


class SyntheticProvider:
    """テスト用。米国市場時間(UTC 14:30〜21:00相当)のランダムウォーク。"""

    def __init__(self, seed: int = 1, start: datetime | None = None, days: int = 20):
        self.seed = seed
        self.start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.days = days

    def fetch(self, symbol: str, interval_minutes: int, days: int) -> pd.DataFrame:
        rng = random.Random(f"{self.seed}-{symbol}")
        price = 100 + rng.random() * 100
        rows, idx = [], []
        d = self.start
        made = 0
        while made < self.days:
            if d.weekday() < 5:
                t = d.replace(hour=14, minute=30)
                end = d.replace(hour=21, minute=0)
                while t < end:
                    o = price
                    c = o * math.exp(rng.gauss(0.0001, 0.003))
                    rows.append([o, max(o, c) * 1.001, min(o, c) * 0.999, c, rng.randint(1000, 9000)])
                    idx.append(t)
                    price = c
                    t += timedelta(minutes=interval_minutes)
                made += 1
            d += timedelta(days=1)
        df = pd.DataFrame(rows, columns=COLUMNS, index=pd.DatetimeIndex(idx, name="ts"))
        return df.astype("float64")


def load_bars(data_dir: Path, symbol: str) -> pd.DataFrame:
    path = data_dir / f"{symbol}.csv"
    if not path.exists():
        return empty_frame()
    df = pd.read_csv(path, index_col="ts")
    df.index = pd.to_datetime(df.index, utc=True)
    return df[COLUMNS].astype("float64")


def merge_and_save(data_dir: Path, symbol: str, old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """新しい足を足して保存。同じ時刻は新しい方で上書き(取りこぼし・修正の吸収)。"""
    frames = [f for f in (old, new) if not f.empty]
    if not frames:
        return empty_frame()
    merged = pd.concat(frames)
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    data_dir.mkdir(parents=True, exist_ok=True)
    out = merged.copy()
    out.index = out.index.strftime("%Y-%m-%dT%H:%M:%SZ")
    out.index.name = "ts"
    out.round(4).to_csv(data_dir / f"{symbol}.csv")
    return merged


def completed_only(df: pd.DataFrame, interval_minutes: int, now: datetime) -> pd.DataFrame:
    """まだ形成途中の足を除く(足の終わり時刻が現在より後のもの)。"""
    if df.empty:
        return df
    ends = df.index + pd.Timedelta(minutes=interval_minutes)
    return df[ends <= pd.Timestamp(now)]
