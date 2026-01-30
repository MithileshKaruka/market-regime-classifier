import { useState, useEffect } from 'react'
import './AgentPanel.css'
import {
  API_CONFIG,
  COLORS,
  SCORE_WEIGHTS,
  THRESHOLDS,
  getModeColor,
  getActionColor,
  getScoreGradient,
} from '../config'

interface AgentDecision {
  action: string
  action_reason: string
  stop_loss: number | null
  take_profit: number | null
}

interface BiasDetails {
  total_score: number
  mode: string
  recommendation: string
  confidence: string
  trend_structure: {
    score: number
    ema_trend: string
    market_structure: string
    price_vs_sr: string
    details: string
  }
  market_intensity: {
    score: number
    rvol: number
    rvol_contribution: number
    vpin: number
    vpin_contribution: number
    is_high_conviction: boolean
    details: string
  }
  orderflow_alpha: {
    score: number
    obi_score: number
    ldr_score: number
    absorption_score: number
    lsf_score: number
    active_signals: string[]
    details: string
  }
  details: string
}

interface AgentPanelProps {
  timeframe: string
}

export default function AgentPanel({ timeframe }: AgentPanelProps) {
  const [decision, setDecision] = useState<AgentDecision | null>(null)
  const [biasDetails, setBiasDetails] = useState<BiasDetails | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showReasoning, setShowReasoning] = useState(false)

  const fetchBiasScore = async () => {
    setLoading(true)
    setError(null)
    try {
      // Fetch both agent decision (for action/SL/TP) and detailed bias in parallel
      const [agentRes, biasRes] = await Promise.all([
        fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.agentDecision}/${timeframe}?position=FLAT`),
        fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.agentBias}/${timeframe}`)
      ])

      if (agentRes.ok) {
        const agentData = await agentRes.json()
        setDecision(agentData)
      }

      if (!biasRes.ok) throw new Error('Failed to fetch bias score')
      const data = await biasRes.json()
      setBiasDetails(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchBiasScore()
  }, [timeframe])

  if (error) {
    return (
      <div className="agent-panel">
        <div className="agent-header">
          <h3>Market Bias</h3>
          <button onClick={fetchBiasScore} className="refresh-btn">Retry</button>
        </div>
        <div className="agent-error">{error}</div>
      </div>
    )
  }

  return (
    <div className="agent-panel">
      <div className="agent-header">
        <h3>Market Bias</h3>
        <button
          onClick={fetchBiasScore}
          className="refresh-btn"
          disabled={loading}
        >
          {loading ? '...' : 'Refresh'}
        </button>
      </div>

      {biasDetails && (
        <>
          {/* Bias Score Gauge */}
          <div className="bias-gauge">
            <div className="gauge-label">
              <span>Bias Score</span>
              <span
                className="score-value"
                style={{ color: getModeColor(biasDetails.mode) }}
              >
                {biasDetails.total_score.toFixed(1)}
              </span>
            </div>
            <div
              className="gauge-bar"
              style={{ background: getScoreGradient(biasDetails.total_score) }}
            />
            <div className="gauge-labels">
              <span>Bearish</span>
              <span>Neutral</span>
              <span>Bullish</span>
            </div>
          </div>

          {/* Mode & Confidence */}
          <div className="agent-mode">
            <div
              className="mode-badge"
              style={{ backgroundColor: getModeColor(biasDetails.mode) }}
            >
              {biasDetails.mode.replace('_', ' ')}
            </div>
            <div className="confidence-badge">
              {biasDetails.confidence}
            </div>
          </div>

          {/* Action Card with SL/TP */}
          {decision && (
            <div
              className="action-card"
              style={{ borderColor: getActionColor(decision.action) }}
            >
              <div className="action-label">Recommended Action</div>
              <div
                className="action-value"
                style={{ color: getActionColor(decision.action) }}
              >
                {decision.action.replace('_', ' ')}
              </div>
              <div className="action-reason">{decision.action_reason}</div>

              {decision.stop_loss && decision.take_profit && (
                <div className="trade-levels">
                  <div className="level stop-loss">
                    <span>SL</span>
                    <span>${decision.stop_loss.toFixed(2)}</span>
                  </div>
                  <div className="level take-profit">
                    <span>TP</span>
                    <span>${decision.take_profit.toFixed(2)}</span>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Component Scores */}
          <div className="component-scores">
            <div className="score-row">
              <span className="score-label">{SCORE_WEIGHTS.trend.label} ({SCORE_WEIGHTS.trend.weight}%)</span>
              <div className="score-bar-container">
                <div
                  className="score-bar"
                  style={{
                    width: `${biasDetails.trend_structure.score}%`,
                    backgroundColor: biasDetails.trend_structure.score >= THRESHOLDS.score.midpoint ? COLORS.bullishLight : COLORS.bearish
                  }}
                />
              </div>
              <span className="score-num">{biasDetails.trend_structure.score.toFixed(0)}</span>
            </div>
            <div className="score-row">
              <span className="score-label">{SCORE_WEIGHTS.intensity.label} ({SCORE_WEIGHTS.intensity.weight}%)</span>
              <div className="score-bar-container">
                <div
                  className="score-bar"
                  style={{
                    width: `${biasDetails.market_intensity.score}%`,
                    backgroundColor: biasDetails.market_intensity.score >= THRESHOLDS.score.midpoint ? COLORS.bullishLight : COLORS.bearish
                  }}
                />
              </div>
              <span className="score-num">{biasDetails.market_intensity.score.toFixed(0)}</span>
            </div>
            <div className="score-row">
              <span className="score-label">{SCORE_WEIGHTS.orderflow.label} ({SCORE_WEIGHTS.orderflow.weight}%)</span>
              <div className="score-bar-container">
                <div
                  className="score-bar"
                  style={{
                    width: `${biasDetails.orderflow_alpha.score}%`,
                    backgroundColor: biasDetails.orderflow_alpha.score >= THRESHOLDS.score.midpoint ? COLORS.bullishLight : COLORS.bearish
                  }}
                />
              </div>
              <span className="score-num">{biasDetails.orderflow_alpha.score.toFixed(0)}</span>
            </div>
          </div>

          {/* Active Signals */}
          {biasDetails.orderflow_alpha.active_signals.length > 0 && (
            <div className="active-signals-banner">
              <span className="signals-label">Active Signals:</span>
              <span className="signals-list">{biasDetails.orderflow_alpha.active_signals.join(', ')}</span>
            </div>
          )}

          {/* Show Reasoning Button */}
          <button
            className="reasoning-toggle"
            onClick={() => setShowReasoning(true)}
          >
            View Details
          </button>
        </>
      )}

      {/* Reasoning Overlay */}
      {showReasoning && biasDetails && (
        <div className="reasoning-overlay" onClick={() => setShowReasoning(false)}>
          <div className="reasoning-content" onClick={(e) => e.stopPropagation()}>
            <div className="reasoning-header">
              <h3>Bias Analysis Details</h3>
              <button className="close-btn" onClick={() => setShowReasoning(false)}>×</button>
            </div>

            <div className="reasoning-summary">
              <div className="summary-score" style={{ color: getModeColor(biasDetails.mode) }}>
                {biasDetails.total_score.toFixed(1)}
              </div>
              <div className="summary-mode">{biasDetails.mode.replace('_', ' ')}</div>
              <div className="summary-recommendation">{biasDetails.recommendation}</div>
            </div>

            {/* Trend & Structure Section */}
            <div className="reasoning-section">
              <div className="section-header">
                <span className="section-title">Trend & Structure</span>
                <span className="section-weight">{SCORE_WEIGHTS.trend.weight}%</span>
                <span
                  className="section-score"
                  style={{ color: biasDetails.trend_structure.score >= THRESHOLDS.score.midpoint ? COLORS.bullishLight : COLORS.bearish }}
                >
                  {biasDetails.trend_structure.score.toFixed(0)}
                </span>
              </div>
              <div className="section-details">
                <div className="detail-row">
                  <span>EMA Trend</span>
                  <span className={biasDetails.trend_structure.ema_trend === 'BULLISH' ? 'bullish' : biasDetails.trend_structure.ema_trend === 'BEARISH' ? 'bearish' : ''}>
                    {biasDetails.trend_structure.ema_trend}
                  </span>
                </div>
                <div className="detail-row">
                  <span>Structure</span>
                  <span>{biasDetails.trend_structure.market_structure}</span>
                </div>
                <div className="detail-row">
                  <span>vs S/R</span>
                  <span>{biasDetails.trend_structure.price_vs_sr}</span>
                </div>
              </div>
              <div className="section-explanation">{biasDetails.trend_structure.details}</div>
            </div>

            {/* Market Intensity Section */}
            <div className="reasoning-section">
              <div className="section-header">
                <span className="section-title">Market Intensity</span>
                <span className="section-weight">{SCORE_WEIGHTS.intensity.weight}%</span>
                <span
                  className="section-score"
                  style={{ color: biasDetails.market_intensity.score >= THRESHOLDS.score.midpoint ? COLORS.bullishLight : COLORS.bearish }}
                >
                  {biasDetails.market_intensity.score.toFixed(0)}
                </span>
              </div>
              <div className="section-details">
                <div className="detail-row">
                  <span>RVOL</span>
                  <span>{biasDetails.market_intensity.rvol.toFixed(2)}x → +{biasDetails.market_intensity.rvol_contribution.toFixed(0)} pts</span>
                </div>
                <div className="detail-row">
                  <span>VPIN</span>
                  <span>{(biasDetails.market_intensity.vpin * 100).toFixed(0)}% → +{biasDetails.market_intensity.vpin_contribution.toFixed(0)} pts</span>
                </div>
                <div className="detail-row">
                  <span>Conviction</span>
                  <span className={biasDetails.market_intensity.is_high_conviction ? 'bullish' : ''}>
                    {biasDetails.market_intensity.is_high_conviction ? 'HIGH' : 'NORMAL'}
                  </span>
                </div>
              </div>
              <div className="section-explanation">{biasDetails.market_intensity.details}</div>
            </div>

            {/* Orderflow Alpha Section */}
            <div className="reasoning-section">
              <div className="section-header">
                <span className="section-title">Orderflow Alpha</span>
                <span className="section-weight">{SCORE_WEIGHTS.orderflow.weight}%</span>
                <span
                  className="section-score"
                  style={{ color: biasDetails.orderflow_alpha.score >= THRESHOLDS.score.midpoint ? COLORS.bullishLight : COLORS.bearish }}
                >
                  {biasDetails.orderflow_alpha.score.toFixed(0)}
                </span>
              </div>
              <div className="section-details">
                <div className="detail-row">
                  <span>OBI</span>
                  <span>{biasDetails.orderflow_alpha.obi_score.toFixed(0)} pts</span>
                </div>
                <div className="detail-row">
                  <span>LDR</span>
                  <span>{biasDetails.orderflow_alpha.ldr_score.toFixed(0)} pts</span>
                </div>
                <div className="detail-row">
                  <span>Absorption</span>
                  <span>{biasDetails.orderflow_alpha.absorption_score.toFixed(0)} pts</span>
                </div>
                <div className="detail-row">
                  <span>LSF</span>
                  <span>{biasDetails.orderflow_alpha.lsf_score.toFixed(0)} pts</span>
                </div>
                {biasDetails.orderflow_alpha.active_signals.length > 0 && (
                  <div className="active-signals">
                    Active: {biasDetails.orderflow_alpha.active_signals.join(', ')}
                  </div>
                )}
              </div>
              <div className="section-explanation">{biasDetails.orderflow_alpha.details}</div>
            </div>

            {/* Overall Details */}
            <div className="reasoning-footer">
              {biasDetails.details}
            </div>
          </div>
        </div>
      )}

      {loading && !biasDetails && (
        <div className="agent-loading">Loading bias analysis...</div>
      )}
    </div>
  )
}
