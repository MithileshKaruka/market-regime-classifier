import { useEffect, useRef, useState } from 'react'
import { createChart } from 'lightweight-charts'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import { API_BASE_URL } from '../config'
import './ChartView.css'

interface ChartBar {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  regime: string
  vwap?: number
  rvwap_7?: number
  rvwap_30?: number
  rvwap_90?: number
  rvwap_200?: number
  ema_12?: number
  ema_25?: number
  ema_20?: number
  ema_50?: number
  ema_100?: number
  ema_200?: number
  bb_upper?: number
  bb_middle?: number
  bb_lower?: number
  atr?: number
}

interface SRLevel {
  price: number
  touches: number
  type: string
  volume?: number
}

interface OrderflowSignal {
  timestamp: number
  signal_type: string  // "Absorption", "LSF", "OB Imb"
  direction: string    // "BULLISH" or "BEARISH"
  price: number
  strength: number
  details: string
}

type Timeframe = '5M' | '15M' | '1H' | '4H' | '1D'

interface ChartViewProps {
  timeframe: string
  onTimeframeChange?: (tf: Timeframe) => void
}

const TIMEFRAMES: Timeframe[] = ['5M', '15M', '1H', '4H', '1D']

export default function ChartView({ timeframe, onTimeframeChange }: ChartViewProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candlestickSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const indicatorSeriesRef = useRef<Map<string, ISeriesApi<'Line'>>>(new Map())
  const priceLinesRef = useRef<any[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedIndicators, setSelectedIndicators] = useState<string[]>([])
  const [showIndicatorMenu, setShowIndicatorMenu] = useState(false)
  const [priceRangePct, setPriceRangePct] = useState<number>(10)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [isDarkBackground, setIsDarkBackground] = useState(true)
  const [showOrderflowSignals, setShowOrderflowSignals] = useState(true)
  const [orderflowSignals, setOrderflowSignals] = useState<OrderflowSignal[]>([])
  const chartViewRef = useRef<HTMLDivElement>(null)

  // Lazy loading state
  const [loadedBars, setLoadedBars] = useState<ChartBar[]>([])
  const [totalBars, setTotalBars] = useState<number>(0)
  const [isLoadingMore, setIsLoadingMore] = useState(false)
  const loadedBarsRef = useRef<ChartBar[]>([])  // Ref to avoid stale closure
  const totalBarsRef = useRef<number>(0)
  const isLoadingMoreRef = useRef<boolean>(false)
  const INITIAL_LOAD = 1000  // Initial bars to load
  const LOAD_MORE_SIZE = 500 // Bars to load when scrolling back

  // Keep refs in sync with state
  useEffect(() => {
    loadedBarsRef.current = loadedBars
  }, [loadedBars])
  useEffect(() => {
    totalBarsRef.current = totalBars
  }, [totalBars])
  useEffect(() => {
    isLoadingMoreRef.current = isLoadingMore
  }, [isLoadingMore])

  useEffect(() => {
    if (!chartContainerRef.current) return

    // Create chart
    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { color: '#131a35' },
        textColor: '#9ca3af',
      },
      grid: {
        vertLines: { visible: false },
        horzLines: { visible: false },
      },
      width: chartContainerRef.current.clientWidth,
      height: 500,
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
      rightPriceScale: {
        scaleMargins: {
          top: 0.1,
          bottom: 0.2,
        },
      },
    })

    chartRef.current = chart

    // Add candlestick series
    const candlestickSeries = chart.addCandlestickSeries({
      upColor: '#10b981',
      downColor: '#ef4444',
      borderVisible: false,
      wickUpColor: '#10b981',
      wickDownColor: '#ef4444',
      priceFormat: {
        type: 'price',
        precision: 2,
        minMove: 0.25,
      },
      priceLineVisible: true,
      priceLineColor: '#ffffff',
      priceLineWidth: 1,
      priceLineStyle: 3, // Dashed
    })

    candlestickSeriesRef.current = candlestickSeries

    // Add volume series
    const volumeSeries = chart.addHistogramSeries({
      color: '#6b7280',
      priceFormat: {
        type: 'volume',
      },
      priceScaleId: '',
    })

    volumeSeries.priceScale().applyOptions({
      scaleMargins: {
        top: 0.8,
        bottom: 0,
      },
    })

    volumeSeriesRef.current = volumeSeries

    // Add indicator line series
    const indicators = [
      { key: 'ema_12', color: '#ef4444', title: 'EMA(12)' },       // red (trend fast)
      { key: 'ema_25', color: '#22c55e', title: 'EMA(25)' },       // green (trend slow)
      { key: 'rvwap_7', color: '#f87171', title: 'RVWAP(7)' },     // light red
      { key: 'rvwap_30', color: '#fb923c', title: 'RVWAP(30)' },   // orange
      { key: 'rvwap_90', color: '#38bdf8', title: 'RVWAP(90)' },   // sky blue
      { key: 'rvwap_200', color: '#a78bfa', title: 'RVWAP(200)' }, // purple
      { key: 'ema_20', color: '#fb923c', title: 'EMA(20)' },       // orange
      { key: 'ema_50', color: '#fbbf24', title: 'EMA(50)' },       // yellow
      { key: 'ema_100', color: '#38bdf8', title: 'EMA(100)' },     // sky blue
      { key: 'ema_200', color: '#a78bfa', title: 'EMA(200)' },     // purple
    ]

    indicators.forEach(ind => {
      const series = chart.addLineSeries({
        color: ind.color,
        lineWidth: 2,
        title: ind.title,
        priceLineVisible: false,
        lastValueVisible: false,
        visible: false,  // Start hidden, visibility controlled by selectedIndicators
      })
      indicatorSeriesRef.current.set(ind.key, series)
    })

    // Handle resize
    const handleResize = () => {
      if (chartContainerRef.current && chartRef.current) {
        chartRef.current.applyOptions({
          width: chartContainerRef.current.clientWidth,
        })
      }
    }

    window.addEventListener('resize', handleResize)

    // Cleanup
    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
    }
  }, [])

  // Subscribe to visible range changes for lazy loading
  useEffect(() => {
    if (!chartRef.current) return

    const timeScale = chartRef.current.timeScale()

    const handleVisibleRangeChange = () => {
      const visibleRange = timeScale.getVisibleLogicalRange()
      if (!visibleRange) return

      // If user has scrolled to within 50 bars of the left edge, load more data
      // Use refs to avoid stale closure
      if (visibleRange.from < 50 && loadedBarsRef.current.length < totalBarsRef.current && !isLoadingMoreRef.current) {
        console.log(`[LazyLoad] Near left edge (from: ${visibleRange.from}), triggering load more`)
        loadMoreData()
      }
    }

    timeScale.subscribeVisibleLogicalRangeChange(handleVisibleRangeChange)

    return () => {
      timeScale.unsubscribeVisibleLogicalRangeChange(handleVisibleRangeChange)
    }
  }, [timeframe]) // Only re-subscribe when timeframe changes

  // Helper function to update chart with bars
  const updateChartWithBars = (bars: ChartBar[]) => {
    if (!candlestickSeriesRef.current || !volumeSeriesRef.current) return

    // Sort by time
    const sortedBars = [...bars].sort((a, b) => a.time - b.time)

    // Prepare candlestick data
    const candlestickData = sortedBars.map((bar) => ({
      time: bar.time as any,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }))

    // Prepare volume data
    const volumeData = sortedBars.map((bar) => ({
      time: bar.time as any,
      value: bar.volume,
      color: bar.close >= bar.open ? '#10b98166' : '#ef444466',
    }))

    candlestickSeriesRef.current.setData(candlestickData)
    volumeSeriesRef.current.setData(volumeData)

    // Update indicator series
    const indicatorKeys = ['ema_12', 'ema_25', 'rvwap_7', 'rvwap_30', 'rvwap_90', 'rvwap_200', 'ema_20', 'ema_50', 'ema_100', 'ema_200']
    indicatorKeys.forEach(key => {
      const series = indicatorSeriesRef.current.get(key)
      if (series) {
        const data = sortedBars
          .filter(bar => (bar as any)[key] != null)
          .map(bar => ({
            time: bar.time as any,
            value: (bar as any)[key],
          }))
        series.setData(data)
      }
    })
  }

  // Load more historical data (uses refs to avoid stale closure issues)
  const loadMoreData = async () => {
    if (isLoadingMoreRef.current || loadedBarsRef.current.length >= totalBarsRef.current) return

    setIsLoadingMore(true)
    isLoadingMoreRef.current = true
    try {
      const currentBars = loadedBarsRef.current
      // Offset is simply the number of bars already loaded
      const newOffset = currentBars.length
      // Always fetch all indicators for consistency
      const allIndicators = 'ema_12,ema_25,rvwap_7,rvwap_30,rvwap_90,rvwap_200,ema_20,ema_50,ema_100,ema_200'
      const response = await fetch(
        `${API_BASE_URL}/api/v2/chart/${timeframe}?limit=${LOAD_MORE_SIZE}&offset=${newOffset}&indicators=${allIndicators}`
      )

      if (!response.ok) {
        throw new Error('Failed to fetch more data')
      }

      const data = await response.json()
      const newBars: ChartBar[] = data.bars

      if (newBars.length > 0) {
        // Merge new bars with existing (new bars are older, so prepend)
        const mergedBars = [...newBars, ...currentBars]
        setLoadedBars(mergedBars)
        loadedBarsRef.current = mergedBars
        updateChartWithBars(mergedBars)
        console.log(`[LazyLoad] Loaded ${newBars.length} more bars, total: ${mergedBars.length}/${totalBarsRef.current}`)
      }
    } catch (error) {
      console.error('Error loading more data:', error)
    } finally {
      setIsLoadingMore(false)
      isLoadingMoreRef.current = false
    }
  }

  // Track previous timeframe to detect timeframe changes
  const prevTimeframeRef = useRef<string>(timeframe)

  // Separate effect for indicator visibility (no data fetch needed)
  useEffect(() => {
    // Show/hide indicator series based on selection
    const allIndicatorKeys = ['ema_12', 'ema_25', 'rvwap_7', 'rvwap_30', 'rvwap_90', 'rvwap_200', 'ema_20', 'ema_50', 'ema_100', 'ema_200']
    allIndicatorKeys.forEach(key => {
      const series = indicatorSeriesRef.current.get(key)
      if (series) {
        // Show if selected, hide if not
        series.applyOptions({
          visible: selectedIndicators.includes(key)
        })
      }
    })
  }, [selectedIndicators])

  // Fetch data when timeframe changes OR when we need indicator data we don't have
  useEffect(() => {
    const fetchChartData = async () => {
      if (!candlestickSeriesRef.current || !volumeSeriesRef.current) return

      // Check if this is a timeframe change
      const isTimeframeChange = prevTimeframeRef.current !== timeframe
      prevTimeframeRef.current = timeframe

      try {
        setLoading(true)

        // Always reset on timeframe change
        if (isTimeframeChange) {
          setLoadedBars([])
          loadedBarsRef.current = []
        }

        // Always request all indicators so we have the data available for toggling
        const allIndicators = 'ema_12,ema_25,rvwap_7,rvwap_30,rvwap_90,rvwap_200,ema_20,ema_50,ema_100,ema_200'
        const response = await fetch(`${API_BASE_URL}/api/v2/chart/${timeframe}?limit=${INITIAL_LOAD}&offset=0&indicators=${allIndicators}`)

        if (!response.ok) {
          throw new Error('Failed to fetch chart data')
        }

        const data = await response.json()
        const bars: ChartBar[] = data.bars
        setTotalBars(data.total_count)
        setLoadedBars(bars)
        loadedBarsRef.current = bars

        console.log(`[Chart] Loaded ${bars.length} bars, total available: ${data.total_count}`)

        updateChartWithBars(bars)

        // Fetch S/R levels
        const priceRangeParam = priceRangePct !== 10 ? `?price_range_pct=${priceRangePct}` : ''
        const srResponse = await fetch(`${API_BASE_URL}/api/regime/support-resistance/${timeframe}${priceRangeParam}`)
        if (srResponse.ok) {
          const srData = await srResponse.json()
          console.log('S/R Data received:', srData)

          // Clear old price lines
          priceLinesRef.current.forEach(line => {
            candlestickSeriesRef.current?.removePriceLine(line)
          })
          priceLinesRef.current = []

          // Draw S/R levels as price lines
          if (chartRef.current && candlestickSeriesRef.current) {
            // Support levels (green)
            console.log(`Drawing ${srData.support.length} support levels`)
            srData.support.forEach((level: SRLevel) => {
              const line = candlestickSeriesRef.current?.createPriceLine({
                price: level.price,
                color: '#22c55e',
                lineWidth: 1,
                lineStyle: 2, // Dotted
                axisLabelVisible: true,
                title: `S ${level.touches}`,
              })
              if (line) {
                priceLinesRef.current.push(line)
                console.log(`Drew support at ${level.price}`)
              }
            })

            // Resistance levels (red)
            console.log(`Drawing ${srData.resistance.length} resistance levels`)
            srData.resistance.forEach((level: SRLevel) => {
              const line = candlestickSeriesRef.current?.createPriceLine({
                price: level.price,
                color: '#ef4444',
                lineWidth: 1,
                lineStyle: 2, // Dotted
                axisLabelVisible: true,
                title: `R ${level.touches}`,
              })
              if (line) {
                priceLinesRef.current.push(line)
                console.log(`Drew resistance at ${level.price}`)
              }
            })

            // Volume nodes (POC) - disabled until we have proper volume profile
            // if (srData.volume_nodes) {
            //   console.log(`Drawing ${srData.volume_nodes.length} volume nodes`)
            //   srData.volume_nodes.forEach((level: SRLevel) => {
            //     const line = candlestickSeriesRef.current?.createPriceLine({
            //       price: level.price,
            //       color: '#3b82f6',
            //       lineWidth: 1,
            //       lineStyle: 3, // Dotted
            //       axisLabelVisible: true,
            //       title: 'POC',
            //     })
            //     if (line) {
            //       priceLinesRef.current.push(line)
            //       console.log(`Drew volume node at ${level.price}`)
            //     }
            //   })
            // }

            console.log(`Total price lines drawn: ${priceLinesRef.current.length}`)
          } else {
            console.error('Chart or candlestick series not available')
          }
        } else {
          console.error('Failed to fetch S/R levels:', srResponse.status)
        }

        // Fetch orderflow signals
        try {
          const signalsResponse = await fetch(`${API_BASE_URL}/api/orderflow/signals/${timeframe}?limit=500`)
          if (signalsResponse.ok) {
            const signalsData = await signalsResponse.json()
            setOrderflowSignals(signalsData.signals || [])
            console.log(`[Orderflow] Loaded ${signalsData.signals?.length || 0} signals`)
          }
        } catch (err) {
          console.error('Error fetching orderflow signals:', err)
        }

        // Fit content to view on load
        if (chartRef.current) {
          chartRef.current.timeScale().fitContent()
        }

        setLoading(false)
      } catch (error) {
        console.error('Error fetching chart data:', error)
        setLoading(false)
      }
    }

    fetchChartData()
  }, [timeframe, priceRangePct]) // Only refetch on timeframe or S/R range change, NOT on indicator change

  const availableIndicators = [
    { key: 'trend', label: 'Trend (12/25)', color: '#ef4444', color2: '#22c55e', isCombo: true, keys: ['ema_12', 'ema_25'] },
    { key: 'rvwap_7', label: 'RVWAP(7)', color: '#f87171' },      // light red
    { key: 'rvwap_30', label: 'RVWAP(30)', color: '#fb923c' },    // orange
    { key: 'rvwap_90', label: 'RVWAP(90)', color: '#38bdf8' },    // sky blue
    { key: 'rvwap_200', label: 'RVWAP(200)', color: '#a78bfa' },  // purple
    { key: 'ema_20', label: 'EMA(20)', color: '#fb923c' },        // orange
    { key: 'ema_50', label: 'EMA(50)', color: '#fbbf24' },        // yellow
    { key: 'ema_100', label: 'EMA(100)', color: '#38bdf8' },      // sky blue
    { key: 'ema_200', label: 'EMA(200)', color: '#a78bfa' },      // purple
  ]

  const toggleIndicator = (key: string) => {
    const indicator = availableIndicators.find(ind => ind.key === key)
    if (indicator?.isCombo && indicator.keys) {
      // Handle combo indicator (like Trend which includes ema_12 and ema_25)
      const allSelected = indicator.keys.every(k => selectedIndicators.includes(k))
      if (allSelected) {
        // Remove all keys
        setSelectedIndicators(prev => prev.filter(k => !indicator.keys!.includes(k)))
      } else {
        // Add all keys
        setSelectedIndicators(prev => [...new Set([...prev, ...indicator.keys!])])
      }
    } else {
      // Single indicator
      setSelectedIndicators(prev =>
        prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key]
      )
    }
  }

  const isIndicatorSelected = (ind: typeof availableIndicators[0]) => {
    if (ind.isCombo && ind.keys) {
      return ind.keys.every(k => selectedIndicators.includes(k))
    }
    return selectedIndicators.includes(ind.key)
  }

  // Fullscreen toggle
  const toggleFullscreen = async () => {
    if (!chartViewRef.current) return

    try {
      if (!document.fullscreenElement) {
        await chartViewRef.current.requestFullscreen()
        setIsFullscreen(true)
      } else {
        await document.exitFullscreen()
        setIsFullscreen(false)
      }
    } catch (error) {
      console.error('Fullscreen error:', error)
    }
  }

  // Handle fullscreen change (including ESC key)
  useEffect(() => {
    const resizeChart = (isFullscreen: boolean) => {
      if (chartRef.current && chartContainerRef.current) {
        const newHeight = isFullscreen ? window.innerHeight - 80 : 500
        const newWidth = chartContainerRef.current.clientWidth

        // Force the container to the correct height first
        chartContainerRef.current.style.height = `${newHeight}px`

        chartRef.current.applyOptions({
          width: newWidth,
          height: newHeight,
        })
      }
    }

    const handleFullscreenChange = () => {
      const isNowFullscreen = !!document.fullscreenElement
      setIsFullscreen(isNowFullscreen)

      // When exiting fullscreen, reset container height immediately
      if (!isNowFullscreen && chartContainerRef.current) {
        chartContainerRef.current.style.height = '500px'
      }

      // Resize chart when entering/exiting fullscreen
      // Use multiple timeouts to ensure layout has settled
      resizeChart(isNowFullscreen)
      setTimeout(() => resizeChart(isNowFullscreen), 50)
      setTimeout(() => resizeChart(isNowFullscreen), 150)
      setTimeout(() => resizeChart(isNowFullscreen), 300)
    }

    document.addEventListener('fullscreenchange', handleFullscreenChange)
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange)
  }, [])

  // Handle background color change
  useEffect(() => {
    if (!chartRef.current) return

    const bgColor = isDarkBackground ? '#131a35' : '#ffffff'
    const textColor = isDarkBackground ? '#9ca3af' : '#333333'

    chartRef.current.applyOptions({
      layout: {
        background: { color: bgColor },
        textColor: textColor,
      },
    })
  }, [isDarkBackground])

  // Apply orderflow signal markers to chart
  useEffect(() => {
    if (!candlestickSeriesRef.current) return

    if (!showOrderflowSignals || orderflowSignals.length === 0) {
      // Clear markers when toggled off or no signals
      candlestickSeriesRef.current.setMarkers([])
      return
    }

    // Convert signals to chart markers
    const markers = orderflowSignals.map(signal => {
      // Determine marker appearance based on signal type and direction
      let shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square' = 'circle'
      let color = '#ffffff'
      let text = ''
      let position: 'belowBar' | 'aboveBar' = 'belowBar'

      if (signal.signal_type === 'Absorption') {
        // Absorption: Show the IMPLICATION for price direction
        // BULLISH (bids absorbing aggressive sells) = buyers defending = bullish = green up from below
        // BEARISH (asks absorbing aggressive buys) = sellers defending = bearish = red down from above
        text = 'Abs'
        shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
        color = signal.direction === 'BULLISH' ? '#22c55e' : '#ef4444'
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      } else if (signal.signal_type === 'LSF') {
        // LSF: Shows reversal direction after stop sweep
        text = 'LSF'
        shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
        color = signal.direction === 'BULLISH' ? '#3b82f6' : '#f97316'
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      } else if (signal.signal_type === 'OB Imb') {
        // OBI (Order Book Imbalance): Shows which side has more depth stacked
        // BULLISH = more bids than asks (buying pressure) -> green square below bar
        // BEARISH = more asks than bids (selling pressure) -> red square above bar
        text = 'OBI'
        shape = 'square'
        color = signal.direction === 'BULLISH' ? '#22c55e' : '#ef4444'
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      }

      return {
        time: signal.timestamp as any,
        position: position,
        color: color,
        shape: shape,
        text: text,
        size: 1,
      }
    })

    // Sort markers by time (required by lightweight-charts)
    markers.sort((a, b) => (a.time as number) - (b.time as number))

    candlestickSeriesRef.current.setMarkers(markers as any)
    console.log(`[Orderflow] Applied ${markers.length} markers to chart`)
  }, [orderflowSignals, showOrderflowSignals])

  return (
    <div
      ref={chartViewRef}
      className="chart-view"
    >
      <div className="chart-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <h3 style={{ margin: 0 }}>MNQ</h3>
          {/* Timeframe selector */}
          <div style={{ display: 'flex', gap: '4px' }}>
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                onClick={() => onTimeframeChange?.(tf)}
                style={{
                  padding: '4px 10px',
                  borderRadius: '4px',
                  border: timeframe === tf ? '1px solid #3b82f6' : '1px solid #4b5563',
                  background: timeframe === tf ? '#1e40af' : '#1f2937',
                  color: timeframe === tf ? '#ffffff' : '#9ca3af',
                  cursor: 'pointer',
                  fontSize: '12px',
                  fontWeight: timeframe === tf ? 600 : 400,
                }}
              >
                {tf}
              </button>
            ))}
          </div>
          {isLoadingMore && <span style={{ fontSize: '12px', color: '#6b7280' }}>(loading...)</span>}
        </div>
        <span style={{ fontSize: '11px', color: '#6b7280' }}>
          {loadedBars.length > 0 && `${loadedBars.length.toLocaleString()} / ${totalBars.toLocaleString()} bars`}
        </span>
        <div style={{ display: 'flex', gap: '16px', alignItems: 'center' }}>
          <button
            onClick={() => setIsDarkBackground(!isDarkBackground)}
            style={{
              padding: '4px 12px',
              borderRadius: '4px',
              border: '1px solid #4b5563',
              background: isDarkBackground ? '#1f2937' : '#e5e7eb',
              color: isDarkBackground ? '#e5e7eb' : '#1f2937',
              cursor: 'pointer',
              fontSize: '12px'
            }}
            title="Toggle background color"
          >
            {isDarkBackground ? '☀' : '☾'}
          </button>
          <button
            onClick={toggleFullscreen}
            style={{
              padding: '4px 12px',
              borderRadius: '4px',
              border: '1px solid #4b5563',
              background: '#1f2937',
              color: '#e5e7eb',
              cursor: 'pointer',
              fontSize: '12px'
            }}
            title={isFullscreen ? 'Exit Fullscreen (ESC)' : 'Fullscreen'}
          >
            {isFullscreen ? '⛶ Exit' : '⛶ Fullscreen'}
          </button>
          <button
            onClick={() => setShowOrderflowSignals(!showOrderflowSignals)}
            style={{
              padding: '4px 12px',
              borderRadius: '4px',
              border: showOrderflowSignals ? '1px solid #3b82f6' : '1px solid #4b5563',
              background: showOrderflowSignals ? '#1e40af' : '#1f2937',
              color: showOrderflowSignals ? '#ffffff' : '#9ca3af',
              cursor: 'pointer',
              fontSize: '12px',
              fontWeight: showOrderflowSignals ? 600 : 400,
            }}
            title="Toggle orderflow signals (Absorption, LSF, OBI)"
          >
            Signals {orderflowSignals.length > 0 ? `(${orderflowSignals.length})` : ''}
          </button>
          <div className="chart-legend">
            <span className="legend-item" title="Absorption">
              <span className="legend-color" style={{ backgroundColor: '#22c55e' }}></span>
              Abs
            </span>
            <span className="legend-item" title="Liquidity Sweep Fade">
              <span className="legend-color" style={{ backgroundColor: '#3b82f6' }}></span>
              LSF
            </span>
            <span className="legend-item" title="Order Book Imbalance (green=bid heavy, red=ask heavy)">
              <span className="legend-color" style={{ backgroundColor: '#22c55e' }}></span>
              OBI
            </span>
          </div>
          <div style={{ position: 'relative' }}>
            <button
              onClick={() => setShowIndicatorMenu(!showIndicatorMenu)}
              style={{
                padding: '4px 12px',
                borderRadius: '4px',
                border: '1px solid #4b5563',
                background: '#1f2937',
                color: '#e5e7eb',
                cursor: 'pointer',
                fontSize: '12px'
              }}
            >
              Indicators ({selectedIndicators.length})
            </button>
            {showIndicatorMenu && (
              <div style={{
                position: 'absolute',
                top: '100%',
                right: 0,
                marginTop: '4px',
                background: '#1f2937',
                border: '1px solid #4b5563',
                borderRadius: '4px',
                padding: '8px',
                minWidth: '160px',
                zIndex: 1000,
                boxShadow: '0 4px 6px rgba(0, 0, 0, 0.3)'
              }}>
                <div style={{
                  borderBottom: '1px solid #4b5563',
                  paddingBottom: '8px',
                  marginBottom: '8px'
                }}>
                  <label style={{
                    display: 'flex',
                    flexDirection: 'column',
                    padding: '4px 8px',
                    fontSize: '12px',
                    color: '#e5e7eb',
                    gap: '4px'
                  }}>
                    <span>S/R Range (±%)</span>
                    <input
                      type="number"
                      min="0"
                      max="100"
                      step="5"
                      value={priceRangePct}
                      onChange={(e) => setPriceRangePct(Number(e.target.value))}
                      style={{
                        padding: '4px',
                        borderRadius: '2px',
                        border: '1px solid #4b5563',
                        background: '#131a35',
                        color: '#e5e7eb',
                        fontSize: '12px'
                      }}
                    />
                  </label>
                </div>
                {availableIndicators.map(ind => (
                  <label
                    key={ind.key}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      padding: '4px 8px',
                      cursor: 'pointer',
                      fontSize: '12px',
                      color: '#e5e7eb',
                      gap: '8px'
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={isIndicatorSelected(ind)}
                      onChange={() => toggleIndicator(ind.key)}
                      style={{ cursor: 'pointer' }}
                    />
                    <div style={{ display: 'flex', gap: '2px' }}>
                      <span style={{ width: '12px', height: '12px', background: ind.color, borderRadius: '2px' }}></span>
                      {ind.color2 && <span style={{ width: '12px', height: '12px', background: ind.color2, borderRadius: '2px' }}></span>}
                    </div>
                    <span>{ind.label}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
      {loading && (
        <div style={{
          position: 'absolute',
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -50%)',
          color: '#9ca3af',
          zIndex: 10
        }}>
          Loading chart data...
        </div>
      )}
      <div ref={chartContainerRef} className="chart-canvas"></div>
    </div>
  )
}
