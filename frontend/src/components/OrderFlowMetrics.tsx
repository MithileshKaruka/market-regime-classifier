import { useEffect, useState } from 'react'
import './OrderFlowMetrics.css'
import { API_BASE_URL } from '../config'

interface DOMSummary {
  timeframe: string
  dom_imbalance: number
  direction: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
}

interface VWAPStatus {
  vwap: number
  current_price: number
  position: 'ABOVE' | 'BELOW' | 'AT' | 'UNKNOWN'
  distance_pct: number
}

interface SimplifiedMetrics {
  dom_by_timeframe: DOMSummary[]
  daily_vwap: VWAPStatus
}

interface OrderFlowMetricsProps {
  timeframe: string
}

export default function OrderFlowMetrics({ timeframe }: OrderFlowMetricsProps) {
  const [metrics, setMetrics] = useState<SimplifiedMetrics | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchMetrics()
    // Poll for updates every 2 seconds
    const interval = setInterval(fetchMetrics, 2000)
    return () => clearInterval(interval)
  }, [timeframe])

  const fetchMetrics = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/api/orderflow/metrics`)
      const data = await response.json()
      setMetrics(data)
      setLoading(false)
    } catch (error) {
      console.error('Error fetching metrics:', error)
      // Use sample data if API is not available
      setMetrics(getSampleMetrics())
      setLoading(false)
    }
  }

  if (loading || !metrics) {
    return (
      <div className="orderflow-metrics">
        <h3>Order Flow</h3>
        <div className="loading">Loading...</div>
      </div>
    )
  }

  const vwap = metrics.daily_vwap
  const vwapColor = vwap.position === 'ABOVE' ? '#10b981' : vwap.position === 'BELOW' ? '#ef4444' : '#6b7280'

  return (
    <div className="orderflow-metrics">
      <h3>Order Flow</h3>

      {/* DOM Imbalance by Timeframe */}
      <div className="dom-section">
        <div className="section-label">DOM Imbalance</div>
        <div className="dom-grid">
          {metrics.dom_by_timeframe.map((dom) => {
            const domColor = dom.direction === 'BULLISH' ? '#10b981' : dom.direction === 'BEARISH' ? '#ef4444' : '#6b7280'
            const isSelected = dom.timeframe === timeframe
            return (
              <div
                key={dom.timeframe}
                className={`dom-item ${isSelected ? 'selected' : ''}`}
                style={{ borderColor: isSelected ? domColor : 'transparent' }}
              >
                <div className="dom-tf">{dom.timeframe}</div>
                <div
                  className="dom-bar"
                  style={{
                    background: `linear-gradient(to right, #ef4444 0%, #ef4444 ${(1 - dom.dom_imbalance) * 100}%, #10b981 ${(1 - dom.dom_imbalance) * 100}%, #10b981 100%)`,
                  }}
                >
                  <div
                    className="dom-indicator"
                    style={{ left: `${dom.dom_imbalance * 100}%` }}
                  />
                </div>
                <div className="dom-value" style={{ color: domColor }}>
                  {(dom.dom_imbalance * 100).toFixed(0)}%
                </div>
              </div>
            )
          })}
        </div>
        <div className="dom-legend">
          <span style={{ color: '#ef4444' }}>Ask Heavy</span>
          <span style={{ color: '#6b7280' }}>50%</span>
          <span style={{ color: '#10b981' }}>Bid Heavy</span>
        </div>
      </div>

      {/* Daily VWAP */}
      <div className="vwap-section">
        <div className="section-label">Daily VWAP</div>
        <div className="vwap-display" style={{ borderLeftColor: vwapColor }}>
          <div className="vwap-price">{vwap.vwap.toFixed(2)}</div>
          <div className="vwap-status" style={{ color: vwapColor }}>
            <span className="vwap-position">
              {vwap.position === 'ABOVE' ? '+ ' : vwap.position === 'BELOW' ? '- ' : ''}
              {vwap.distance_pct.toFixed(2)}%
            </span>
            <span className="vwap-direction">
              {vwap.position === 'ABOVE' ? 'ABOVE' : vwap.position === 'BELOW' ? 'BELOW' : 'AT'}
            </span>
          </div>
          <div className="current-price">
            Current: <span style={{ color: vwapColor }}>{vwap.current_price.toFixed(2)}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function getSampleMetrics(): SimplifiedMetrics {
  return {
    dom_by_timeframe: [
      { timeframe: '5M', dom_imbalance: 0.62, direction: 'BULLISH' },
      { timeframe: '15M', dom_imbalance: 0.55, direction: 'NEUTRAL' },
      { timeframe: '1H', dom_imbalance: 0.48, direction: 'NEUTRAL' },
      { timeframe: '4H', dom_imbalance: 0.42, direction: 'BEARISH' },
      { timeframe: '1D', dom_imbalance: 0.58, direction: 'BULLISH' },
    ],
    daily_vwap: {
      vwap: 21450.25,
      current_price: 21525.50,
      position: 'ABOVE',
      distance_pct: 0.35,
    },
  }
}
