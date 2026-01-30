import { useEffect, useState, useCallback, useRef } from 'react'
import './OrderFlowMetrics.css'
import { API_CONFIG, COLORS, POLLING_INTERVALS, LABELS, SYMBOL_CONFIG } from '../config'
import { useWebSocket } from '../hooks/useWebSocket'

// Throttle interval for bar_update refreshes (ms)
const UPDATE_THROTTLE_MS = 5000

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
  const lastFetchTimeRef = useRef<number>(0)

  const fetchMetrics = useCallback(async () => {
    lastFetchTimeRef.current = Date.now()
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 10000) // 10 second timeout

    try {
      const response = await fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.metrics}`, {
        signal: controller.signal
      })
      clearTimeout(timeoutId)
      const data = await response.json()
      setMetrics(data)
      setLoading(false)
    } catch (error) {
      clearTimeout(timeoutId)
      if (error instanceof Error && error.name === 'AbortError') {
        console.warn('Metrics fetch timed out')
      } else {
        console.error('Error fetching metrics:', error)
      }
      // Use sample data if API is not available
      setMetrics(getSampleMetrics())
      setLoading(false)
    }
  }, [])

  // Throttled fetch for bar_update events (don't flood API on every tick)
  const throttledFetch = useCallback(() => {
    const now = Date.now()
    if (now - lastFetchTimeRef.current >= UPDATE_THROTTLE_MS) {
      fetchMetrics()
    }
  }, [fetchMetrics])

  // Subscribe to WebSocket - refresh metrics on bar update (throttled) and bar close
  useWebSocket({
    timeframe,
    symbol: SYMBOL_CONFIG.backendSymbol,
    onBarUpdate: throttledFetch,
    onBarClose: fetchMetrics,
  })

  useEffect(() => {
    fetchMetrics()
    // Keep polling as fallback (longer interval since WebSocket handles real-time)
    const interval = setInterval(fetchMetrics, POLLING_INTERVALS.orderflowMetrics * 5)
    return () => clearInterval(interval)
  }, [timeframe, fetchMetrics])

  if (loading || !metrics) {
    return (
      <div className="orderflow-metrics">
        <h3>Order Flow</h3>
        <div className="loading">Loading...</div>
      </div>
    )
  }

  const vwap = metrics.daily_vwap
  const vwapColor = vwap.position === 'ABOVE' ? COLORS.bullish : vwap.position === 'BELOW' ? COLORS.bearish : COLORS.neutral

  return (
    <div className="orderflow-metrics">
      <h3>{LABELS.panels.orderFlow}</h3>

      {/* DOM Imbalance by Timeframe */}
      <div className="dom-section">
        <div className="section-label">DOM Imbalance</div>
        <div className="dom-grid">
          {metrics.dom_by_timeframe.map((dom) => {
            const domColor = dom.direction === 'BULLISH' ? COLORS.bullish : dom.direction === 'BEARISH' ? COLORS.bearish : COLORS.neutral
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
                    background: `linear-gradient(to right, ${COLORS.bearish} 0%, ${COLORS.bearish} ${(1 - dom.dom_imbalance) * 100}%, ${COLORS.bullish} ${(1 - dom.dom_imbalance) * 100}%, ${COLORS.bullish} 100%)`,
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
          <span style={{ color: COLORS.bearish }}>Ask Heavy</span>
          <span style={{ color: COLORS.neutral }}>50%</span>
          <span style={{ color: COLORS.bullish }}>Bid Heavy</span>
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
