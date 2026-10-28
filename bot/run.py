"""ボットの入口。GitHub Actions から15分おきに呼ばれる。

使い方:
  python -m bot.run                 本番(ペーパー)の1回分を実行
  python -m bot.run --backtest      保存済みデータで最初からやり直した成績を表示(状態は変えない)
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import data as datamod
from .engine import initialize, new_state, processing_cutoff, step
from .strategy import build

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--provider", default="yahoo", choices=["yahoo", "synthetic"])
    ap.add_argument("--now", help="テスト用に現在時刻を指定 (ISO8601, UTC)")
    ap.add_argument("--backtest", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root)
    conf = json.loads((root / "config.json").read_text(encoding="utf-8"))
    now = pd.Timestamp(args.now) if args.now else pd.Timestamp(datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.tz_localize("UTC")

    if args.backtest:
        return backtest(root, conf)

    provider = datamod.SyntheticProvider() if args.provider == "synthetic" else datamod.YahooProvider()
    run_once(root, conf, provider, now)


def run_once(root: Path, conf: dict, provider, now: pd.Timestamp) -> dict:
    data_dir = root / "data" / "bars"
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    interval = int(conf["interval_minutes"])

    # 1) データ取得(失敗した銘柄があっても他は続ける)
    def fetch(sym):
        old = datamod.load_bars(data_dir, sym)
        gap_days = 60 if old.empty else (now - old.index.max()).days + 1
        try:
            new = provider.fetch(sym, interval, gap_days)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {sym} の取得に失敗: {e}")
            new = datamod.empty_frame()
        new = datamod.completed_only(new, interval, now.to_pydatetime())
        return datamod.merge_and_save(data_dir, sym, old, new)

    bars = {sym: fetch(sym) for sym in conf["symbols"]}
    bench_sym = conf.get("benchmark", "SPY")
    bench = bars[bench_sym] if bench_sym in bars else fetch(bench_sym)

    # 2) 状態を読み込み、売買を進める
    state_path = state_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() \
        else new_state(float(conf["starting_cash_usd"]))

    cutoff = processing_cutoff(bars)
    if cutoff is None:
        print("データがまだありません")
        return state

    strategy = build(conf["strategy"])
    if state["last_ts"] is None:
        # 初回は過去データで売買せず、今この瞬間からスタートする
        fills, points = [], [initialize(state, bars, bench, cutoff)]
        print(f"初回起動: {state['last_ts']} から運用開始")
    else:
        if "costs_paid" not in state:
            state = rebuild_history(root, conf, state, bars, bench, strategy)
        fills, points = step(state, bars, strategy, conf["costs"], float(conf["cash_buffer"]),
                             cutoff, bench)

    state["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    append_csv(state_dir / "trades.csv", fills, TRADE_FIELDS)
    append_csv(state_dir / "equity.csv", points, EQUITY_FIELDS)
    write_report(root, conf, state)
    print(f"処理: 約定 {len(fills)} 件 / 足 {len(points)} 本 / 処理済み {state['last_ts']}")
    return state


TRADE_FIELDS = ["ts", "decided_at", "side", "symbol", "qty", "price", "fee"]
EQUITY_FIELDS = ["ts", "equity", "cash", "gross", "bench"]


def rebuild_history(root: Path, conf: dict, old: dict, bars, bench, strategy) -> dict:
    """比較用の数値(gross / bench)が無い古い記録を、保存済みの5分足から最初から計算し直す。"""
    state = new_state(float(old["starting_cash"]))
    start = pd.Timestamp(old["started_at"])
    first = initialize(state, bars, bench, start)
    fills, points = step(state, bars, strategy, conf["costs"], float(conf["cash_buffer"]),
                         pd.Timestamp(old["last_ts"]), bench)
    old_trades = len(read_csv(root / "state" / "trades.csv"))
    print(f"記録を作り直しました: 約定 {old_trades} 件 → {len(fills)} 件 / 足 {len(points)} 本")
    for name, rows, fields in (("trades.csv", fills, TRADE_FIELDS),
                               ("equity.csv", [first] + points, EQUITY_FIELDS)):
        path = root / "state" / name
        path.unlink(missing_ok=True)
        append_csv(path, rows, fields)
    return state


def append_csv(path: Path, rows: list[dict], fields: list[str]):
    if not rows:
        return
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_report(root: Path, conf: dict, state: dict):
    """GitHub Pages の成績ページが読む JSON を書き出す。"""
    out = root / "docs" / "data"
    out.mkdir(parents=True, exist_ok=True)
    equity = read_csv(root / "state" / "equity.csv")
    trades = read_csv(root / "state" / "trades.csv")

    start = float(state["starting_cash"])
    last = equity[-1] if equity else {}
    last_equity = float(last.get("equity") or start)
    last_bench = float(last.get("bench") or start)
    positions = []
    for s, q in sorted(state["positions"].items()):
        price = state["last_close"].get(s, 0.0)
        positions.append({"symbol": s, "qty": q, "price": round(price, 2), "value": round(q * price, 2)})

    summary = {
        "updated_at": state.get("updated_at"),
        "started_at": state.get("started_at"),
        "last_bar": state.get("last_ts"),
        "starting_cash": state["starting_cash"],
        "equity": round(last_equity, 2),
        "cash": round(state["cash"], 2),
        "return_pct": round((last_equity / state["starting_cash"] - 1) * 100, 3),
        "trade_count": len(trades),
        "fees_paid": round(sum(float(t["fee"]) for t in trades), 2),
        "costs_paid": round(float(state.get("costs_paid", 0.0)), 2),
        "benchmark": conf.get("benchmark", "SPY"),
        "bench_equity": round(last_bench, 2),
        "vs_bench": round(last_equity - last_bench, 2),
        "positions": positions,
        "pending": [{k: o[k] for k in ("symbol", "side", "qty", "decided_at")} for o in state["pending"]],
        "symbols": conf["symbols"],
        "strategy": conf["strategy"],
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "equity.json").write_text(
        json.dumps([[e["ts"], float(e["equity"]), float(e.get("gross") or e["equity"]),
                     float(e.get("bench") or start)] for e in equity],
                   separators=(",", ":")), encoding="utf-8")
    (out / "trades.json").write_text(json.dumps(trades[-300:], ensure_ascii=False, indent=0), encoding="utf-8")


def backtest(root: Path, conf: dict):
    """保存済みの全データで、最初から運用した場合の成績を計算する(状態ファイルは触らない)。"""
    bars = {s: datamod.load_bars(root / "data" / "bars", s) for s in conf["symbols"]}
    cutoff = processing_cutoff(bars)
    if cutoff is None:
        print("データがありません")
        return
    bench_sym = conf.get("benchmark", "SPY")
    bench = bars.get(bench_sym)
    if bench is None:
        bench = datamod.load_bars(root / "data" / "bars", bench_sym)
    state = new_state(float(conf["starting_cash_usd"]))
    fills, points = step(state, bars, build(conf["strategy"]), conf["costs"],
                         float(conf["cash_buffer"]), cutoff, bench)
    if not points:
        print("処理できる足がありません")
        return
    start, end = points[0], points[-1]
    ret = (end["equity"] / state["starting_cash"] - 1) * 100
    print(f"期間: {start['ts']} 〜 {end['ts']} ({len(points)} 本)")
    print(f"資産: {state['starting_cash']:.2f} → {end['equity']:.2f} USD ({ret:+.2f}%)")
    print(f"コスト前: {end['gross']:.2f} USD / {bench_sym}放置: {end['bench']:.2f} USD")
    print(f"約定: {len(fills)} 件 / 手数料+価格ズレ {state['costs_paid']:.2f} USD")
    return state, fills, points


if __name__ == "__main__":
    main()
