import { useState } from 'react'
import './App.css'
import ChartView from './components/ChartView'
import RegimePanel from './components/RegimePanel'
import OrderFlowMetrics from './components/OrderFlowMetrics'
import AdvancedMetricsPanel from './components/AdvancedMetrics'
import AgentPanel from './components/AgentPanel'

type Timeframe = '5M' | '15M' | '1H' | '4H' | '1D'

function App() {
  const [selectedTimeframe, setSelectedTimeframe] = useState<Timeframe>('1H')

  const timeframes: Timeframe[] = ['5M', '15M', '1H', '4H', '1D']

  return (
    <div className="app">
      <header className="header">
        <h1>MNQ Regime Classifier</h1>
        <div className="timeframe-selector">
          {timeframes.map((tf) => (
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
