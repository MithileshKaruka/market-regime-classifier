import { useState, useEffect } from 'react'
import './App.css'
import ChartView from './components/ChartView'
import RegimePanel from './components/RegimePanel'
import OrderFlowMetrics from './components/OrderFlowMetrics'
import AdvancedMetricsPanel from './components/AdvancedMetrics'
import { DEFAULT_TIMEFRAME, type Timeframe } from './config'
import { useWebSocketStore } from './stores/webSocketStore'

function App() {
  const [selectedTimeframe, setSelectedTimeframe] = useState<Timeframe>(DEFAULT_TIMEFRAME)
  const { connect } = useWebSocketStore()

  // Initialize WebSocket connection on app mount
  useEffect(() => {
    connect()
  }, [connect])

  return (
    <div className="app">
      <main className="main-content">
        <div className="chart-container">
          <ChartView timeframe={selectedTimeframe} onTimeframeChange={setSelectedTimeframe} />
        </div>

        <div className="metrics-container">
          <OrderFlowMetrics timeframe={selectedTimeframe} />
          <AdvancedMetricsPanel timeframe={selectedTimeframe} />
        </div>

        <div className="regime-container">
          <RegimePanel timeframe={selectedTimeframe} onTimeframeChange={setSelectedTimeframe} />
        </div>
      </main>
    </div>
  )
}

export default App
