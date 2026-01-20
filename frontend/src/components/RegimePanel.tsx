import { useEffect, useState } from 'react'
import './RegimePanel.css'
import { API_BASE_URL } from '../config'

interface DOMSummary {
  timeframe: string
  dom_imbalance: number
  direction: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
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
  const [domData, setDomData] = useState<DOMSummary[]>([])
  const [recentSignals, setRecentSignals] = useState<OrderflowSignal[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchData()
    // Poll for updates every 5 seconds
    const interval = setInterval(fetchData, 5000)
    return () => clearInterval(interval)
  }, [])

  const fetchData = async () => {
    try {
      // Fetch DOM data
      const metricsResponse = await fetch(`${API_BASE_URL}/api/orderflow/metrics`)
      if (metricsResponse.ok) {
        const metricsData = await metricsResponse.json()
        setDomData(metricsData.dom_by_timeframe || [])
      }

      // Fetch recent signals (from 1H timeframe as representative)
      const signalsResponse = await fetch(`${API_BASE_URL}/api/orderflow/signals/1H?limit=100`)
      if (signalsResponse.ok) {
        const signalsData = await signalsResponse.json()
        // Get only the 5 most recent signals
        const recent = (signalsData.signals || []).slice(-5).reverse()
        setRecentSignals(recent)
      }

      setLoading(false)
    } catch (error) {
      console.error('Error fetching data:', error)
      // Use sample data if API is not available
      setDomData(getSampleDOMData())
      setRecentSignals(getSampleSignals())
      setLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="regime-panel">
        <h3>Orderflow Signals</h3>
        <div className="loading">Loading...</div>
      </div>
    )
  }

  // Calculate overall bias from DOM data
  const bullishCount = domData.filter(d => d.direction === 'BULLISH').length
  const bearishCount = domData.filter(d => d.direction === 'BEARISH').length
  const overallBias = bullishCount > bearishCount ? 'BULLISH' : bearishCount > bullishCount ? 'BEARISH' : 'NEUTRAL'

  return (
    <div className="regime-panel">
      <h3>Orderflow Analysis</h3>

      {/* Overall Bias */}
      <div className="overall-bias">
        <span className="bias-label">Overall Bias:</span>
        <span className={`bias-value bias-${overallBias.toLowerCase()}`}>
          {getDirectionIcon(overallBias)} {overallBias}
        </span>
      </div>

      {/* DOM Alignment Visual */}
      <div className="alignment-section">
        <h4>DOM Alignment</h4>
        <div className="alignment-visual">
          {domData.map((dom) => (
            <div
              key={dom.timeframe}
              className={`alignment-bar alignment-${dom.direction.toLowerCase()}`}
              title={`${dom.timeframe}: ${(dom.dom_imbalance * 100).toFixed(0)}% (${dom.direction})`}
            >
              <span className="alignment-tf">{dom.timeframe}</span>
              <span className="alignment-pct">{(dom.dom_imbalance * 100).toFixed(0)}%</span>
            </div>
          ))}
        </div>
      </div>

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

      {/* Signal Legend */}
      <div className="signal-legend">
        <div className="legend-row">
          <span className="legend-dot" style={{ backgroundColor: '#22c55e' }}></span>
          <span>Absorption - Price defended</span>
        </div>
        <div className="legend-row">
          <span className="legend-dot" style={{ backgroundColor: '#3b82f6' }}></span>
          <span>LSF - Stop sweep reversal</span>
        </div>
        <div className="legend-row">
          <span className="legend-dot" style={{ backgroundColor: '#a855f7' }}></span>
          <span>OBI - Order book imbalance</span>
        </div>
      </div>
    </div>
  )
}

function getDirectionIcon(direction: string): string {
  switch (direction) {
    case 'BULLISH':
      return '▲'
    case 'BEARISH':
      return '▼'
    case 'NEUTRAL':
      return '●'
    default:
      return '●'
  }
}

function getSampleDOMData(): DOMSummary[] {
  return [
    { timeframe: '5M', dom_imbalance: 0.62, direction: 'BULLISH' },
    { timeframe: '15M', dom_imbalance: 0.55, direction: 'NEUTRAL' },
    { timeframe: '1H', dom_imbalance: 0.48, direction: 'NEUTRAL' },
    { timeframe: '4H', dom_imbalance: 0.42, direction: 'BEARISH' },
    { timeframe: '1D', dom_imbalance: 0.58, direction: 'BULLISH' },
  ]
}

function getSampleSignals(): OrderflowSignal[] {
  return [
    { timestamp: Date.now() / 1000 - 300, signal_type: 'Absorption', direction: 'BULLISH', price: 21450.25, strength: 0.8, details: 'Bid absorption at support' },
    { timestamp: Date.now() / 1000 - 600, signal_type: 'LSF', direction: 'BEARISH', price: 21520.50, strength: 0.7, details: 'High sweep then reversal' },
    { timestamp: Date.now() / 1000 - 900, signal_type: 'OB Imb', direction: 'BULLISH', price: 21480.00, strength: 0.6, details: '4x bid imbalance' },
  ]
}
