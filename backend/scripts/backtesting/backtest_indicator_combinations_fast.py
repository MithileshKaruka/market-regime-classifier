#!/usr/bin/env python3
"""
Fast Backtest Agent Bias with Different Orderflow Indicator Combinations

Simplified version that tests orderflow component weights without
per-bar signal detection (which is slow).

Tests:
1. Component weights (Orderflow vs Trend vs Intensity)
2. Orderflow sub-weights (LDR, OBI, CVD)
3. Direction thresholds
"""
import argparse
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from app.data.storage import DuckDBStorage


@dataclass
class Config:
    name: str
    # Main component weights (sum to 100)
    of_weight: float = 60.0
    trend_weight: float = 20.0
    intensity_weight: float = 20.0
    # Orderflow sub-weights (sum to 100)
    ldr_weight: float = 33.0
    obi_weight: float = 33.0
    cvd_weight: float = 34.0
    # Thresholds
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
    """Pre-calculate all component scores for the entire dataframe"""

    # LDR score from DOM imbalance (0.3-0.7 -> 0-100)
    df = df.with_columns([
        ((pl.col("dom_imbalance").fill_null(0.5) - 0.3) / 0.4 * 100)
        .clip(0, 100)
        .alias("ldr_score")
    ])

    # OBI score (more aggressive scaling)
    df = df.with_columns([
        pl.when(pl.col("dom_imbalance") > 0.55)
        .then(60 + (pl.col("dom_imbalance") - 0.55) / 0.15 * 40)
        .when(pl.col("dom_imbalance") < 0.45)
        .then(40 - (0.45 - pl.col("dom_imbalance")) / 0.15 * 40)
        .otherwise(50)
        .clip(0, 100)
        .alias("obi_score")
    ])

    # CVD z-score -> score
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

    # Trend score from EMA crossover (rolling)
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

    # Intensity score from RVOL
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

    # Price change for direction
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

    # Forward returns for evaluation
    for fwd in [3, 5, 10]:
        df = df.with_columns([
            ((pl.col("close").shift(-fwd) - pl.col("close")) / pl.col("close") * 100)
            .alias(f"fwd_{fwd}")
        ])

    return df


def run_config(df: pl.DataFrame, cfg: Config, fwd_bars: int = 5) -> dict:
    """Run backtest for a single configuration"""

    # Calculate orderflow score
    of_score = (
        df["ldr_score"] * (cfg.ldr_weight / 100) +
        df["obi_score"] * (cfg.obi_weight / 100) +
        df["cvd_score"] * (cfg.cvd_weight / 100)
    )

    # Calculate final score with alignment modifiers (copy to allow modification)
    trend_score = df["trend_score"].to_numpy().copy()
    intensity_score = df["intensity_score"].to_numpy().copy()
    of_np = of_score.to_numpy().copy()

    # Apply alignment modifiers
    for i in range(len(of_np)):
        of_dir = "BULL" if of_np[i] > cfg.bullish_thresh else "BEAR" if of_np[i] < cfg.bearish_thresh else "NEUT"

        if of_dir != "NEUT":
            # Trend alignment
            t_dir = "BULL" if trend_score[i] > 55 else "BEAR" if trend_score[i] < 45 else "NEUT"
            if t_dir == of_dir:
                trend_score[i] = 50 + (trend_score[i] - 50) * 1.15
            elif t_dir != "NEUT":
                trend_score[i] = 50 + (trend_score[i] - 50) * 0.85

            # Intensity alignment
            i_dir = "BULL" if intensity_score[i] > 55 else "BEAR" if intensity_score[i] < 45 else "NEUT"
            if i_dir == of_dir:
                intensity_score[i] = 50 + (intensity_score[i] - 50) * 1.2
            elif i_dir != "NEUT":
                intensity_score[i] = 50 + (intensity_score[i] - 50) * 0.8

    trend_score = np.clip(trend_score, 0, 100)
    intensity_score = np.clip(intensity_score, 0, 100)

    # Final score
    final_score = (
        of_np * (cfg.of_weight / 100) +
        trend_score * (cfg.trend_weight / 100) +
        intensity_score * (cfg.intensity_weight / 100)
    )

    # Get forward returns
    fwd_col = f"fwd_{fwd_bars}"
    fwd_returns = df[fwd_col].to_numpy()

    # Classify predictions
    bullish_mask = final_score > cfg.bullish_thresh
    bearish_mask = final_score < cfg.bearish_thresh
    neutral_mask = ~bullish_mask & ~bearish_mask

    # Remove NaN forward returns
    valid_mask = ~np.isnan(fwd_returns)

    # Calculate hit rates
    bull_correct = np.sum((bullish_mask & valid_mask) & (fwd_returns > 0))
    bull_total = np.sum(bullish_mask & valid_mask)

    bear_correct = np.sum((bearish_mask & valid_mask) & (fwd_returns < 0))
    bear_total = np.sum(bearish_mask & valid_mask)

    bull_hit = (bull_correct / bull_total * 100) if bull_total > 0 else 0
    bear_hit = (bear_correct / bear_total * 100) if bear_total > 0 else 0
    overall_hit = ((bull_correct + bear_correct) / (bull_total + bear_total) * 100) if (bull_total + bear_total) > 0 else 0

    # Profit factor
    bull_returns = fwd_returns[bullish_mask & valid_mask]
    bear_returns = -fwd_returns[bearish_mask & valid_mask]  # Invert for shorts
    all_returns = np.concatenate([bull_returns, bear_returns]) if len(bull_returns) + len(bear_returns) > 0 else np.array([0])

    wins = np.sum(all_returns[all_returns > 0])
    losses = abs(np.sum(all_returns[all_returns < 0]))
    pf = (wins / losses) if losses > 0 else (float('inf') if wins > 0 else 0)

    # High conviction (score > 70 or < 30)
    hc_bull = final_score > 70
    hc_bear = final_score < 30
    hc_correct = np.sum((hc_bull & valid_mask) & (fwd_returns > 0)) + np.sum((hc_bear & valid_mask) & (fwd_returns < 0))
    hc_total = np.sum((hc_bull | hc_bear) & valid_mask)
    hc_hit = (hc_correct / hc_total * 100) if hc_total > 0 else 0

    # Correlation
    valid_scores = final_score[valid_mask]
    valid_fwd = fwd_returns[valid_mask]
    corr = np.corrcoef(valid_scores, valid_fwd)[0, 1] if len(valid_scores) > 10 else 0

    return {
        "name": cfg.name,
        "bull_hit": bull_hit,
        "bear_hit": bear_hit,
        "overall_hit": overall_hit,
        "pf": pf,
        "hc_hit": hc_hit,
        "corr": corr,
        "bull_n": int(bull_total),
        "bear_n": int(bear_total),
        "neut_n": int(np.sum(neutral_mask & valid_mask)),
        "avg_bull_ret": float(np.mean(bull_returns)) if len(bull_returns) > 0 else 0,
        "avg_bear_ret": float(np.mean(bear_returns)) if len(bear_returns) > 0 else 0,
    }


def get_configs() -> List[Config]:
    """Generate test configurations"""
    return [
        # Current production
        Config("PROD_60_20_20", of_weight=60, trend_weight=20, intensity_weight=20),

        # Orderflow variations
        Config("OF_80", of_weight=80, trend_weight=10, intensity_weight=10),
        Config("OF_70", of_weight=70, trend_weight=15, intensity_weight=15),
        Config("OF_50", of_weight=50, trend_weight=25, intensity_weight=25),
        Config("OF_40", of_weight=40, trend_weight=30, intensity_weight=30),
        Config("OF_30", of_weight=30, trend_weight=35, intensity_weight=35),
        Config("OF_20", of_weight=20, trend_weight=40, intensity_weight=40),

        # Equal weights
        Config("EQUAL_33", of_weight=34, trend_weight=33, intensity_weight=33),

        # Trend focused (higher trend weights)
        Config("TREND_60", of_weight=20, trend_weight=60, intensity_weight=20),
        Config("TREND_50", of_weight=30, trend_weight=50, intensity_weight=20),
        Config("TREND_50_INT30", of_weight=20, trend_weight=50, intensity_weight=30),
        Config("TREND_40", of_weight=40, trend_weight=40, intensity_weight=20),
        Config("TREND_40_INT30", of_weight=30, trend_weight=40, intensity_weight=30),

        # Intensity focused (higher intensity weights)
        Config("INT_60", of_weight=20, trend_weight=20, intensity_weight=60),
        Config("INT_50", of_weight=25, trend_weight=25, intensity_weight=50),
        Config("INT_50_TR30", of_weight=20, trend_weight=30, intensity_weight=50),
        Config("INT_40", of_weight=40, trend_weight=20, intensity_weight=40),
        Config("INT_40_TR30", of_weight=30, trend_weight=30, intensity_weight=40),

        # Trend + Intensity focused (low orderflow)
        Config("TR_INT_45_45", of_weight=10, trend_weight=45, intensity_weight=45),
        Config("TR_INT_40_40", of_weight=20, trend_weight=40, intensity_weight=40),
        Config("TR60_INT30", of_weight=10, trend_weight=60, intensity_weight=30),
        Config("TR30_INT60", of_weight=10, trend_weight=30, intensity_weight=60),

        # Orderflow sub-weight variations
        Config("CVD_ONLY", of_weight=60, trend_weight=20, intensity_weight=20,
               ldr_weight=0, obi_weight=0, cvd_weight=100),
        Config("LDR_ONLY", of_weight=60, trend_weight=20, intensity_weight=20,
               ldr_weight=100, obi_weight=0, cvd_weight=0),
        Config("OBI_ONLY", of_weight=60, trend_weight=20, intensity_weight=20,
               ldr_weight=0, obi_weight=100, cvd_weight=0),
        Config("LDR_CVD", of_weight=60, trend_weight=20, intensity_weight=20,
               ldr_weight=50, obi_weight=0, cvd_weight=50),
        Config("OBI_CVD", of_weight=60, trend_weight=20, intensity_weight=20,
               ldr_weight=0, obi_weight=50, cvd_weight=50),
        Config("LDR_OBI", of_weight=60, trend_weight=20, intensity_weight=20,
               ldr_weight=50, obi_weight=50, cvd_weight=0),

        # Threshold variations
        Config("THRESH_60_40", of_weight=60, trend_weight=20, intensity_weight=20,
               bullish_thresh=60, bearish_thresh=40),
        Config("THRESH_52_48", of_weight=60, trend_weight=20, intensity_weight=20,
               bullish_thresh=52, bearish_thresh=48),
        Config("THRESH_58_42", of_weight=60, trend_weight=20, intensity_weight=20,
               bullish_thresh=58, bearish_thresh=42),

        # Trend-focused with thresholds
        Config("TREND50_TH58", of_weight=30, trend_weight=50, intensity_weight=20,
               bullish_thresh=58, bearish_thresh=42),
        Config("TREND40_TH58", of_weight=40, trend_weight=40, intensity_weight=20,
               bullish_thresh=58, bearish_thresh=42),

        # Intensity-focused with thresholds
        Config("INT50_TH58", of_weight=25, trend_weight=25, intensity_weight=50,
               bullish_thresh=58, bearish_thresh=42),
        Config("INT40_TH58", of_weight=40, trend_weight=20, intensity_weight=40,
               bullish_thresh=58, bearish_thresh=42),

        # Combined optimizations with CVD
        Config("CVD_HIGH_OF", of_weight=70, trend_weight=15, intensity_weight=15,
               ldr_weight=20, obi_weight=20, cvd_weight=60),
        Config("CVD_TREND", of_weight=40, trend_weight=40, intensity_weight=20,
               ldr_weight=20, obi_weight=20, cvd_weight=60),
        Config("CVD_INT", of_weight=40, trend_weight=20, intensity_weight=40,
               ldr_weight=20, obi_weight=20, cvd_weight=60),

        # LDR-focused combinations
        Config("LDR_HIGH_OF", of_weight=70, trend_weight=15, intensity_weight=15,
               ldr_weight=60, obi_weight=20, cvd_weight=20),
        Config("LDR_TREND", of_weight=40, trend_weight=40, intensity_weight=20,
               ldr_weight=60, obi_weight=20, cvd_weight=20),
        Config("LDR_INT", of_weight=40, trend_weight=20, intensity_weight=40,
               ldr_weight=60, obi_weight=20, cvd_weight=20),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--timeframe", default="15M", choices=["5M", "15M", "1H", "4H", "1D"])
    parser.add_argument("-l", "--limit", type=int, default=5000)
    parser.add_argument("-f", "--forward", type=int, default=5)
    parser.add_argument("-a", "--all-tf", action="store_true", help="Test all timeframes")
    args = parser.parse_args()

    timeframes = ["5M", "15M", "1H", "4H"] if args.all_tf else [args.timeframe]
    configs = get_configs()

    print(f"Testing {len(configs)} configurations")
    print(f"Timeframes: {timeframes}")
    print(f"Forward bars: {args.forward}")

    for tf in timeframes:
        print(f"\n{'='*100}")
        print(f"TIMEFRAME: {tf}")
        print(f"{'='*100}")

        df = load_data(tf, args.limit)
        print(f"Loaded {len(df)} bars: {df[0, 'timestamp']} to {df[-1, 'timestamp']}")

        df = calc_scores(df)

        results = []
        for cfg in configs:
            r = run_config(df, cfg, args.forward)
            results.append(r)

        # Sort by overall hit rate
        results.sort(key=lambda x: x["overall_hit"], reverse=True)

        print(f"\n{'Config':<20} {'Bull%':>7} {'Bear%':>7} {'All%':>7} {'PF':>7} {'HC%':>7} {'Corr':>7} {'B#':>5} {'S#':>5} {'N#':>5}")
        print("-" * 100)
        for r in results:
            pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "inf"
            print(f"{r['name']:<20} {r['bull_hit']:>6.1f}% {r['bear_hit']:>6.1f}% {r['overall_hit']:>6.1f}% {pf_str:>7} {r['hc_hit']:>6.1f}% {r['corr']:>7.3f} {r['bull_n']:>5} {r['bear_n']:>5} {r['neut_n']:>5}")

        # Top performers
        print(f"\nTOP PERFORMERS ({tf}):")
        best_hit = max(results, key=lambda x: x["overall_hit"])
        best_pf = max(results, key=lambda x: x["pf"] if x["pf"] < 100 else 0)
        best_hc = max(results, key=lambda x: x["hc_hit"])
        best_corr = max(results, key=lambda x: x["corr"])

        print(f"  Best Overall: {best_hit['name']} ({best_hit['overall_hit']:.1f}%)")
        print(f"  Best PF:      {best_pf['name']} (PF {best_pf['pf']:.2f})")
        print(f"  Best HC:      {best_hc['name']} ({best_hc['hc_hit']:.1f}%)")
        print(f"  Best Corr:    {best_corr['name']} ({best_corr['corr']:.3f})")


if __name__ == "__main__":
    main()
