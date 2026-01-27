"""Agent Configuration Loader

Loads and caches agent configuration from YAML file.
Provides typed access to all configuration parameters.
"""
import os
import yaml
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Default config path - now in the same directory
CONFIG_PATH = Path(__file__).parent / "agent_config.yaml"


@dataclass
class ScoringConfig:
    """Bias scoring weights"""
    trend_structure_weight: int = 20
    market_intensity_weight: int = 30
    orderflow_alpha_weight: int = 50


@dataclass
class ThresholdsConfig:
    """Score threshold boundaries"""
    high_bearish_max: int = 30
    weak_bearish_max: int = 45
    neutral_max: int = 55
    weak_bullish_max: int = 70


@dataclass
class TrendStructureConfig:
    """Trend & Structure parameters"""
    ema_fast: int = 12
    ema_slow: int = 25
    swing_lookback: int = 5
    structure_lookback: int = 20  # Bars for market structure analysis
    sr_proximity_pct: float = 0.5


@dataclass
class IndicatorsConfig:
    """Technical indicators parameters"""
    # RVWAP periods
    rvwap_periods: List[int] = field(default_factory=lambda: [7, 30, 90, 200])
    # EMA periods
    ema_periods: List[int] = field(default_factory=lambda: [20, 50, 100, 200])
    # Bollinger Bands
    bb_period: int = 20
    bb_std: float = 2.0
    # ATR
    atr_period: int = 14


@dataclass
class InstrumentConfig:
    """Instrument-specific parameters"""
    symbol: str = "MNQ"
    tick_size: float = 0.25
    min_price: float = 18000  # Price filter minimum
    max_price: float = 32000  # Price filter maximum


@dataclass
class StreamingConfig:
    """Live streaming parameters"""
    dataset: str = "GLBX.MDP3"  # Databento dataset
    default_symbols: List[str] = field(default_factory=lambda: ["MNQ"])
    default_timeframes: List[str] = field(default_factory=lambda: ["5M", "15M", "1H", "4H", "1D"])
    flush_interval_seconds: float = 1.0  # How often to flush to database
    dom_smoothing_factor: float = 0.9  # EMA factor for DOM updates


@dataclass
class MarketIntensityConfig:
    """Market Intensity parameters"""
    rvol_lookback: int = 20
    rvol_high: float = 1.5
    rvol_low: float = 0.5
    vpin_buckets: int = 50
    vpin_num_buckets: int = 50  # Number of buckets for rolling VPIN
    vpin_elevated: float = 0.5
    vpin_alert: float = 0.7
    poc_lookback: int = 100  # Bars for POC calculation


@dataclass
class OrderflowAlphaConfig:
    """Orderflow Alpha parameters"""
    # OBI (Order Book Imbalance) thresholds
    # Backtested: 15M shows edge (55.7% hit rate, 1.57 PF at threshold 2.2)
    obi_strong_imbalance: float = 2.5
    obi_moderate_imbalance: float = 2.0
    obi_threshold: float = 2.2  # Signal detection threshold (backtested optimal)
    # LDR (Liquidity Depth Ratio)
    ldr_wall_threshold: float = 2.5
    # CVD (Cumulative Volume Delta)
    cvd_threshold: float = 5000  # Contracts threshold for scoring
    # Absorption detection (conservative middle-ground for untested timeframes)
    absorption_volume_mult: float = 1.5
    absorption_price_tol: float = 0.0015  # 0.15% price tolerance
    absorption_dom_threshold: float = 0.52  # DOM > 0.52 bullish, < 0.48 bearish
    absorption_lookback: int = 15
    # Timeframe-specific absorption parameters (1M, 5M, 15M: backtested | 1H+: projected)
    absorption_by_tf: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        '1M': {'volume_mult': 1.8, 'price_tol': 0.002, 'dom_threshold': 0.51, 'lookback': 10},
        '5M': {'volume_mult': 1.8, 'price_tol': 0.0005, 'dom_threshold': 0.51, 'lookback': 50},
        '15M': {'volume_mult': 1.3, 'price_tol': 0.002, 'dom_threshold': 0.51, 'lookback': 10},
        '1H': {'volume_mult': 1.8, 'price_tol': 0.005, 'dom_threshold': 0.51, 'lookback': 24},
        '4H': {'volume_mult': 1.8, 'price_tol': 0.005, 'dom_threshold': 0.51, 'lookback': 30},
        '1D': {'volume_mult': 1.8, 'price_tol': 0.005, 'dom_threshold': 0.51, 'lookback': 20},
    })
    # Timeframe-specific OBI thresholds
    # Only 15M currently shows predictive value
    obi_by_tf: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        '1M': {'threshold': 2.5},
        '5M': {'threshold': 2.2},
        '15M': {'threshold': 2.2},  # Backtested: 55.7% hit rate, 1.57 PF
        '1H': {'threshold': 2.0},
        '4H': {'threshold': 2.0},
        '1D': {'threshold': 2.0},
    })
    # LSF (Liquidity Sweep Fade) detection - PURE PRICE BASED
    lsf_sweep_threshold_pct: float = 0.001  # Min % beyond level for sweep
    lsf_snapback_pct: float = 0.002  # 0.2% snapback required
    lsf_snapback_bars: int = 3  # Max bars to wait for snapback
    # Delta Unwind detection
    delta_zscore_threshold: float = 2.0  # Z-score threshold for extreme
    delta_unwind_pct: float = 0.1  # Min % of delta that must unwind
    delta_unwind_bars: int = 3  # Bars to confirm unwind
    # Exhaustion detection
    exhaustion_volume_mult: float = 1.5  # Volume spike multiplier
    exhaustion_range_ratio_max: float = 0.5  # Max range ratio for exhaustion


@dataclass
class TakeProfitConfig:
    """Take profit targets by mode"""
    weak_bullish: float = 1.0
    high_bullish: float = 1.5
    weak_bearish: float = 1.0
    high_bearish: float = 1.5


@dataclass
class RiskConfig:
    """Risk management parameters"""
    stop_loss_pct: float = 0.5
    take_profit: TakeProfitConfig = field(default_factory=TakeProfitConfig)
    max_position_size: float = 1.0
    scale_in_size: float = 0.5


@dataclass
class ConfidenceConfig:
    """Confidence level thresholds"""
    high_threshold: int = 70
    medium_threshold: int = 55


@dataclass
class AgentBehaviorConfig:
    """Agent behavior parameters"""
    max_iterations: int = 3
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    require_high_confidence: List[str] = field(default_factory=lambda: [
        "ADD_TO_LONG", "ADD_TO_SHORT", "ENTER_SHORT"
    ])


@dataclass
class StrengthMultipliersConfig:
    """Signal strength multipliers"""
    absorption: float = 1.0
    lsf: float = 1.0  # Pure price LSF
    obi: float = 0.8
    delta_unwind: float = 1.2  # Reversal signal
    exhaustion: float = 1.0  # Reversal signal


@dataclass
class SignalsConfig:
    """Signal detection parameters"""
    recent_signal_bars: int = 20
    strength_multipliers: StrengthMultipliersConfig = field(
        default_factory=StrengthMultipliersConfig
    )


@dataclass
class RegimeThresholdsConfig:
    """Regime classification thresholds"""
    dom_threshold: float = 0.55
    cvd_threshold: float = 5000
    vwap_threshold: float = 0.001


@dataclass
class RegimeSignalWeightsConfig:
    """Regime signal weights"""
    dom: float = 0.6
    cvd: float = 0.2
    vwap: float = 0.2


@dataclass
class RegimeConfig:
    """Regime classification parameters"""
    cvd_windows: Dict[str, int] = field(default_factory=lambda: {
        '5M': 288, '15M': 96, '1H': 24, '4H': 30, '1D': 5
    })
    thresholds: RegimeThresholdsConfig = field(default_factory=RegimeThresholdsConfig)
    signal_weights: RegimeSignalWeightsConfig = field(default_factory=RegimeSignalWeightsConfig)


@dataclass
class SRSignalWeightsConfig:
    """S/R signal weights"""
    dom: float = 0.5
    cvd: float = 0.5


@dataclass
class SRSignalThresholdsConfig:
    """S/R signal thresholds"""
    dom_threshold: float = 0.55
    cvd_threshold: float = 500


@dataclass
class SupportResistanceConfig:
    """Support/Resistance parameters"""
    min_touches: int = 3
    proximity_pct: float = 0.002
    swing_window: int = 5  # Window for swing point detection (default)
    volume_profile_bins: int = 50  # Number of bins for volume profile
    volume_profile_top_n: int = 3  # Top N volume nodes to include
    signal_weights: SRSignalWeightsConfig = field(default_factory=SRSignalWeightsConfig)
    signal_thresholds: SRSignalThresholdsConfig = field(default_factory=SRSignalThresholdsConfig)
    price_range_pct: Dict[str, float] = field(default_factory=lambda: {
        '5M': 10.0, '15M': 10.0, '1H': 15.0, '4H': 15.0, '1D': 20.0
    })
    recency_threshold_pct: Dict[str, float] = field(default_factory=lambda: {
        '5M': 0.5, '15M': 0.8, '1H': 1.0, '4H': 1.5, '1D': 2.5
    })
    # Timeframe-specific swing window (smaller = more sensitive)
    swing_window_by_tf: Dict[str, int] = field(default_factory=lambda: {
        '5M': 3, '15M': 3, '1H': 3, '4H': 3, '1D': 5
    })
    # Timeframe-specific min touches
    min_touches_by_tf: Dict[str, int] = field(default_factory=lambda: {
        '5M': 2, '15M': 2, '1H': 2, '4H': 2, '1D': 2
    })


@dataclass
class AgentConfig:
    """Complete agent configuration"""
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)
    trend_structure: TrendStructureConfig = field(default_factory=TrendStructureConfig)
    market_intensity: MarketIntensityConfig = field(default_factory=MarketIntensityConfig)
    orderflow_alpha: OrderflowAlphaConfig = field(default_factory=OrderflowAlphaConfig)
    indicators: IndicatorsConfig = field(default_factory=IndicatorsConfig)
    instrument: InstrumentConfig = field(default_factory=InstrumentConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    agent: AgentBehaviorConfig = field(default_factory=AgentBehaviorConfig)
    signals: SignalsConfig = field(default_factory=SignalsConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    support_resistance: SupportResistanceConfig = field(default_factory=SupportResistanceConfig)


# Singleton instance
_config: Optional[AgentConfig] = None


def _dict_to_dataclass(data: dict, cls):
    """Recursively convert dict to dataclass"""
    if data is None:
        return cls()

    field_types = {f.name: f.type for f in cls.__dataclass_fields__.values()}
    kwargs = {}

    for key, value in data.items():
        if key in field_types:
            field_type = field_types[key]
            # Check if field is a dataclass
            if hasattr(field_type, '__dataclass_fields__'):
                kwargs[key] = _dict_to_dataclass(value, field_type)
            else:
                kwargs[key] = value

    return cls(**kwargs)


def load_config(config_path: Optional[Path] = None, force_reload: bool = False) -> AgentConfig:
    """Load agent configuration from YAML file

    Args:
        config_path: Optional path to config file. Defaults to config/agent_config.yaml
        force_reload: If True, reload config even if cached

    Returns:
        AgentConfig dataclass with all parameters
    """
    global _config

    if _config is not None and not force_reload:
        return _config

    path = config_path or CONFIG_PATH

    if not path.exists():
        logger.warning(f"Config file not found at {path}, using defaults")
        _config = AgentConfig()
        return _config

    try:
        with open(path, 'r') as f:
            raw_config = yaml.safe_load(f)

        # Build config from YAML
        _config = AgentConfig(
            scoring=_dict_to_dataclass(raw_config.get('scoring'), ScoringConfig),
            thresholds=_dict_to_dataclass(raw_config.get('thresholds'), ThresholdsConfig),
            trend_structure=_dict_to_dataclass(raw_config.get('trend_structure'), TrendStructureConfig),
            market_intensity=_dict_to_dataclass(raw_config.get('market_intensity'), MarketIntensityConfig),
            orderflow_alpha=_dict_to_dataclass(raw_config.get('orderflow_alpha'), OrderflowAlphaConfig),
            indicators=_dict_to_dataclass(raw_config.get('indicators'), IndicatorsConfig),
            instrument=_dict_to_dataclass(raw_config.get('instrument'), InstrumentConfig),
            streaming=_dict_to_dataclass(raw_config.get('streaming'), StreamingConfig),
            risk=_dict_to_dataclass(raw_config.get('risk'), RiskConfig),
            agent=_dict_to_dataclass(raw_config.get('agent'), AgentBehaviorConfig),
            signals=_dict_to_dataclass(raw_config.get('signals'), SignalsConfig),
            regime=_dict_to_dataclass(raw_config.get('regime'), RegimeConfig),
            support_resistance=_dict_to_dataclass(raw_config.get('support_resistance'), SupportResistanceConfig),
        )

        logger.info(f"Loaded agent config from {path}")
        return _config

    except Exception as e:
        logger.error(f"Error loading config from {path}: {e}")
        _config = AgentConfig()
        return _config


def get_config() -> AgentConfig:
    """Get current agent configuration (loads if not cached)"""
    return load_config()


def reload_config() -> AgentConfig:
    """Force reload configuration from file"""
    return load_config(force_reload=True)
