import { useEffect, useState } from 'react'
import './AdvancedMetrics.css'
import {
  API_CONFIG,
  COLORS,
  POLLING_INTERVALS,
  THRESHOLDS,
  getBiasColor,
  getAlertColor,
  getToxicityColor,
} from '../config'

interface RVOLData {
  rvol: number
  rvol_20ma: number
  current_volume: number
  poc_price: number
  poc_distance_pct: number
  price_vs_poc: 'ABOVE' | 'BELOW' | 'AT'
  bias: string
  conviction: 'HIGH' | 'MEDIUM' | 'LOW'
  details: string
}

interface VPINData {
  vpin: number
  vpin_threshold: number
  is_elevated: boolean
  toxicity_level: 'LOW' | 'MODERATE' | 'HIGH' | 'EXTREME'
  recent_trend: 'RISING' | 'STABLE' | 'FALLING'
  details: string
}

interface LDRData {
  ldr: number
  total_bid_depth: number
  total_ask_depth: number
  bid_concentration: number
  ask_concentration: number
  support_wall: boolean
  resistance_wall: boolean
  bias: string
  details: string
}

interface AdvancedMetrics {
  timestamp: number
  timeframe: string
  rvol: RVOLData | null
  vpin: VPINData | null
  ldr: LDRData | null
  overall_bias: string
  alert_level: 'NORMAL' | 'ELEVATED' | 'HIGH_ALERT'
}

interface AdvancedMetricsProps {
  timeframe: string
}

export default function AdvancedMetricsPanel({ timeframe }: AdvancedMetricsProps) {
  const [metrics, setMetrics] = useState<AdvancedMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchMetrics()
    const interval = setInterval(fetchMetrics, POLLING_INTERVALS.advancedMetrics)
    return () => clearInterval(interval)
  }, [timeframe])

  const fetchMetrics = async () => {
    try {
      const response = await fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.advancedMetrics}/${timeframe}`)
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }
      const data = await response.json()
      setMetrics(data)
      setError(null)
      setLoading(false)
    } catch (err) {
      console.error('Error fetching advanced metrics:', err)
      setError('Unable to load metrics')
      setLoading(false)
    }
  }

  const getConvictionBadge = (conviction: string) => {
    const colors: Record<string, string> = {
      HIGH: COLORS.bullish,
      MEDIUM: COLORS.alert.elevated,
      LOW: COLORS.neutral,
    }
    return (
      <span className="conviction-badge" style={{ backgroundColor: colors[conviction] || COLORS.neutral }}>
        {conviction}
      </span>
    )
  }

  if (loading) {
    return (
      <div className="advanced-metrics">
        <h3>Advanced Orderflow</h3>
        <div className="loading">Loading...</div>
      </div>
    )
  }

  if (error || !metrics) {
    return (
      <div className="advanced-metrics">
        <h3>Advanced Orderflow</h3>
        <div className="error">{error || 'No data available'}</div>
      </div>
    )
  }

  return (
    <div className="advanced-metrics">
      <div className="metrics-header">
        <h3>Advanced Orderflow</h3>
        <div className="header-badges">
          <span
            className="bias-badge"
            style={{ backgroundColor: getBiasColor(metrics.overall_bias) }}
          >
            {metrics.overall_bias.replace('_', ' ')}
          </span>
          {metrics.alert_level !== 'NORMAL' && (
            <span
              className="alert-badge"
              style={{ backgroundColor: getAlertColor(metrics.alert_level) }}
            >
              {metrics.alert_level.replace('_', ' ')}
            </span>
          )}
        </div>
      </div>

      {/* RVOL Section */}
      {metrics.rvol && (
        <div className="metric-section rvol-section">
          <div className="metric-header">
            <span className="metric-title">RVOL</span>
            <span className="metric-value" style={{ color: metrics.rvol.rvol >= THRESHOLDS.rvol.high ? COLORS.bullish : metrics.rvol.rvol < THRESHOLDS.rvol.low ? COLORS.neutral : COLORS.text.primary }}>
              {metrics.rvol.rvol.toFixed(2)}x
            </span>
            {getConvictionBadge(metrics.rvol.conviction)}
          </div>
          <div className="rvol-bar-container">
            <div className="rvol-bar">
              <div
                className="rvol-fill"
                style={{
                  width: `${Math.min(100, metrics.rvol.rvol * 40)}%`,
                  backgroundColor: metrics.rvol.rvol >= THRESHOLDS.rvol.high ? COLORS.bullish : metrics.rvol.rvol >= THRESHOLDS.rvol.medium ? COLORS.signals.lsf : COLORS.neutral,
                }}
              />
              <div className="rvol-threshold" style={{ left: '60%' }} title="1.5x threshold" />
            </div>
            <div className="rvol-labels">
              <span>0x</span>
              <span>1.5x</span>
              <span>2.5x</span>
            </div>
          </div>
          <div className="poc-info">
            <span className="poc-label">POC: ${metrics.rvol.poc_price.toFixed(2)}</span>
            <span
              className="poc-position"
              style={{ color: getBiasColor(metrics.rvol.bias) }}
            >
              {metrics.rvol.price_vs_poc} ({metrics.rvol.poc_distance_pct > 0 ? '+' : ''}{metrics.rvol.poc_distance_pct.toFixed(2)}%)
            </span>
          </div>
        </div>
      )}

      {/* VPIN Section */}
      {metrics.vpin && (
        <div className={`metric-section vpin-section ${metrics.vpin.is_elevated ? 'elevated' : ''}`}>
          <div className="metric-header">
            <span className="metric-title">VPIN</span>
            <span
              className="metric-value"
              style={{ color: getToxicityColor(metrics.vpin.toxicity_level) }}
            >
              {(metrics.vpin.vpin * 100).toFixed(1)}%
            </span>
            <span
              className="toxicity-badge"
              style={{ backgroundColor: getToxicityColor(metrics.vpin.toxicity_level) }}
            >
              {metrics.vpin.toxicity_level}
            </span>
          </div>
          <div className="vpin-bar-container">
            <div className="vpin-bar">
              <div
                className="vpin-fill"
                style={{
                  width: `${metrics.vpin.vpin * 100}%`,
                  backgroundColor: getToxicityColor(metrics.vpin.toxicity_level),
                }}
              />
              <div className="vpin-threshold" style={{ left: '70%' }} title="Alert threshold" />
            </div>
            <div className="vpin-zones">
              <span className="zone low">Low</span>
              <span className="zone moderate">Mod</span>
              <span className="zone high">High</span>
              <span className="zone extreme">Extreme</span>
            </div>
          </div>
          <div className="vpin-trend">
            <span className="trend-label">Trend:</span>
            <span
              className="trend-value"
              style={{
                color: metrics.vpin.recent_trend === 'RISING' ? COLORS.bearish :
                       metrics.vpin.recent_trend === 'FALLING' ? COLORS.bullish : COLORS.neutral
              }}
            >
              {metrics.vpin.recent_trend === 'RISING' ? '↑' :
               metrics.vpin.recent_trend === 'FALLING' ? '↓' : '→'} {metrics.vpin.recent_trend}
            </span>
          </div>
          {metrics.vpin.is_elevated && (
            <div className="vpin-alert">
              Institutional flow detected
            </div>
          )}
        </div>
      )}

      {/* LDR Section */}
      {metrics.ldr && (
        <div className="metric-section ldr-section">
          <div className="metric-header">
            <span className="metric-title">LDR</span>
            <span
              className="metric-value"
              style={{ color: getBiasColor(metrics.ldr.bias) }}
            >
              {metrics.ldr.ldr.toFixed(2)}:1
            </span>
          </div>
          <div className="ldr-visual">
            <div className="ldr-bar">
              <div
                className="ldr-bid"
                style={{
                  width: `${(metrics.ldr.ldr / (metrics.ldr.ldr + 1)) * 100}%`,
                }}
                title={`Bids: ${metrics.ldr.total_bid_depth.toLocaleString()}`}
              />
              <div
                className="ldr-ask"
                style={{
                  width: `${(1 / (metrics.ldr.ldr + 1)) * 100}%`,
                }}
                title={`Asks: ${metrics.ldr.total_ask_depth.toLocaleString()}`}
              />
            </div>
            <div className="ldr-labels">
              <span style={{ color: COLORS.bullish }}>Bids: {(metrics.ldr.total_bid_depth / 1000).toFixed(1)}K</span>
              <span style={{ color: COLORS.bearish }}>Asks: {(metrics.ldr.total_ask_depth / 1000).toFixed(1)}K</span>
            </div>
          </div>
          {(metrics.ldr.support_wall || metrics.ldr.resistance_wall) && (
            <div className="wall-indicators">
              {metrics.ldr.support_wall && (
                <span className="wall support-wall">Support Wall</span>
              )}
              {metrics.ldr.resistance_wall && (
                <span className="wall resistance-wall">Resistance Wall</span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
