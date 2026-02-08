#!/usr/bin/env python3
"""
Grid Search for Agent Bias Configurations

Exhaustively tests combinations of:
- Main component weights (OF, Trend, Intensity)
- Orderflow sub-weights (LDR, OBI, CVD)
- Direction thresholds
"""
import argparse
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple
from itertools import product
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from app.data.storage import DuckDBStorage


@dataclass
class Config:
    name: str
    of_weight: float = 60.0
    trend_weight: float = 20.0
    intensity_weight: float = 20.0
    ldr_weight: float = 33.0
    obi_weight: float = 33.0
    cvd_weight: float = 34.0
    bullish_thresh: float = 55.0
    bearish_thresh: float = 45.0


def load_data(tf: str, limit: int = 3000) -> pl.DataFrame:
    with DuckDBStorage() as storage:
        df = storage.conn.execute(f"""
            SELECT * FROM (
                SELECT timestamp, open, high, low, close, volume,
                       dom_imbalance, cvd, instant_delta
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
                ORDER BY timestamp DESC
                LIMIT {limit}
            ) ORDER BY timestamp ASC
        """).pl()
    return df


def calc_scores(df: pl.DataFrame) -> pl.DataFrame:
    """Pre-calculate all component scores"""

    # LDR score
    df = df.with_columns([
        ((pl.col("dom_imbalance").fill_null(0.5) - 0.3) / 0.4 * 100)
        .clip(0, 100)
        .alias("ldr_score")
    ])

    # OBI score
    df = df.with_columns([
        pl.when(pl.col("dom_imbalance") > 0.55)
        .then(60 + (pl.col("dom_imbalance") - 0.55) / 0.15 * 40)
        .when(pl.col("dom_imbalance") < 0.45)
        .then(40 - (0.45 - pl.col("dom_imbalance")) / 0.15 * 40)
        .otherwise(50)
        .clip(0, 100)
        .alias("obi_score")
    ])

    # CVD score
    cvd_mean = df["cvd"].mean()
    cvd_std = df["cvd"].std()
    if cvd_std and cvd_std > 0:
        df = df.with_columns([
            (50 + ((pl.col("cvd").fill_null(0) - cvd_mean) / cvd_std) * 17.5)
            .clip(0, 100)
            .alias("cvd_score")
        ])
    else:
        df = df.with_columns([pl.lit(50.0).alias("cvd_score")])

    # Trend score
    df = df.with_columns([
        pl.col("close").rolling_mean(window_size=5).alias("ema_fast"),
        pl.col("close").rolling_mean(window_size=12).alias("ema_slow"),
    ])
    df = df.with_columns([
        pl.when(pl.col("ema_fast") > pl.col("ema_slow") * 1.001)
        .then(pl.when(pl.col("close") > pl.col("ema_fast")).then(80).otherwise(65))
        .when(pl.col("ema_fast") < pl.col("ema_slow") * 0.999)
        .then(pl.when(pl.col("close") < pl.col("ema_fast")).then(20).otherwise(35))
        .otherwise(50)
        .alias("trend_score")
    ])

    # Intensity score
    vol_ma = df["volume"].mean()
    df = df.with_columns([
        (pl.col("volume") / vol_ma).alias("rvol")
    ])
    df = df.with_columns([
        pl.when(pl.col("rvol") >= 2.0).then(80)
        .when(pl.col("rvol") >= 1.5).then(70)
        .when(pl.col("rvol") >= 1.0).then(55)
        .when(pl.col("rvol") >= 0.5).then(40)
        .otherwise(30)
        .alias("intensity_base")
    ])
    df = df.with_columns([
        ((pl.col("close") - pl.col("close").shift(1)) / pl.col("close").shift(1))
        .alias("price_change")
    ])
    df = df.with_columns([
        pl.when(pl.col("price_change") > 0.001)
        .then(pl.col("intensity_base") + 10)
        .when(pl.col("price_change") < -0.001)
        .then(100 - pl.col("intensity_base"))
        .otherwise(50)
        .clip(0, 100)
        .alias("intensity_score")
    ])

    # Forward returns
    for fwd in [3, 5, 10]:
        df = df.with_columns([
            ((pl.col("close").shift(-fwd) - pl.col("close")) / pl.col("close") * 100)
            .alias(f"fwd_{fwd}")
        ])

    return df


def run_config(df: pl.DataFrame, cfg: Config, fwd_bars: int = 5) -> dict:
    """Run backtest for a single configuration"""

    of_score = (
        df["ldr_score"] * (cfg.ldr_weight / 100) +
        df["obi_score"] * (cfg.obi_weight / 100) +
        df["cvd_score"] * (cfg.cvd_weight / 100)
    )

    trend_score = df["trend_score"].to_numpy().copy()
    intensity_score = df["intensity_score"].to_numpy().copy()
    of_np = of_score.to_numpy().copy()

    # Apply alignment modifiers
    for i in range(len(of_np)):
        of_dir = "BULL" if of_np[i] > cfg.bullish_thresh else "BEAR" if of_np[i] < cfg.bearish_thresh else "NEUT"
        if of_dir != "NEUT":
            t_dir = "BULL" if trend_score[i] > 55 else "BEAR" if trend_score[i] < 45 else "NEUT"
            if t_dir == of_dir:
                trend_score[i] = 50 + (trend_score[i] - 50) * 1.15
            elif t_dir != "NEUT":
                trend_score[i] = 50 + (trend_score[i] - 50) * 0.85
            i_dir = "BULL" if intensity_score[i] > 55 else "BEAR" if intensity_score[i] < 45 else "NEUT"
            if i_dir == of_dir:
                intensity_score[i] = 50 + (intensity_score[i] - 50) * 1.2
            elif i_dir != "NEUT":
                intensity_score[i] = 50 + (intensity_score[i] - 50) * 0.8

    trend_score = np.clip(trend_score, 0, 100)
    intensity_score = np.clip(intensity_score, 0, 100)

    final_score = (
        of_np * (cfg.of_weight / 100) +
        trend_score * (cfg.trend_weight / 100) +
        intensity_score * (cfg.intensity_weight / 100)
    )

    fwd_col = f"fwd_{fwd_bars}"
    fwd_returns = df[fwd_col].to_numpy()

    bullish_mask = final_score > cfg.bullish_thresh
    bearish_mask = final_score < cfg.bearish_thresh
    valid_mask = ~np.isnan(fwd_returns)

    bull_correct = np.sum((bullish_mask & valid_mask) & (fwd_returns > 0))
    bull_total = np.sum(bullish_mask & valid_mask)
    bear_correct = np.sum((bearish_mask & valid_mask) & (fwd_returns < 0))
    bear_total = np.sum(bearish_mask & valid_mask)

    overall_hit = ((bull_correct + bear_correct) / (bull_total + bear_total) * 100) if (bull_total + bear_total) > 0 else 0

    bull_returns = fwd_returns[bullish_mask & valid_mask]
    bear_returns = -fwd_returns[bearish_mask & valid_mask]
    all_returns = np.concatenate([bull_returns, bear_returns]) if len(bull_returns) + len(bear_returns) > 0 else np.array([0])
    wins = np.sum(all_returns[all_returns > 0])
    losses = abs(np.sum(all_returns[all_returns < 0]))
    pf = (wins / losses) if losses > 0 else (float('inf') if wins > 0 else 0)

    return {
        "name": cfg.name,
        "overall_hit": overall_hit,
        "pf": pf,
        "bull_n": int(bull_total),
        "bear_n": int(bear_total),
        "of_weight": cfg.of_weight,
        "trend_weight": cfg.trend_weight,
        "intensity_weight": cfg.intensity_weight,
        "ldr_weight": cfg.ldr_weight,
        "obi_weight": cfg.obi_weight,
        "cvd_weight": cfg.cvd_weight,
        "bullish_thresh": cfg.bullish_thresh,
        "bearish_thresh": cfg.bearish_thresh,
    }


def generate_grid_configs() -> List[Config]:
    """Generate all combinations to test"""
    configs = []

    # Main weight combinations (must sum to 100)
    # Test in steps of 10
    main_weights = []
    for of in range(0, 101, 10):
        for tr in range(0, 101 - of, 10):
            int_w = 100 - of - tr
            main_weights.append((of, tr, int_w))

    # Orderflow sub-weights (must sum to 100)
    # Test key combinations
    of_sub_weights = [
        (33, 33, 34),  # Equal
        (50, 25, 25),  # LDR heavy
        (25, 50, 25),  # OBI heavy
        (25, 25, 50),  # CVD heavy
        (60, 20, 20),  # LDR dominant
        (20, 60, 20),  # OBI dominant
        (20, 20, 60),  # CVD dominant
        (50, 50, 0),   # LDR+OBI
        (50, 0, 50),   # LDR+CVD
        (0, 50, 50),   # OBI+CVD
        (100, 0, 0),   # LDR only
        (0, 100, 0),   # OBI only
        (0, 0, 100),   # CVD only
    ]

    # Threshold combinations
    thresholds = [
        (55, 45),  # Default
        (56, 44),
        (57, 43),
        (58, 42),
        (59, 41),
        (60, 40),
    ]

    idx = 0
    for of_w, tr_w, int_w in main_weights:
        for ldr, obi, cvd in of_sub_weights:
            for bull_th, bear_th in thresholds:
                name = f"C{idx}"
                configs.append(Config(
                    name=name,
                    of_weight=of_w,
                    trend_weight=tr_w,
                    intensity_weight=int_w,
                    ldr_weight=ldr,
                    obi_weight=obi,
                    cvd_weight=cvd,
                    bullish_thresh=bull_th,
                    bearish_thresh=bear_th,
                ))
                idx += 1

    return configs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--timeframe", default="15M", choices=["5M", "15M", "1H", "4H", "1D"])
    parser.add_argument("-l", "--limit", type=int, default=5000)
    parser.add_argument("-f", "--forward", type=int, default=5)
    parser.add_argument("--top", type=int, default=20, help="Show top N results")
    args = parser.parse_args()

    configs = generate_grid_configs()
    print(f"Testing {len(configs)} configurations on {args.timeframe}")
    print(f"Forward bars: {args.forward}")

    df = load_data(args.timeframe, args.limit)
    print(f"Loaded {len(df)} bars: {df[0, 'timestamp']} to {df[-1, 'timestamp']}")

    df = calc_scores(df)

    results = []
    for i, cfg in enumerate(configs):
        if i % 500 == 0:
            print(f"Progress: {i}/{len(configs)}...")
        r = run_config(df, cfg, args.forward)
        results.append(r)

    # Sort by overall hit rate
    results.sort(key=lambda x: x["overall_hit"], reverse=True)

    print(f"\n{'='*120}")
    print(f"TOP {args.top} CONFIGURATIONS BY HIT RATE ({args.timeframe})")
    print(f"{'='*120}")
    print(f"{'Rank':<5} {'Hit%':>7} {'PF':>7} {'OF':>4} {'TR':>4} {'INT':>4} {'LDR':>4} {'OBI':>4} {'CVD':>4} {'TH':>7} {'B#':>6} {'S#':>6}")
    print("-" * 120)

    for i, r in enumerate(results[:args.top]):
        pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "inf"
        th_str = f"{int(r['bullish_thresh'])}/{int(r['bearish_thresh'])}"
        print(f"{i+1:<5} {r['overall_hit']:>6.1f}% {pf_str:>7} {int(r['of_weight']):>4} {int(r['trend_weight']):>4} {int(r['intensity_weight']):>4} "
              f"{int(r['ldr_weight']):>4} {int(r['obi_weight']):>4} {int(r['cvd_weight']):>4} {th_str:>7} {r['bull_n']:>6} {r['bear_n']:>6}")

    # Sort by profit factor (exclude infinite)
    pf_results = [r for r in results if r['pf'] < 100 and r['pf'] > 0]
    pf_results.sort(key=lambda x: x["pf"], reverse=True)

    print(f"\n{'='*120}")
    print(f"TOP {args.top} CONFIGURATIONS BY PROFIT FACTOR ({args.timeframe})")
    print(f"{'='*120}")
    print(f"{'Rank':<5} {'PF':>7} {'Hit%':>7} {'OF':>4} {'TR':>4} {'INT':>4} {'LDR':>4} {'OBI':>4} {'CVD':>4} {'TH':>7} {'B#':>6} {'S#':>6}")
    print("-" * 120)

    for i, r in enumerate(pf_results[:args.top]):
        th_str = f"{int(r['bullish_thresh'])}/{int(r['bearish_thresh'])}"
        print(f"{i+1:<5} {r['pf']:>7.2f} {r['overall_hit']:>6.1f}% {int(r['of_weight']):>4} {int(r['trend_weight']):>4} {int(r['intensity_weight']):>4} "
              f"{int(r['ldr_weight']):>4} {int(r['obi_weight']):>4} {int(r['cvd_weight']):>4} {th_str:>7} {r['bull_n']:>6} {r['bear_n']:>6}")

    # Best overall (combined score: hit_rate * pf)
    for r in results:
        r['combined'] = r['overall_hit'] * min(r['pf'], 2.0)  # Cap PF contribution at 2
    results.sort(key=lambda x: x['combined'], reverse=True)

    print(f"\n{'='*120}")
    print(f"TOP {args.top} CONFIGURATIONS BY COMBINED SCORE (HitRate * min(PF, 2)) ({args.timeframe})")
    print(f"{'='*120}")
    print(f"{'Rank':<5} {'Score':>7} {'Hit%':>7} {'PF':>7} {'OF':>4} {'TR':>4} {'INT':>4} {'LDR':>4} {'OBI':>4} {'CVD':>4} {'TH':>7}")
    print("-" * 120)

    for i, r in enumerate(results[:args.top]):
        pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "inf"
        th_str = f"{int(r['bullish_thresh'])}/{int(r['bearish_thresh'])}"
        print(f"{i+1:<5} {r['combined']:>7.1f} {r['overall_hit']:>6.1f}% {pf_str:>7} {int(r['of_weight']):>4} {int(r['trend_weight']):>4} {int(r['intensity_weight']):>4} "
              f"{int(r['ldr_weight']):>4} {int(r['obi_weight']):>4} {int(r['cvd_weight']):>4} {th_str:>7}")


if __name__ == "__main__":
    main()
