"""起動タイミングがバラバラでも結果が同じになることを確かめるテスト。

  python -m tests.test_engine
"""
import json
import random
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from bot import run as runmod
from bot.data import SyntheticProvider

ROOT = Path(__file__).resolve().parent.parent


def fresh_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    shutil.copy(ROOT / "config.json", d / "config.json")
    return d


def simulate(times) -> Path:
    d = fresh_dir()
    conf = json.loads((d / "config.json").read_text())
    provider = SyntheticProvider(seed=7, days=15)
    for t in times:
        runmod.run_once(d, conf, provider, pd.Timestamp(t))
    return d


def main():
    start = pd.Timestamp("2026-09-01T15:00:00Z")
    end = pd.Timestamp("2026-09-19T21:00:00Z")

    # A: 開始後、最後に1回だけまとめて処理
    a = simulate([start, end])

    # B: 13〜27分のバラバラな間隔で何百回も起動(遅延・取りこぼし込み)
    rng = random.Random(3)
    times, t = [start], start
    while t < end:
        t += pd.Timedelta(minutes=rng.randint(13, 27))
        times.append(min(t, end))
    b = simulate(times)

    ta = (a / "state" / "trades.csv").read_text()
    tb = (b / "state" / "trades.csv").read_text()
    ea = (a / "state" / "equity.csv").read_text()
    eb = (b / "state" / "equity.csv").read_text()
    sa = json.loads((a / "state" / "state.json").read_text())
    sb = json.loads((b / "state" / "state.json").read_text())

    assert ta == tb, "約定履歴が一致しません"
    assert ea == eb, "資産推移が一致しません"
    for k in ("cash", "positions", "pending", "last_ts"):
        assert sa[k] == sb[k], f"{k} が一致しません"

    n_trades = len(ta.strip().splitlines()) - 1
    summary = json.loads((b / "docs" / "data" / "summary.json").read_text())
    print(f"OK: 起動 {len(times)} 回 vs 2 回で完全一致 / 約定 {n_trades} 件 / "
          f"資産 {summary['equity']} USD ({summary['return_pct']:+}%) / 手数料 {summary['fees_paid']} USD")
    assert n_trades > 0, "売買が1回も起きていません"

    # 資金がマイナスになっていないか
    eq = pd.read_csv(b / "state" / "equity.csv")
    assert (eq["cash"] >= -1e-6).all(), "現金がマイナスになりました"
    print("OK: 現金がマイナスになることはありませんでした")


if __name__ == "__main__":
    main()
