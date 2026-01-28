import { useEffect, useRef, useState } from 'react'
import { createChart } from 'lightweight-charts'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import {
  API_CONFIG,
  COLORS,
  CHART_CONFIG,
  THRESHOLDS,
  TIMEFRAMES as CONFIG_TIMEFRAMES,
  INDICATORS,
  ALL_INDICATOR_KEYS,
  AVAILABLE_INDICATORS,
  LABELS,
  type Timeframe,
} from '../config'
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

interface ChartViewProps {
  timeframe: string
  onTimeframeChange?: (tf: Timeframe) => void
}

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
  const [priceRangePct, setPriceRangePct] = useState<number>(THRESHOLDS.srRange.default)
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
        background: { color: COLORS.chart.background },
        textColor: COLORS.chart.text,
      },
      grid: {
        vertLines: { visible: false },
        horzLines: { visible: false },
      },
      width: chartContainerRef.current.clientWidth,
      height: CHART_CONFIG.defaultHeight,
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
          top: CHART_CONFIG.priceScaleMargins.top,
          bottom: CHART_CONFIG.priceScaleMargins.bottom,
        },
      },
    })

    chartRef.current = chart

    // Add candlestick series
    const candlestickSeries = chart.addCandlestickSeries({
      upColor: COLORS.bullish,
      downColor: COLORS.bearish,
      borderVisible: false,
      wickUpColor: COLORS.bullish,
      wickDownColor: COLORS.bearish,
      priceFormat: {
        type: 'price',
        precision: CHART_CONFIG.priceFormat.precision,
        minMove: CHART_CONFIG.priceFormat.minMove,
      },
      priceLineVisible: true,
      priceLineColor: COLORS.chart.priceLine,
      priceLineWidth: 1,
      priceLineStyle: 3, // Dashed
    })

    candlestickSeriesRef.current = candlestickSeries

    // Add volume series
    const volumeSeries = chart.addHistogramSeries({
      color: COLORS.neutral,
      priceFormat: {
        type: 'volume',
      },
      priceScaleId: '',
    })

    volumeSeries.priceScale().applyOptions({
      scaleMargins: {
        top: CHART_CONFIG.volumeScaleMargins.top,
        bottom: CHART_CONFIG.volumeScaleMargins.bottom,
      },
    })

    volumeSeriesRef.current = volumeSeries

    // Add indicator line series
    INDICATORS.forEach(ind => {
      const series = chart.addLineSeries({
        color: ind.color,
        lineWidth: CHART_CONFIG.lineWidth,
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

      // If user has scrolled to within threshold bars of the left edge, load more data
      // Use refs to avoid stale closure
      if (visibleRange.from < CHART_CONFIG.scrollThreshold && loadedBarsRef.current.length < totalBarsRef.current && !isLoadingMoreRef.current) {
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
      color: bar.close >= bar.open ? COLORS.chart.volumeUp : COLORS.chart.volumeDown,
    }))

    candlestickSeriesRef.current.setData(candlestickData)
    volumeSeriesRef.current.setData(volumeData)

    // Update indicator series
    ALL_INDICATOR_KEYS.forEach(key => {
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
      const allIndicators = ALL_INDICATOR_KEYS.join(',')
      const response = await fetch(
        `${API_CONFIG.baseUrl}${API_CONFIG.endpoints.chart}/${timeframe}?limit=${CHART_CONFIG.loadMoreSize}&offset=${newOffset}&indicators=${allIndicators}`
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
    ALL_INDICATOR_KEYS.forEach(key => {
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
        const allIndicators = ALL_INDICATOR_KEYS.join(',')
        const priceRangeParam = priceRangePct !== THRESHOLDS.srRange.default ? `?price_range_pct=${priceRangePct}` : ''

        // Fetch ALL data in PARALLEL to prevent visual flash when switching timeframes
        const [chartResponse, signalsResponse, srResponse] = await Promise.all([
          fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.chart}/${timeframe}?limit=${CHART_CONFIG.initialLoad}&offset=0&indicators=${allIndicators}`),
          fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.orderflowSignals}/${timeframe}?limit=${CHART_CONFIG.signalsLimit}`),
          fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.supportResistance}/${timeframe}${priceRangeParam}`)
        ])

        if (!chartResponse.ok) {
          throw new Error('Failed to fetch chart data')
        }

        // Parse all responses before rendering anything
        const data = await chartResponse.json()
        const bars: ChartBar[] = data.bars

        let signals: OrderflowSignal[] = []
        if (signalsResponse.ok) {
          const signalsData = await signalsResponse.json()
          signals = signalsData.signals || []
        }

        let srData = null
        if (srResponse.ok) {
          srData = await srResponse.json()
        }

        console.log(`[Chart] Loaded ${bars.length} bars, ${signals.length} signals, S/R: ${srData ? 'yes' : 'no'}`)

        // Update state
        setTotalBars(data.total_count)
        setLoadedBars(bars)
        loadedBarsRef.current = bars
        setOrderflowSignals(signals)

        // Clear old price lines BEFORE updating chart
        priceLinesRef.current.forEach(line => {
          candlestickSeriesRef.current?.removePriceLine(line)
        })
        priceLinesRef.current = []

        // Render chart with candles
        updateChartWithBars(bars)

        // Apply signals as markers IMMEDIATELY (not via useEffect)
        if (candlestickSeriesRef.current && showOrderflowSignals && signals.length > 0) {
          const markers = signals.map(signal => {
            let shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square' = 'circle'
            let color = '#ffffff'
            let text = ''
            let position: 'belowBar' | 'aboveBar' = 'belowBar'

            if (signal.signal_type === 'Absorption') {
              text = LABELS.signals.absorption.slice(0, 3)
              shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
              color = signal.direction === 'BULLISH' ? COLORS.signals.absorption : COLORS.bearish
              position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
            } else if (signal.signal_type === 'LSF') {
              text = LABELS.signals.lsf
              shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
              color = signal.direction === 'BULLISH' ? COLORS.signals.lsf : COLORS.signals.lsfBearish
              position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
            } else if (signal.signal_type === 'OB Imb') {
              text = LABELS.signals.obi
              shape = 'square'
              color = signal.direction === 'BULLISH' ? COLORS.bullishLight : COLORS.bearish
              position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
            } else if (signal.signal_type === 'Delta Unwind') {
              text = 'DU'
              shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
              color = COLORS.signals.deltaUnwind
              position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
            } else if (signal.signal_type === 'Exhaustion') {
              text = 'EXH'
              shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
              color = COLORS.signals.exhaustion
              position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
            }

            return {
              time: signal.timestamp as any,
              position: position,
              color: color,
              shape: shape,
              text: text,
              size: CHART_CONFIG.markerSize,
            }
          })
          markers.sort((a, b) => (a.time as number) - (b.time as number))
          candlestickSeriesRef.current.setMarkers(markers as any)
        }

        // Draw S/R levels as price lines
        if (srData && chartRef.current && candlestickSeriesRef.current) {
          console.log('S/R Data received:', srData)

          srData.support.forEach((level: SRLevel) => {
            const line = candlestickSeriesRef.current?.createPriceLine({
              price: level.price,
              color: COLORS.chart.support,
              lineWidth: 1,
              lineStyle: CHART_CONFIG.srLineStyle,
              axisLabelVisible: true,
              title: `S ${level.touches}`,
            })
            if (line) priceLinesRef.current.push(line)
          })

          srData.resistance.forEach((level: SRLevel) => {
            const line = candlestickSeriesRef.current?.createPriceLine({
              price: level.price,
              color: COLORS.chart.resistance,
              lineWidth: 1,
              lineStyle: CHART_CONFIG.srLineStyle,
              axisLabelVisible: true,
              title: `R ${level.touches}`,
            })
            if (line) priceLinesRef.current.push(line)
          })

          console.log(`Total price lines drawn: ${priceLinesRef.current.length}`)
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

  const toggleIndicator = (key: string) => {
    const indicator = AVAILABLE_INDICATORS.find(ind => ind.key === key)
    if (indicator && 'isCombo' in indicator && indicator.isCombo && 'keys' in indicator) {
      // Handle combo indicator (like Trend which includes ema_12 and ema_25)
      const keys = indicator.keys as readonly string[]
      const allSelected = keys.every((k: string) => selectedIndicators.includes(k))
      if (allSelected) {
        // Remove all keys
        setSelectedIndicators(prev => prev.filter(k => !keys.includes(k)))
      } else {
        // Add all keys
        setSelectedIndicators(prev => [...new Set([...prev, ...keys])])
      }
    } else {
      // Single indicator
      setSelectedIndicators(prev =>
        prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key]
      )
    }
  }

  const isIndicatorSelected = (ind: typeof AVAILABLE_INDICATORS[number]) => {
    if ('isCombo' in ind && ind.isCombo && 'keys' in ind) {
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
        const newHeight = isFullscreen ? window.innerHeight - CHART_CONFIG.fullscreenHeaderOffset : CHART_CONFIG.defaultHeight
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
        chartContainerRef.current.style.height = `${CHART_CONFIG.defaultHeight}px`
      }

      // Resize chart when entering/exiting fullscreen
      // Use multiple timeouts to ensure layout has settled
      resizeChart(isNowFullscreen)
      CHART_CONFIG.resizeTimeouts.forEach(timeout => {
        setTimeout(() => resizeChart(isNowFullscreen), timeout)
      })
    }

    document.addEventListener('fullscreenchange', handleFullscreenChange)
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange)
  }, [])

  // Handle background color change
  useEffect(() => {
    if (!chartRef.current) return

    const bgColor = isDarkBackground ? COLORS.chart.background : COLORS.chart.backgroundLight
    const textColor = isDarkBackground ? COLORS.chart.text : COLORS.chart.textLight

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
        text = LABELS.signals.absorption.slice(0, 3)
        shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
        color = signal.direction === 'BULLISH' ? COLORS.signals.absorption : COLORS.bearish
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      } else if (signal.signal_type === 'LSF') {
        // LSF: Shows reversal direction after stop sweep
        text = LABELS.signals.lsf
        shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
        color = signal.direction === 'BULLISH' ? COLORS.signals.lsf : COLORS.signals.lsfBearish
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      } else if (signal.signal_type === 'OB Imb') {
        // OBI (Order Book Imbalance): Shows which side has more depth stacked
        // BULLISH = more bids than asks (buying pressure) -> green square below bar
        // BEARISH = more asks than bids (selling pressure) -> red square above bar
        text = LABELS.signals.obi
        shape = 'square'
        color = signal.direction === 'BULLISH' ? COLORS.bullishLight : COLORS.bearish
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      } else if (signal.signal_type === 'Delta Unwind') {
        // Delta Unwind: Cumulative delta reached extreme and is now reversing
        // Trade in direction of the unwind (reversal signal)
        text = 'DU'
        shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
        color = COLORS.signals.deltaUnwind
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      } else if (signal.signal_type === 'Exhaustion') {
        // Exhaustion: High volume with minimal price movement
        // Indicates move is running out of steam (reversal signal)
        text = 'EXH'
        shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
        color = COLORS.signals.exhaustion
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      }

      return {
        time: signal.timestamp as any,
        position: position,
        color: color,
        shape: shape,
        text: text,
        size: CHART_CONFIG.markerSize,
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
          <h3 style={{ margin: 0 }}>{LABELS.symbol}</h3>
          {/* Timeframe selector */}
          <div style={{ display: 'flex', gap: '4px' }}>
            {CONFIG_TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                onClick={() => onTimeframeChange?.(tf)}
                style={{
                  padding: '4px 10px',
                  borderRadius: '4px',
                  border: timeframe === tf ? `1px solid ${COLORS.border.active}` : `1px solid ${COLORS.border.light}`,
                  background: timeframe === tf ? COLORS.background.buttonHover : COLORS.background.button,
                  color: timeframe === tf ? COLORS.text.white : COLORS.text.secondary,
                  cursor: 'pointer',
                  fontSize: '12px',
                  fontWeight: timeframe === tf ? 600 : 400,
                }}
              >
                {tf}
              </button>
            ))}
          </div>
          {isLoadingMore && <span style={{ fontSize: '12px', color: COLORS.text.muted }}>(loading...)</span>}
        </div>
        <span style={{ fontSize: '11px', color: COLORS.text.muted }}>
          {loadedBars.length > 0 && `${loadedBars.length.toLocaleString()} / ${totalBars.toLocaleString()} bars`}
        </span>
        <div style={{ display: 'flex', gap: '16px', alignItems: 'center' }}>
          <button
            onClick={() => setIsDarkBackground(!isDarkBackground)}
            style={{
              padding: '4px 12px',
              borderRadius: '4px',
              border: `1px solid ${COLORS.border.light}`,
              background: isDarkBackground ? COLORS.background.button : COLORS.text.primary,
              color: isDarkBackground ? COLORS.text.primary : COLORS.background.button,
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
              border: `1px solid ${COLORS.border.light}`,
              background: COLORS.background.button,
              color: COLORS.text.primary,
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
              border: showOrderflowSignals ? `1px solid ${COLORS.border.active}` : `1px solid ${COLORS.border.light}`,
              background: showOrderflowSignals ? COLORS.background.buttonHover : COLORS.background.button,
              color: showOrderflowSignals ? COLORS.text.white : COLORS.text.secondary,
              cursor: 'pointer',
              fontSize: '12px',
              fontWeight: showOrderflowSignals ? 600 : 400,
            }}
            title="Toggle orderflow signals (Absorption, LSF, OBI, Delta Unwind, Exhaustion)"
          >
            Signals {orderflowSignals.length > 0 ? `(${orderflowSignals.length})` : ''}
          </button>
          <div className="chart-legend">
            <span className="legend-item" title="Absorption">
              <span className="legend-color" style={{ backgroundColor: COLORS.signals.absorption }}></span>
              {LABELS.signals.absorption.slice(0, 3)}
            </span>
            <span className="legend-item" title="Liquidity Sweep Fade">
              <span className="legend-color" style={{ backgroundColor: COLORS.signals.lsf }}></span>
              {LABELS.signals.lsf}
            </span>
            <span className="legend-item" title="Order Book Imbalance (green=bid heavy, red=ask heavy)">
              <span className="legend-color" style={{ backgroundColor: COLORS.signals.absorption }}></span>
              {LABELS.signals.obi}
            </span>
            <span className="legend-item" title="Delta Unwind - Reversal after extreme delta">
              <span className="legend-color" style={{ backgroundColor: COLORS.signals.deltaUnwind }}></span>
              DU
            </span>
            <span className="legend-item" title="Exhaustion - High volume, minimal price movement">
              <span className="legend-color" style={{ backgroundColor: COLORS.signals.exhaustion }}></span>
              EXH
            </span>
          </div>
          <div style={{ position: 'relative' }}>
            <button
              onClick={() => setShowIndicatorMenu(!showIndicatorMenu)}
              style={{
                padding: '4px 12px',
                borderRadius: '4px',
                border: `1px solid ${COLORS.border.light}`,
                background: COLORS.background.button,
                color: COLORS.text.primary,
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
                background: COLORS.background.button,
                border: `1px solid ${COLORS.border.light}`,
                borderRadius: '4px',
                padding: '8px',
                minWidth: '160px',
                zIndex: 1000,
                boxShadow: '0 4px 6px rgba(0, 0, 0, 0.3)'
              }}>
                <div style={{
                  borderBottom: `1px solid ${COLORS.border.light}`,
                  paddingBottom: '8px',
                  marginBottom: '8px'
                }}>
                  <label style={{
                    display: 'flex',
                    flexDirection: 'column',
                    padding: '4px 8px',
                    fontSize: '12px',
                    color: COLORS.text.primary,
                    gap: '4px'
                  }}>
                    <span>S/R Range (±%)</span>
                    <input
                      type="number"
                      min={THRESHOLDS.srRange.min}
                      max={THRESHOLDS.srRange.max}
                      step={THRESHOLDS.srRange.step}
                      value={priceRangePct}
                      onChange={(e) => setPriceRangePct(Number(e.target.value))}
                      style={{
                        padding: '4px',
                        borderRadius: '2px',
                        border: `1px solid ${COLORS.border.light}`,
                        background: COLORS.background.secondary,
                        color: COLORS.text.primary,
                        fontSize: '12px'
                      }}
                    />
                  </label>
                </div>
                {AVAILABLE_INDICATORS.map(ind => (
                  <label
                    key={ind.key}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      padding: '4px 8px',
                      cursor: 'pointer',
                      fontSize: '12px',
                      color: COLORS.text.primary,
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
                      {'color2' in ind && ind.color2 && <span style={{ width: '12px', height: '12px', background: ind.color2, borderRadius: '2px' }}></span>}
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
          color: COLORS.text.secondary,
          zIndex: 10
        }}>
          Loading chart data...
        </div>
      )}
      <div ref={chartContainerRef} className="chart-canvas"></div>
    </div>
  )
}
