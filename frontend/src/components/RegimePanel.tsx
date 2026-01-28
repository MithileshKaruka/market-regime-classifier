import { useEffect, useState } from 'react'
import './RegimePanel.css'
import {
  API_CONFIG,
  COLORS,
  POLLING_INTERVALS,
  THRESHOLDS,
  LABELS,
  getDirectionIcon,
} from '../config'

interface TrendStructure {
  score: number
  ema_trend: string
  market_structure: string
  price_vs_sr: string
  details: string
}

interface MarketIntensity {
  score: number
  rvol: number
  rvol_contribution: number
  vpin: number
  vpin_contribution: number
  is_high_conviction: boolean
  details: string
}

interface OrderFlowAlpha {
  score: number
  active_mode: string  // ABSORPTION, EXHAUSTION, DELTA_UNWIND, or BASE
  primary_score: number
  ldr_score: number
  obi_score: number
  cvd_score: number
  active_signals: string[]
  details: string
}

interface AgentBias {
  timestamp: number
  timeframe: string
  total_score: number
  mode: string
  recommendation: string
  confidence: string
  trend_structure: TrendStructure
  market_intensity: MarketIntensity
  orderflow_alpha: OrderFlowAlpha
  details: string
}

interface OrderflowSignal {
  timestamp: number
  signal_type: string
  direction: string
  price: number
  strength: number
  details: string
}

export default function RegimePanel() {
  const [agentBias, setAgentBias] = useState<AgentBias | null>(null)
  const [recentSignals, setRecentSignals] = useState<OrderflowSignal[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedTimeframe, setSelectedTimeframe] = useState('15M')

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, POLLING_INTERVALS.domSignals)
    return () => clearInterval(interval)
  }, [selectedTimeframe])

  const fetchData = async () => {
    try {
      // Fetch Agent Bias Score
      const biasResponse = await fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.agentBias}/${selectedTimeframe}`)
      if (biasResponse.ok) {
        const biasData = await biasResponse.json()
        setAgentBias(biasData)
      }

      // Fetch recent signals
      const signalsResponse = await fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.orderflowSignals}/${selectedTimeframe}?limit=${THRESHOLDS.signals.fetchLimit}`)
      if (signalsResponse.ok) {
        const signalsData = await signalsResponse.json()
        const recent = (signalsData.signals || []).slice(-THRESHOLDS.signals.recentCount).reverse()
        setRecentSignals(recent)
      }

      setLoading(false)
    } catch (error) {
      console.error('Error fetching data:', error)
      setAgentBias(getSampleBiasData())
      setRecentSignals(getSampleSignals())
      setLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="regime-panel">
        <h3>Agent Bias Score</h3>
        <div className="loading">Loading...</div>
      </div>
    )
  }

  const getModeColor = (mode: string) => {
    switch (mode) {
      case 'HIGH_BULLISH': return COLORS.bullish
      case 'WEAK_BULLISH': return COLORS.bullishLight
      case 'HIGH_BEARISH': return COLORS.bearish
      case 'WEAK_BEARISH': return '#f97316'
      default: return COLORS.neutral
    }
  }

  const getModeIcon = (mode: string) => {
    if (mode.includes('BULLISH')) return getDirectionIcon('BULLISH')
    if (mode.includes('BEARISH')) return getDirectionIcon('BEARISH')
    return getDirectionIcon('NEUTRAL')
  }

  const getScoreBarWidth = (score: number) => `${score}%`

  const getConfidenceColor = (confidence: string) => {
    switch (confidence) {
      case 'HIGH': return COLORS.bullish
      case 'MEDIUM': return '#f59e0b'
      default: return COLORS.bearish
    }
  }

  return (
    <div className="regime-panel">
      <h3>Agent Bias Score</h3>

      {/* Timeframe Selector */}
      <div className="timeframe-selector">
        {['5M', '15M', '1H', '4H'].map(tf => (
          <button
            key={tf}
            className={`tf-btn ${selectedTimeframe === tf ? 'active' : ''}`}
            onClick={() => setSelectedTimeframe(tf)}
          >
            {tf}
          </button>
        ))}
      </div>

      {agentBias && (
        <>
          {/* Main Score Display */}
          <div className="score-display">
            <div className="score-value" style={{ color: getModeColor(agentBias.mode) }}>
              {agentBias.total_score.toFixed(0)}
            </div>
            <div className="score-label">/ 100</div>
          </div>

          {/* Score Bar */}
          <div className="score-bar-container">
            <div className="score-bar-bg">
              <div
                className="score-bar-fill"
                style={{
                  width: getScoreBarWidth(agentBias.total_score),
                  backgroundColor: getModeColor(agentBias.mode)
                }}
              />
              <div className="score-bar-markers">
                <span style={{ left: '30%' }}>30</span>
                <span style={{ left: '45%' }}>45</span>
                <span style={{ left: '55%' }}>55</span>
                <span style={{ left: '70%' }}>70</span>
              </div>
            </div>
          </div>

          {/* Mode & Recommendation */}
          <div className="mode-display">
            <span className="mode-icon">{getModeIcon(agentBias.mode)}</span>
            <span className="mode-text" style={{ color: getModeColor(agentBias.mode) }}>
              {agentBias.mode.replace('_', ' ')}
            </span>
            <span
              className="confidence-badge"
              style={{ backgroundColor: getConfidenceColor(agentBias.confidence) }}
            >
              {agentBias.confidence}
            </span>
          </div>

          <div className="recommendation">
            {agentBias.recommendation}
          </div>

          {/* Component Breakdown */}
          <div className="components-section">
            <h4>Component Scores</h4>

            {/* Trend & Structure (20%) */}
            <div className="component-row">
              <div className="component-header">
                <span className="component-name">Trend & Structure</span>
                <span className="component-weight">20%</span>
              </div>
              <div className="component-bar-bg">
                <div
                  className="component-bar-fill"
                  style={{ width: `${agentBias.trend_structure.score}%` }}
                />
              </div>
              <div className="component-details">
                <span>{agentBias.trend_structure.ema_trend}</span>
                <span>{agentBias.trend_structure.market_structure}</span>
                <span className="component-score">{agentBias.trend_structure.score.toFixed(0)}</span>
              </div>
            </div>

            {/* Market Intensity (20%) */}
            <div className="component-row">
              <div className="component-header">
                <span className="component-name">Market Intensity</span>
                <span className="component-weight">20%</span>
              </div>
              <div className="component-bar-bg">
                <div
                  className="component-bar-fill"
                  style={{ width: `${agentBias.market_intensity.score}%` }}
                />
              </div>
              <div className="component-details">
                <span>RVOL: {agentBias.market_intensity.rvol.toFixed(1)}x</span>
                <span>VPIN: {(agentBias.market_intensity.vpin * 100).toFixed(0)}%</span>
                <span className="component-score">{agentBias.market_intensity.score.toFixed(0)}</span>
              </div>
            </div>

            {/* Order Flow Alpha (60%) - Context-Aware */}
            <div className="component-row">
              <div className="component-header">
                <span className="component-name">Order Flow Alpha</span>
                <span className="component-weight">60%</span>
              </div>
              <div className="component-bar-bg">
                <div
                  className="component-bar-fill"
                  style={{ width: `${agentBias.orderflow_alpha.score}%` }}
                />
              </div>
              <div className="component-details">
                <span className="active-mode">
                  {agentBias.orderflow_alpha.active_mode.replace('_', ' ')}
                </span>
                {agentBias.orderflow_alpha.active_signals.length > 0 && (
                  <span className="active-signals">
                    {agentBias.orderflow_alpha.active_signals.join(', ')}
                  </span>
                )}
                <span className="component-score">{agentBias.orderflow_alpha.score.toFixed(0)}</span>
              </div>
            </div>
          </div>
        </>
      )}

      {/* Recent Signals */}
      <div className="signals-section">
        <h4>Recent Signals</h4>
        {recentSignals.length === 0 ? (
          <div className="no-signals">No recent signals detected</div>
        ) : (
          <div className="signals-list">
            {recentSignals.map((signal, idx) => (
              <div key={idx} className={`signal-item signal-${signal.direction.toLowerCase()}`}>
                <span className="signal-type">{signal.signal_type}</span>
                <span className="signal-direction">
                  {getDirectionIcon(signal.direction)}
                </span>
                <span className="signal-price">${signal.price.toFixed(2)}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Signal Legend - Primary signals only */}
      <div className="signal-legend">
        <div className="legend-row">
          <span className="legend-dot" style={{ backgroundColor: COLORS.signals.deltaUnwind }}></span>
          <span>{LABELS.signals.deltaUnwind} (86.7%)</span>
        </div>
        <div className="legend-row">
          <span className="legend-dot" style={{ backgroundColor: COLORS.signals.exhaustion }}></span>
          <span>{LABELS.signals.exhaustion} (81.8%)</span>
        </div>
        <div className="legend-row">
          <span className="legend-dot" style={{ backgroundColor: COLORS.signals.absorption }}></span>
          <span>{LABELS.signals.absorption} (66.7%)</span>
        </div>
      </div>
    </div>
  )
}

function getSampleBiasData(): AgentBias {
  return {
    timestamp: Date.now() / 1000,
    timeframe: '15M',
    total_score: 68.5,
    mode: 'WEAK_BULLISH',
    recommendation: 'CAUTIOUS LONGS - Only at proven S/R levels',
    confidence: 'MEDIUM',
    trend_structure: {
      score: 65,
      ema_trend: 'UP',
      market_structure: 'HH_HL',
      price_vs_sr: 'IN_RANGE',
      details: 'EMA UP (65) | Structure HH_HL (80) | S/R IN_RANGE (50)'
    },
    market_intensity: {
      score: 58,
      rvol: 1.2,
      rvol_contribution: 30,
      vpin: 0.45,
      vpin_contribution: 28,
      is_high_conviction: false,
      details: 'RVOL 1.20x (60) | VPIN 45% (55)'
    },
    orderflow_alpha: {
      score: 72,
      active_mode: 'DELTA_UNWIND',
      primary_score: 45,
      ldr_score: 14,
      obi_score: 8,
      cvd_score: 5,
      active_signals: ['DU+', 'LDR+', 'CVD+'],
      details: 'Mode: DELTA UNWIND | DU 90 | LDR 70 | OBI 55 | CVD 55'
    },
    details: 'Score: 68.5/100 | Mode: WEAK_BULLISH'
  }
}

function getSampleSignals(): OrderflowSignal[] {
  return [
    { timestamp: Date.now() / 1000 - 300, signal_type: 'Delta Unwind', direction: 'BULLISH', price: 21450.25, strength: 0.9, details: 'CVD reversal from extreme' },
    { timestamp: Date.now() / 1000 - 600, signal_type: 'Absorption', direction: 'BULLISH', price: 21420.50, strength: 0.8, details: 'Bid absorption at support' },
    { timestamp: Date.now() / 1000 - 900, signal_type: 'Exhaustion', direction: 'BEARISH', price: 21480.00, strength: 0.7, details: 'High volume, small range' },
  ]
}
