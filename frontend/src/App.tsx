import { useState, useEffect } from 'react'
import './App.css'
import ChartView from './components/ChartView'
import RegimePanel from './components/RegimePanel'
import OrderFlowMetrics from './components/OrderFlowMetrics'
import AdvancedMetricsPanel from './components/AdvancedMetrics'
import AgentPanel from './components/AgentPanel'
import ConnectionStatus from './components/ConnectionStatus'
import { TIMEFRAMES, DEFAULT_TIMEFRAME, LABELS, type Timeframe } from './config'
import { useWebSocketStore } from './stores/webSocketStore'

function App() {
  const [selectedTimeframe, setSelectedTimeframe] = useState<Timeframe>(DEFAULT_TIMEFRAME)
  const { connect, status } = useWebSocketStore()

  // Initialize WebSocket connection on app mount
  useEffect(() => {
    connect()
  }, [connect])

  return (
    <div className="app">
      <header className="header">
        <h1>{LABELS.appTitle}</h1>
        <ConnectionStatus status={status} />
        <div className="timeframe-selector">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              className={`tf-button ${selectedTimeframe === tf ? 'active' : ''}`}
              onClick={() => setSelectedTimeframe(tf)}
            >
              {tf}
            </button>
          ))}
        </div>
      </header>

      <main className="main-content">
        <div className="chart-container">
          <ChartView timeframe={selectedTimeframe} onTimeframeChange={setSelectedTimeframe} />
        </div>

        <div className="metrics-container">
          <OrderFlowMetrics timeframe={selectedTimeframe} />
          <AdvancedMetricsPanel timeframe={selectedTimeframe} />
        </div>

        <div className="regime-container">
          <RegimePanel />
        </div>

        <div className="agent-container">
          <AgentPanel timeframe={selectedTimeframe} />
        </div>
      </main>
    </div>
  )
}

export default App
