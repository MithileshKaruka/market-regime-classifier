"""LangGraph Trading Agent

A state machine that observes market conditions, evaluates bias, and makes trading decisions.

Graph Structure:
    [START] -> observe -> evaluate -> decide -> [END or back to observe]

Nodes:
- observe: Fetch current market data (OHLCV, orderflow metrics)
- evaluate: Calculate bias score using the scoring system
- decide: Make trading decision based on score and current position

Conditional Edges:
- After decide: If in active trade, may loop back to observe for monitoring
- If score in neutral zone (45-55), wait and re-observe
"""
import logging
from typing import TypedDict, Literal, Optional, List, Annotated
from datetime import datetime
from enum import Enum

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from app.features.agent_bias import AgentBiasCalculator, AgentMode, AgentBiasResult
from app.features.orderflow_metrics import OrderflowMetricsCalculator
from app.features.orderflow_signals import OrderflowSignalDetector
from app.features.zone_bias import ZoneBiasScorer
from app.data.storage import DuckDBStorage
import polars as pl

logger = logging.getLogger(__name__)


class TradeAction(str, Enum):
    """Possible trading actions"""
    WAIT = "WAIT"           # No action, continue observing
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT_LONG = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"
    ADD_TO_LONG = "ADD_TO_LONG"
    ADD_TO_SHORT = "ADD_TO_SHORT"


class PositionState(str, Enum):
    """Current position state"""
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


class AgentState(TypedDict, total=False):
    """State schema for the trading agent graph"""
    # Market observation
    timeframe: str
    symbol: str
    current_price: float
    timestamp: int

    # Bias evaluation
    bias_score: float
    agent_mode: str
    confidence: str
    trend_score: float
    intensity_score: float
    orderflow_score: float

    # Position tracking
    position: str  # FLAT, LONG, SHORT
    entry_price: Optional[float]
    position_size: Optional[float]
    unrealized_pnl: Optional[float]

    # Decision output
    action: str
    action_reason: str
    stop_loss: Optional[float]
    take_profit: Optional[float]

    # Zone-aware bias
    zone_bias: Optional[float]
    zone_type: Optional[str]  # DEMAND or SUPPLY
    zone_quality: Optional[float]
    zone_confirmed: Optional[bool]

    # Metadata
    iteration: int
    last_action_time: Optional[int]
    messages: Annotated[list, add_messages]

    # Internal data (not returned to API)
    _market_df: Optional[list]  # Serialized market data


def observe_market(state: AgentState) -> AgentState:
    """Observation node: Fetch current market data

    Gathers:
    - Latest OHLCV data
    - Orderflow metrics (RVOL, VPIN, LDR)
    - Recent signals (Absorption, LSF)
    """
    timeframe = state.get("timeframe", "1H")
    symbol = state.get("symbol", "MNQ")

    logger.info(f"[Observe] Fetching market data for {symbol} {timeframe}")

    with DuckDBStorage() as storage:
        # Get latest OHLCV
        df = storage.conn.execute(f"""
            SELECT
                timestamp,
                open, high, low, close, volume,
                dom_imbalance,
                instant_delta
            FROM ohlcv_ticks
            WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
            ORDER BY timestamp DESC
            LIMIT 200
        """).pl()

        if len(df) == 0:
            logger.warning(f"No data for {symbol} {timeframe}")
            return {
                **state,
                "current_price": 0,
                "timestamp": int(datetime.utcnow().timestamp()),
                "_market_df": [],
                "messages": [{"role": "system", "content": f"No market data available for {symbol} {timeframe}"}],
            }

        # Reverse to chronological order
        df = df.reverse()

        latest = df.tail(1).to_dicts()[0]
        current_price = latest["close"]
        timestamp = int(latest["timestamp"].timestamp()) if hasattr(latest["timestamp"], "timestamp") else latest["timestamp"]

        # Serialize DataFrame to list of dicts for state transfer between nodes
        market_data_serialized = df.to_dicts()

        return {
            **state,
            "current_price": current_price,
            "timestamp": timestamp,
            "iteration": state.get("iteration", 0) + 1,
            "_market_df": market_data_serialized,
            "messages": [{"role": "assistant", "content": f"Observed {symbol} at ${current_price:.2f}"}],
        }


def evaluate_bias(state: AgentState) -> AgentState:
    """Evaluation node: Calculate bias score from market data

    Uses the AgentBiasCalculator to compute:
    - Trend & Structure score (20%)
    - Market Intensity score (20%)
    - Order Flow Alpha score (60%)

    Zone-Aware Enhancement:
    - Detects S/D zones on the analysis timeframe
    - Uses 15M orderflow signals for confirmation (always)
    - Adjusts final bias based on zone proximity
    """
    timeframe = state.get("timeframe", "1H")
    symbol = state.get("symbol", "MNQ")

    # Deserialize market data from state
    market_data_list = state.get("_market_df", [])
    if not market_data_list:
        return {
            **state,
            "bias_score": 50.0,
            "agent_mode": AgentMode.NEUTRAL.value,
            "confidence": "LOW",
            "messages": [{"role": "system", "content": "No market data to evaluate"}],
        }

    # Convert list of dicts back to DataFrame with explicit schema to handle nulls
    # Use infer_schema_length=None to scan all rows for proper type inference
    try:
        df = pl.DataFrame(market_data_list, infer_schema_length=None)
    except Exception:
        # Fallback: define schema explicitly if inference fails
        df = pl.DataFrame(market_data_list, schema={
            "timestamp": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "dom_imbalance": pl.Float64,
            "instant_delta": pl.Int64,
        })
    logger.info(f"[Evaluate] Calculating bias score from {len(df)} bars")

    # Add depth columns
    df = df.with_columns([
        (pl.col("volume") * pl.col("dom_imbalance")).alias("total_bid_depth"),
        (pl.col("volume") * (1 - pl.col("dom_imbalance"))).alias("total_ask_depth"),
    ])

    # Calculate metrics
    metrics_calc = OrderflowMetricsCalculator()
    rvol_metrics = metrics_calc.calculate_rvol(df)
    vpin_metrics = metrics_calc.calculate_vpin(df)
    ldr_metrics = metrics_calc.calculate_ldr(df)

    # ============================================================
    # Zone-Aware Orderflow: Use 15M signals for HTF analysis
    # ============================================================
    # For higher timeframes (1H, 4H, 1D), orderflow signals work best on 15M
    # So we fetch 15M data and use those signals instead of same-TF signals

    is_higher_tf = timeframe in ("1H", "4H", "1D")
    zone_scorer = ZoneBiasScorer()

    if is_higher_tf:
        # Get 15M orderflow score and signals for higher timeframes
        logger.info(f"[Evaluate] Using 15M orderflow signals for {timeframe} analysis")
        of_score_15m, active_signals_15m = zone_scorer.get_15m_orderflow_score(symbol)

        # Convert active signals to dict format for bias calculator
        abs_dicts = [{"direction": "BULLISH" if "+" in s else "BEARISH", "strength": 0.8}
                     for s in active_signals_15m if s.startswith("ABS")]
        exh_dicts = [{"direction": "BULLISH" if "+" in s else "BEARISH", "strength": 0.8}
                     for s in active_signals_15m if s.startswith("EXH")]
        du_dicts = [{"direction": "BULLISH" if "+" in s else "BEARISH", "strength": 0.8}
                    for s in active_signals_15m if s.startswith("DU")]
    else:
        # Use same-timeframe signals for 5M and 15M
        detector = OrderflowSignalDetector(timeframe=timeframe, lookback_bars=20)

        # Detect all signals on full dataset (more accurate than per-window detection)
        all_absorption = detector.detect_absorption(df)
        all_exhaustion = detector.detect_exhaustion(df)
        all_delta_unwind = detector.detect_delta_unwind(df)

        # Filter to only signals from recent N bars
        signal_window_bars = 20
        recent_cutoff_ts = df.tail(signal_window_bars)["timestamp"].min()

        def filter_recent(signals):
            """Keep only signals from recent window"""
            recent = []
            for s in signals:
                sig_ts = s.timestamp
                cutoff = recent_cutoff_ts
                if hasattr(sig_ts, "timestamp"):
                    sig_ts = sig_ts.timestamp()
                if hasattr(cutoff, "timestamp"):
                    cutoff = cutoff.timestamp()
                if sig_ts >= cutoff:
                    recent.append({"direction": s.direction.value, "strength": s.strength})
            return recent

        abs_dicts = filter_recent(all_absorption)
        exh_dicts = filter_recent(all_exhaustion)
        du_dicts = filter_recent(all_delta_unwind)

    # Get latest CVD value from the data
    latest = df.tail(1).to_dicts()[0]
    cvd_value = latest.get("instant_delta")
    current_price = latest["close"]

    # Calculate total bias with all signal types
    bias_calc = AgentBiasCalculator()
    bias_result = bias_calc.calculate_total_bias(
        df=df,
        rvol=rvol_metrics.rvol if rvol_metrics else None,
        vpin=vpin_metrics.vpin if vpin_metrics else None,
        obi_ratio=ldr_metrics.ldr if ldr_metrics else None,
        ldr=ldr_metrics.ldr if ldr_metrics else None,
        absorption_signals=abs_dicts,
        cvd=cvd_value,
        delta_unwind_signals=du_dicts,
        exhaustion_signals=exh_dicts,
        timeframe=timeframe,  # Use timeframe-specific weights
    )

    # ============================================================
    # Zone Bias Adjustment
    # ============================================================
    # Calculate zone proximity bias (uses 15M orderflow for confirmation)
    zone_bias_result = zone_scorer.calculate_zone_bias(
        timeframe=timeframe,
        symbol=symbol,
        current_price=current_price,
    )

    # Adjust final score with zone bias
    # Zone bias is capped at +/- 15 points
    adjusted_score = bias_result.total_score + zone_bias_result.zone_bias
    adjusted_score = max(0, min(100, adjusted_score))  # Clamp to 0-100

    # Recalculate mode based on adjusted score
    from config import get_config
    config = get_config()
    thresholds = config.thresholds

    if adjusted_score <= thresholds.high_bearish_max:
        adjusted_mode = AgentMode.HIGH_BEARISH
    elif adjusted_score <= thresholds.weak_bearish_max:
        adjusted_mode = AgentMode.WEAK_BEARISH
    elif adjusted_score <= thresholds.neutral_max:
        adjusted_mode = AgentMode.NEUTRAL
    elif adjusted_score <= thresholds.weak_bullish_max:
        adjusted_mode = AgentMode.WEAK_BULLISH
    else:
        adjusted_mode = AgentMode.HIGH_BULLISH

    # Build evaluation message
    evaluation_msg = (
        f"Bias Score: {adjusted_score:.1f}/100 | Mode: {adjusted_mode.value}\n"
        f"Trend/Structure: {bias_result.trend_structure.score:.1f} | "
        f"Intensity: {bias_result.market_intensity.score:.1f} | "
        f"Orderflow: {bias_result.orderflow_alpha.score:.1f}"
    )

    if zone_bias_result.zone_bias != 0:
        evaluation_msg += f"\nZone Bias: {zone_bias_result.zone_bias:+.1f} ({zone_bias_result.details})"

    if is_higher_tf:
        evaluation_msg += f"\n[Using 15M orderflow for {timeframe} zone analysis]"

    return {
        **state,
        "bias_score": adjusted_score,
        "agent_mode": adjusted_mode.value,
        "confidence": bias_result.confidence,
        "trend_score": bias_result.trend_structure.score,
        "intensity_score": bias_result.market_intensity.score,
        "orderflow_score": bias_result.orderflow_alpha.score,
        "zone_bias": zone_bias_result.zone_bias,
        "zone_type": zone_bias_result.active_zone.zone_type.value if zone_bias_result.active_zone else None,
        "zone_quality": zone_bias_result.zone_quality,
        "zone_confirmed": zone_bias_result.orderflow_confirmation,
        "messages": [{"role": "assistant", "content": evaluation_msg}],
    }


def decide_action(state: AgentState) -> AgentState:
    """Decision node: Determine trading action based on bias and position

    Decision Matrix (thresholds from config):
    - HIGH_BEARISH: Short only, exit longs
    - WEAK_BEARISH: Exit longs, no new trades
    - NEUTRAL: Wait, no trades
    - WEAK_BULLISH: Cautious longs at S/R
    - HIGH_BULLISH: Aggressive longs, add to winners
    """
    from config import get_config
    config = get_config()

    bias_score = state.get("bias_score", 50)
    agent_mode = state.get("agent_mode", AgentMode.NEUTRAL.value)
    position = state.get("position", PositionState.FLAT.value)
    current_price = state.get("current_price", 0)
    confidence = state.get("confidence", "LOW")

    # Get risk params from config
    stop_loss_pct = config.risk.stop_loss_pct / 100
    tp_config = config.risk.take_profit

    logger.info(f"[Decide] Mode={agent_mode}, Position={position}, Score={bias_score:.1f}")

    action = TradeAction.WAIT
    action_reason = ""
    stop_loss = None
    take_profit = None

    # Check if action requires high confidence
    def requires_high_confidence(action_name: str) -> bool:
        return action_name in config.agent.require_high_confidence

    # Decision logic based on mode and current position
    if agent_mode == AgentMode.HIGH_BEARISH.value:
        if position == PositionState.LONG.value:
            action = TradeAction.EXIT_LONG
            action_reason = f"HIGH_BEARISH ({bias_score:.0f}) - Exit long immediately"
        elif position == PositionState.FLAT.value and confidence != "LOW":
            if requires_high_confidence("ENTER_SHORT") and confidence != "HIGH":
                action = TradeAction.WAIT
                action_reason = f"HIGH_BEARISH ({bias_score:.0f}) - Waiting for HIGH confidence to short"
            else:
                action = TradeAction.ENTER_SHORT
                action_reason = f"HIGH_BEARISH ({bias_score:.0f}) - Enter short"
                stop_loss = current_price * (1 + stop_loss_pct)
                take_profit = current_price * (1 - tp_config.high_bearish / 100)
        elif position == PositionState.SHORT.value and confidence == "HIGH":
            action = TradeAction.ADD_TO_SHORT
            action_reason = f"HIGH_BEARISH ({bias_score:.0f}) with HIGH confidence - Add to short"
        else:
            action = TradeAction.WAIT
            action_reason = f"HIGH_BEARISH but already short or low confidence"

    elif agent_mode == AgentMode.WEAK_BEARISH.value:
        if position == PositionState.LONG.value:
            action = TradeAction.EXIT_LONG
            action_reason = f"WEAK_BEARISH ({bias_score:.0f}) - Exit long, market cooling"
        else:
            action = TradeAction.WAIT
            action_reason = f"WEAK_BEARISH ({bias_score:.0f}) - Wait for clarity"

    elif agent_mode == AgentMode.NEUTRAL.value:
        action = TradeAction.WAIT
        action_reason = f"NEUTRAL ({bias_score:.0f}) - Chop zone, avoid trading"

    elif agent_mode == AgentMode.WEAK_BULLISH.value:
        if position == PositionState.SHORT.value:
            action = TradeAction.EXIT_SHORT
            action_reason = f"WEAK_BULLISH ({bias_score:.0f}) - Exit short"
        elif position == PositionState.FLAT.value and confidence != "LOW":
            action = TradeAction.ENTER_LONG
            action_reason = f"WEAK_BULLISH ({bias_score:.0f}) - Cautious long at S/R"
            stop_loss = current_price * (1 - stop_loss_pct)
            take_profit = current_price * (1 + tp_config.weak_bullish / 100)
        else:
            action = TradeAction.WAIT
            action_reason = f"WEAK_BULLISH - Already positioned or low confidence"

    elif agent_mode == AgentMode.HIGH_BULLISH.value:
        if position == PositionState.SHORT.value:
            action = TradeAction.EXIT_SHORT
            action_reason = f"HIGH_BULLISH ({bias_score:.0f}) - Exit short immediately"
        elif position == PositionState.FLAT.value:
            action = TradeAction.ENTER_LONG
            action_reason = f"HIGH_BULLISH ({bias_score:.0f}) - Enter long aggressively"
            stop_loss = current_price * (1 - stop_loss_pct)
            take_profit = current_price * (1 + tp_config.high_bullish / 100)
        elif position == PositionState.LONG.value and confidence == "HIGH":
            action = TradeAction.ADD_TO_LONG
            action_reason = f"HIGH_BULLISH ({bias_score:.0f}) with HIGH confidence - Add to long"
        else:
            action = TradeAction.WAIT
            action_reason = f"HIGH_BULLISH - Already long"

    decision_msg = f"Action: {action.value} | Reason: {action_reason}"
    if stop_loss:
        decision_msg += f" | SL: ${stop_loss:.2f} | TP: ${take_profit:.2f}"

    return {
        **state,
        "action": action.value,
        "action_reason": action_reason,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "last_action_time": int(datetime.utcnow().timestamp()),
        "messages": [{"role": "assistant", "content": decision_msg}],
    }


def should_continue(state: AgentState) -> Literal["observe", "__end__"]:
    """Router: Determine if we should continue observing or end

    Continue if:
    - Action was WAIT and we haven't exceeded max iterations
    - We're in an active position that needs monitoring

    End if:
    - We've taken a decisive action (entry/exit)
    - Max iterations reached (from config)
    """
    from config import get_config
    config = get_config()

    action = state.get("action", TradeAction.WAIT.value)
    iteration = state.get("iteration", 0)
    max_iterations = config.agent.max_iterations

    # End after decisive actions or max iterations
    if iteration >= max_iterations:
        logger.info(f"[Router] Max iterations ({max_iterations}) reached, ending")
        return "__end__"

    if action in [TradeAction.ENTER_LONG.value, TradeAction.ENTER_SHORT.value,
                  TradeAction.EXIT_LONG.value, TradeAction.EXIT_SHORT.value]:
        logger.info(f"[Router] Decisive action taken ({action}), ending")
        return "__end__"

    # Continue observing for WAIT or ADD actions
    logger.info(f"[Router] Action={action}, iteration={iteration}, continuing to observe")
    return "observe"


def create_trading_agent() -> StateGraph:
    """Create the LangGraph trading agent

    Graph:
        START -> observe -> evaluate -> decide -> (continue?) -> observe / END
    """
    # Create graph with state schema
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("observe", observe_market)
    graph.add_node("evaluate", evaluate_bias)
    graph.add_node("decide", decide_action)

    # Add edges
    graph.set_entry_point("observe")
    graph.add_edge("observe", "evaluate")
    graph.add_edge("evaluate", "decide")

    # Conditional edge after decide
    graph.add_conditional_edges(
        "decide",
        should_continue,
        {
            "observe": "observe",
            "__end__": END,
        }
    )

    return graph.compile()


# Create singleton instance
trading_agent = create_trading_agent()


async def run_agent(
    timeframe: str = "1H",
    symbol: str = "MNQ",
    current_position: str = "FLAT",
    entry_price: Optional[float] = None,
) -> dict:
    """Run the trading agent and return the decision

    Args:
        timeframe: Chart timeframe to analyze
        symbol: Trading symbol
        current_position: Current position state (FLAT, LONG, SHORT)
        entry_price: Entry price if in a position

    Returns:
        Final agent state with decision
    """
    initial_state: AgentState = {
        "timeframe": timeframe,
        "symbol": symbol,
        "current_price": 0,
        "timestamp": 0,
        "bias_score": 50,
        "agent_mode": AgentMode.NEUTRAL.value,
        "confidence": "LOW",
        "trend_score": 50,
        "intensity_score": 50,
        "orderflow_score": 50,
        "zone_bias": None,
        "zone_type": None,
        "zone_quality": None,
        "zone_confirmed": None,
        "position": current_position,
        "entry_price": entry_price,
        "position_size": None,
        "unrealized_pnl": None,
        "action": TradeAction.WAIT.value,
        "action_reason": "",
        "stop_loss": None,
        "take_profit": None,
        "iteration": 0,
        "last_action_time": None,
        "messages": [],
    }

    try:
        # Run the graph
        final_state = trading_agent.invoke(initial_state)

        # Clean up internal state (don't return serialized data to client)
        if "_market_df" in final_state:
            del final_state["_market_df"]

        return final_state
    except Exception as e:
        logger.error(f"Agent execution error: {e}", exc_info=True)
        # Return a fallback state on error
        return {
            **initial_state,
            "action": TradeAction.WAIT.value,
            "action_reason": f"Agent error: {str(e)}",
            "messages": [{"role": "system", "content": f"Agent execution failed: {str(e)}"}],
        }
