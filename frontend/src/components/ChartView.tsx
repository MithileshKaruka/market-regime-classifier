import { useEffect, useRef, useState, useCallback } from 'react'
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
  SYMBOL_CONFIG,
  type Timeframe,
} from '../config'
import { useWebSocket } from '../hooks/useWebSocket'
import type { BarData, SignalData } from '../types/websocket'
import { ZonePrimitive, type ZoneData } from './ZonePrimitive'
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

interface KeyLevel {
  name: string
  short_name: string
  price: number
  timestamp: number
  color: string
}

// Zone interface is imported from ZonePrimitive as ZoneData

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
  const isChartDisposedRef = useRef<boolean>(false)  // Track disposal to prevent updates on removed chart
  const [loading, setLoading] = useState(true)
  const [selectedIndicators, setSelectedIndicators] = useState<string[]>([])
  const [showIndicatorMenu, setShowIndicatorMenu] = useState(false)
  const [priceRangePct, setPriceRangePct] = useState<number>(THRESHOLDS.srRange.default)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [isDarkBackground, setIsDarkBackground] = useState(true)
  const [showOrderflowSignals, setShowOrderflowSignals] = useState(true)
  const [orderflowSignals, setOrderflowSignals] = useState<OrderflowSignal[]>([])
  const [selectedSignalTypes, setSelectedSignalTypes] = useState<string[]>([
    'Absorption', 'LSF', 'OB Imb', 'Delta Unwind', 'Exhaustion', 'Institutional', 'TF Div'
  ])
  const [showSignalMenu, setShowSignalMenu] = useState(false)
  const [showZones, setShowZones] = useState(false)
  const [zones, setZones] = useState<ZoneData[]>([])
  const zonePrimitivesRef = useRef<ZonePrimitive[]>([])  // Track zone primitives
  const [showKeyLevels, setShowKeyLevels] = useState(false)
  const [keyLevels, setKeyLevels] = useState<KeyLevel[]>([])
  const keyLevelLinesRef = useRef<any[]>([])  // Track key level price lines
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

  // ============================================================================
  // Timezone Adjustment Helpers
  // ============================================================================

  // Adjust UTC timestamp to browser's local time for chart display
  // lightweight-charts displays timestamps as-is, so we shift to local time
  // For daily (1D) bars, skip adjustment since they represent full days not specific times
  const adjustToLocal = useCallback((utcTimestamp: number): number => {
    // Skip timezone adjustment for daily bars - they should stay at midnight UTC
    // Otherwise bars appear on the wrong date (e.g., Jan 16 00:00 UTC -> Jan 15 18:00 CST)
    if (timeframe === '1D') {
      return utcTimestamp
    }
    const date = new Date(utcTimestamp * 1000)
    // getTimezoneOffset() returns minutes AHEAD of UTC (so CST=-360 becomes +360)
    // We subtract to shift from UTC to local
    const offsetMinutes = date.getTimezoneOffset()
    return utcTimestamp - (offsetMinutes * 60)
  }, [timeframe])

  // ============================================================================
  // WebSocket Real-time Updates
  // ============================================================================

  // Convert ISO timestamp to Unix seconds for lightweight-charts (local timezone)
  const parseTimestamp = useCallback((isoString: string): number => {
    const date = new Date(isoString)
    const utcSeconds = Math.floor(date.getTime() / 1000)
    return adjustToLocal(utcSeconds)
  }, [adjustToLocal])

  // Handle real-time bar updates from WebSocket
  const handleBarUpdate = useCallback((data: BarData) => {
    // Guard against updates after chart disposal
    if (isChartDisposedRef.current) {
      console.log(`[ChartView] Chart disposed, skipping update`)
      return
    }
    if (!candlestickSeriesRef.current || !volumeSeriesRef.current) {
      console.log(`[ChartView] Series refs not ready`)
      return
    }

    try {
      const time = parseTimestamp(data.timestamp)
      console.log(`[ChartView] Updating chart at time=${time}, close=${data.close}`)

      // Update candlestick (creates new bar or updates existing rightmost bar)
      candlestickSeriesRef.current.update({
        time: time as any,
        open: data.open,
        high: data.high,
        low: data.low,
        close: data.close,
      })

      // Update volume
      volumeSeriesRef.current.update({
        time: time as any,
        value: data.volume,
        color: data.close >= data.open ? COLORS.chart.volumeUp : COLORS.chart.volumeDown,
      })
    } catch (err) {
      // Silently ignore errors from disposed chart or timing issues
      console.log(`[ChartView] Update error (chart may be disposed):`, err)
    }
  }, [parseTimestamp])

  // Handle bar close - finalize the bar and update state
  const handleBarClose = useCallback((data: BarData) => {
    // Update chart one final time (chart uses Chicago-adjusted time)
    handleBarUpdate(data)

    // Store in loadedBars using UTC timestamp (consistent with API data)
    // The timezone adjustment happens only at render time in updateChartWithBars
    const utcTime = Math.floor(new Date(data.timestamp).getTime() / 1000)
    setLoadedBars(prev => {
      const idx = prev.findIndex(b => b.time === utcTime)
      const newBar: ChartBar = {
        time: utcTime,  // Store UTC, display adjustment happens in updateChartWithBars
        open: data.open,
        high: data.high,
        low: data.low,
        close: data.close,
        volume: data.volume,
        regime: '',
      }

      if (idx >= 0) {
        const updated = [...prev]
        updated[idx] = newBar
        return updated
      }
      return [...prev, newBar]
    })
  }, [handleBarUpdate])

  // Handle new signals from WebSocket
  const handleSignal = useCallback((data: SignalData) => {
    if (!showOrderflowSignals) return

    // Add new signal to state - the existing useEffect will handle markers
    setOrderflowSignals(prev => {
      const newSignal: OrderflowSignal = {
        timestamp: data.timestamp,
        signal_type: data.signal_type,
        direction: data.direction,
        price: data.price,
        strength: data.strength,
        details: data.details,
      }
      // Append and sort by timestamp
      return [...prev, newSignal].sort((a, b) => a.timestamp - b.timestamp)
    })
  }, [showOrderflowSignals])

  // Subscribe to WebSocket for real-time updates
  const { isConnected } = useWebSocket({
    timeframe,
    symbol: SYMBOL_CONFIG.backendSymbol,
    onBarUpdate: handleBarUpdate,
    onBarClose: handleBarClose,
    onSignal: handleSignal,
  })

  useEffect(() => {
    if (!chartContainerRef.current) return

    // Reset disposal flag when creating new chart
    isChartDisposedRef.current = false

    // Create chart (timestamps already adjusted to Chicago timezone in parseTimestamp)
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
      // Mark chart as disposed BEFORE removing to prevent race conditions
      isChartDisposedRef.current = true
      window.removeEventListener('resize', handleResize)
      chart.remove()
      // Clear refs to prevent stale references
      chartRef.current = null
      candlestickSeriesRef.current = null
      volumeSeriesRef.current = null
      indicatorSeriesRef.current.clear()
      priceLinesRef.current = []
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

  // Helper function to update chart with bars (applies Chicago timezone adjustment)
  const updateChartWithBars = useCallback((bars: ChartBar[]) => {
    // Guard against updates after chart disposal
    if (isChartDisposedRef.current) return
    if (!candlestickSeriesRef.current || !volumeSeriesRef.current) return

    try {
      // Sort by time
      const sortedBars = [...bars].sort((a, b) => a.time - b.time)

      // Prepare candlestick data (adjust timestamp to Chicago timezone)
      const candlestickData = sortedBars.map((bar) => ({
        time: adjustToLocal(bar.time) as any,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      }))

      // Prepare volume data (adjust timestamp to Chicago timezone)
      const volumeData = sortedBars.map((bar) => ({
        time: adjustToLocal(bar.time) as any,
        value: bar.volume,
        color: bar.close >= bar.open ? COLORS.chart.volumeUp : COLORS.chart.volumeDown,
      }))

      candlestickSeriesRef.current.setData(candlestickData)
      volumeSeriesRef.current.setData(volumeData)

      // Update indicator series (adjust timestamp to Chicago timezone)
      ALL_INDICATOR_KEYS.forEach(key => {
        const series = indicatorSeriesRef.current.get(key)
        if (series) {
          const data = sortedBars
            .filter(bar => (bar as any)[key] != null)
            .map(bar => ({
              time: adjustToLocal(bar.time) as any,
              value: (bar as any)[key],
            }))
          series.setData(data)
        }
      })
    } catch (err) {
      console.log(`[ChartView] setData error (chart may be disposed):`, err)
    }
  }, [adjustToLocal])

  // Load more historical data (uses refs to avoid stale closure issues)
  const loadMoreData = async () => {
    // Guard against loading after chart disposal
    if (isChartDisposedRef.current) return
    if (isLoadingMoreRef.current || loadedBarsRef.current.length >= totalBarsRef.current) return

    setIsLoadingMore(true)
    isLoadingMoreRef.current = true
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 10000) // 10 second timeout

    try {
      const currentBars = loadedBarsRef.current
      // Offset is simply the number of bars already loaded
      const newOffset = currentBars.length
      // Always fetch all indicators for consistency
      const allIndicators = ALL_INDICATOR_KEYS.join(',')
      const response = await fetch(
        `${API_CONFIG.baseUrl}${API_CONFIG.endpoints.chart}/${timeframe}?limit=${CHART_CONFIG.loadMoreSize}&offset=${newOffset}&indicators=${allIndicators}`,
        { signal: controller.signal }
      )
      clearTimeout(timeoutId)

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
      clearTimeout(timeoutId)
      if (error instanceof Error && error.name === 'AbortError') {
        console.warn('Load more data fetch timed out')
      } else {
        console.error('Error loading more data:', error)
      }
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
      // Guard against fetching after chart disposal
      if (isChartDisposedRef.current) return
      if (!candlestickSeriesRef.current || !volumeSeriesRef.current) return

      // Check if this is a timeframe change
      const isTimeframeChange = prevTimeframeRef.current !== timeframe
      prevTimeframeRef.current = timeframe

      const controller = new AbortController()
      const timeoutId = setTimeout(() => controller.abort(), 15000) // 15 second timeout for initial load

      try {
        setLoading(true)

        // Always reset on timeframe change - clear chart immediately to prevent stale data
        if (isTimeframeChange) {
          // Clear chart series data immediately (prevents showing old timeframe data)
          candlestickSeriesRef.current?.setData([])
          volumeSeriesRef.current?.setData([])
          // Clear markers
          candlestickSeriesRef.current?.setMarkers([])
          // Clear indicator series
          indicatorSeriesRef.current.forEach(series => series.setData([]))
          // Clear price lines
          priceLinesRef.current.forEach(line => {
            candlestickSeriesRef.current?.removePriceLine(line)
          })
          priceLinesRef.current = []
          // Clear zone primitives
          zonePrimitivesRef.current.forEach(primitive => {
            candlestickSeriesRef.current?.detachPrimitive(primitive)
          })
          zonePrimitivesRef.current = []
          // Clear key level lines
          keyLevelLinesRef.current.forEach(line => {
            candlestickSeriesRef.current?.removePriceLine(line)
          })
          keyLevelLinesRef.current = []
          // Clear state
          setLoadedBars([])
          loadedBarsRef.current = []
          setZones([])
          setKeyLevels([])
        }

        // Always request all indicators so we have the data available for toggling
        const allIndicators = ALL_INDICATOR_KEYS.join(',')
        const priceRangeParam = priceRangePct !== THRESHOLDS.srRange.default ? `?price_range_pct=${priceRangePct}` : ''

        // Fetch ALL data in PARALLEL to prevent visual flash when switching timeframes
        const [chartResponse, signalsResponse, srResponse, zonesResponse, keyLevelsResponse] = await Promise.all([
          fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.chart}/${timeframe}?limit=${CHART_CONFIG.initialLoad}&offset=0&indicators=${allIndicators}`, { signal: controller.signal }),
          fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.orderflowSignals}/${timeframe}?limit=${CHART_CONFIG.signalsLimit}`, { signal: controller.signal }),
          fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.supportResistance}/${timeframe}${priceRangeParam}`, { signal: controller.signal }),
          fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.zones}/${timeframe}?limit=50`, { signal: controller.signal }),
          fetch(`${API_CONFIG.baseUrl}${API_CONFIG.endpoints.keyLevels}`, { signal: controller.signal })
        ])
        clearTimeout(timeoutId)

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

        let zonesData: ZoneData[] = []
        if (zonesResponse.ok) {
          const zonesJson = await zonesResponse.json()
          zonesData = zonesJson.zones || []
        }

        let keyLevelsData: KeyLevel[] = []
        if (keyLevelsResponse.ok) {
          const keyLevelsJson = await keyLevelsResponse.json()
          keyLevelsData = keyLevelsJson.levels || []
        }

        console.log(`[Chart] Loaded ${bars.length} bars, ${signals.length} signals, S/R: ${srData ? 'yes' : 'no'}, Zones: ${zonesData.length}, KeyLevels: ${keyLevelsData.length}`)

        // Check if chart was disposed during fetch
        if (isChartDisposedRef.current) {
          console.log(`[ChartView] Chart disposed during fetch, aborting render`)
          return
        }

        // Update state
        setTotalBars(data.total_count)
        setLoadedBars(bars)
        loadedBarsRef.current = bars
        setOrderflowSignals(signals)
        setZones(zonesData)
        setKeyLevels(keyLevelsData)

        // Clear old price lines BEFORE updating chart
        priceLinesRef.current.forEach(line => {
          candlestickSeriesRef.current?.removePriceLine(line)
        })
        priceLinesRef.current = []

        // Clear old key level lines
        keyLevelLinesRef.current.forEach(line => {
          candlestickSeriesRef.current?.removePriceLine(line)
        })
        keyLevelLinesRef.current = []

        // Clear old zone primitives
        zonePrimitivesRef.current.forEach(primitive => {
          candlestickSeriesRef.current?.detachPrimitive(primitive)
        })
        zonePrimitivesRef.current = []

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
              time: adjustToLocal(signal.timestamp) as any,
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

        // Draw Key Levels as price lines
        if (showKeyLevels && keyLevelsData.length > 0 && chartRef.current && candlestickSeriesRef.current) {
          console.log('Key Levels received:', keyLevelsData)

          keyLevelsData.forEach((level: KeyLevel) => {
            const line = candlestickSeriesRef.current?.createPriceLine({
              price: level.price,
              color: level.color,
              lineWidth: 1,
              lineStyle: 2,  // Dashed line
              axisLabelVisible: false,  // Don't show price on axis
              title: level.short_name,
            })
            if (line) keyLevelLinesRef.current.push(line)
          })

          console.log(`Key level lines drawn: ${keyLevelLinesRef.current.length}`)
        }

        // Draw Supply/Demand zones as filled rectangles starting from formation time
        // Filter out broken zones - only show UNTESTED and HELD
        if (showZones && zonesData.length > 0 && chartRef.current && candlestickSeriesRef.current) {
          const activeZones = zonesData.filter((z: ZoneData) => z.status !== 'BROKEN')
          console.log(`Drawing ${activeZones.length} active zones (${zonesData.length - activeZones.length} broken filtered)`)
          activeZones.forEach((z: ZoneData) => {
            console.log(`  Zone: ${z.zone_type} ${z.price_low.toFixed(0)}-${z.price_high.toFixed(0)} status=${z.status} Q=${z.quality.toFixed(0)}`)
          })

          activeZones.forEach((zone: ZoneData) => {
            const primitive = new ZonePrimitive(chartRef.current!, candlestickSeriesRef.current!, zone)
            candlestickSeriesRef.current?.attachPrimitive(primitive)
            zonePrimitivesRef.current.push(primitive)
          })

          console.log(`Zone primitives attached: ${zonePrimitivesRef.current.length}`)
        }

        // Fit content to view on load
        if (chartRef.current) {
          chartRef.current.timeScale().fitContent()
        }

        setLoading(false)
      } catch (error) {
        clearTimeout(timeoutId)
        if (error instanceof Error && error.name === 'AbortError') {
          console.warn('Chart data fetch timed out')
        } else {
          console.error('Error fetching chart data:', error)
        }
        setLoading(false)
      }
    }

    fetchChartData()
  }, [timeframe, priceRangePct]) // Only refetch on timeframe or S/R range change, NOT on indicator change

  // Redraw zones when showZones toggle changes
  useEffect(() => {
    if (isChartDisposedRef.current || !candlestickSeriesRef.current || !chartRef.current) return

    // Clear existing zone primitives
    zonePrimitivesRef.current.forEach(primitive => {
      candlestickSeriesRef.current?.detachPrimitive(primitive)
    })
    zonePrimitivesRef.current = []

    // Redraw if enabled - filter out broken zones
    if (showZones && zones.length > 0) {
      const activeZones = zones.filter((z: ZoneData) => z.status !== 'BROKEN')
      activeZones.forEach((zone: ZoneData) => {
        const primitive = new ZonePrimitive(chartRef.current!, candlestickSeriesRef.current!, zone)
        candlestickSeriesRef.current?.attachPrimitive(primitive)
        zonePrimitivesRef.current.push(primitive)
      })
    }
  }, [showZones, zones])

  // Redraw key levels when showKeyLevels toggle changes
  useEffect(() => {
    if (isChartDisposedRef.current || !candlestickSeriesRef.current || !chartRef.current) return

    // Clear existing key level lines
    keyLevelLinesRef.current.forEach(line => {
      candlestickSeriesRef.current?.removePriceLine(line)
    })
    keyLevelLinesRef.current = []

    // Redraw if enabled
    if (showKeyLevels && keyLevels.length > 0) {
      keyLevels.forEach((level: KeyLevel) => {
        const line = candlestickSeriesRef.current?.createPriceLine({
          price: level.price,
          color: level.color,
          lineWidth: 1,
          lineStyle: 2,  // Dashed line
          axisLabelVisible: false,  // Don't show price on axis
          title: level.short_name,
        })
        if (line) keyLevelLinesRef.current.push(line)
      })
    }
  }, [showKeyLevels, keyLevels])

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
    // Guard against updates after chart disposal
    if (isChartDisposedRef.current) return
    if (!candlestickSeriesRef.current) return

    try {
      if (!showOrderflowSignals || orderflowSignals.length === 0 || selectedSignalTypes.length === 0) {
        // Clear markers when toggled off, no signals, or no types selected
        candlestickSeriesRef.current.setMarkers([])
        return
      }

    // Filter signals by selected types and convert to chart markers
    const filteredSignals = orderflowSignals.filter(s => selectedSignalTypes.includes(s.signal_type))
    const markers = filteredSignals.map(signal => {
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
      } else if (signal.signal_type === 'Institutional') {
        // Institutional: Large trades with directional flow (from trades data)
        // Indicates smart money accumulation/distribution
        text = 'INS'
        shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
        color = COLORS.signals.institutional
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      } else if (signal.signal_type === 'TF Div') {
        // Trade Flow Divergence: Trade flow diverges from price (from trades data)
        // Contrarian signal - hidden accumulation/distribution
        text = 'TFD'
        shape = signal.direction === 'BULLISH' ? 'arrowUp' : 'arrowDown'
        color = COLORS.signals.tradeFlowDiv
        position = signal.direction === 'BULLISH' ? 'belowBar' : 'aboveBar'
      }

      return {
        time: adjustToLocal(signal.timestamp) as any,
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
      console.log(`[Orderflow] Applied ${markers.length} markers to chart (${selectedSignalTypes.length} types selected)`)
    } catch (err) {
      console.log(`[ChartView] setMarkers error (chart may be disposed):`, err)
    }
  }, [orderflowSignals, showOrderflowSignals, selectedSignalTypes, adjustToLocal])

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
        {isConnected && (
          <span style={{
            fontSize: '10px',
            color: COLORS.bullish,
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: '4px'
          }}>
            <span style={{ fontSize: '8px' }}>●</span> LIVE
          </span>
        )}
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
            onClick={() => setShowZones(!showZones)}
            style={{
              padding: '4px 12px',
              borderRadius: '4px',
              border: showZones ? `1px solid ${COLORS.border.active}` : `1px solid ${COLORS.border.light}`,
              background: showZones ? COLORS.background.buttonHover : COLORS.background.button,
              color: showZones ? COLORS.text.white : COLORS.text.secondary,
              cursor: 'pointer',
              fontSize: '12px',
              fontWeight: showZones ? 600 : 400,
            }}
            title="Toggle Supply/Demand zones"
          >
            Zones ({zones.length})
          </button>
          <button
            onClick={() => setShowKeyLevels(!showKeyLevels)}
            style={{
              padding: '4px 12px',
              borderRadius: '4px',
              border: showKeyLevels ? `1px solid ${COLORS.border.active}` : `1px solid ${COLORS.border.light}`,
              background: showKeyLevels ? COLORS.background.buttonHover : COLORS.background.button,
              color: showKeyLevels ? COLORS.text.white : COLORS.text.secondary,
              cursor: 'pointer',
              fontSize: '12px',
              fontWeight: showKeyLevels ? 600 : 400,
            }}
            title="Toggle Key Levels (YO, MO, WO, MDAY, PWH/PWL)"
          >
            Key Levels ({keyLevels.length})
          </button>
          <div style={{ position: 'relative' }}>
            <button
              onClick={() => setShowSignalMenu(!showSignalMenu)}
              style={{
                padding: '4px 12px',
                borderRadius: '4px',
                border: showOrderflowSignals && selectedSignalTypes.length > 0 ? `1px solid ${COLORS.border.active}` : `1px solid ${COLORS.border.light}`,
                background: showOrderflowSignals && selectedSignalTypes.length > 0 ? COLORS.background.buttonHover : COLORS.background.button,
                color: showOrderflowSignals && selectedSignalTypes.length > 0 ? COLORS.text.white : COLORS.text.secondary,
                cursor: 'pointer',
                fontSize: '12px',
                fontWeight: showOrderflowSignals && selectedSignalTypes.length > 0 ? 600 : 400,
              }}
              title="Select orderflow signal types to display"
            >
              Signals ({selectedSignalTypes.length}/{orderflowSignals.filter(s => selectedSignalTypes.includes(s.signal_type)).length})
            </button>
            {showSignalMenu && (
              <div style={{
                position: 'absolute',
                top: '100%',
                right: 0,
                marginTop: '4px',
                background: COLORS.background.button,
                border: `1px solid ${COLORS.border.light}`,
                borderRadius: '4px',
                padding: '8px',
                minWidth: '200px',
                zIndex: 1000,
                boxShadow: '0 4px 6px rgba(0, 0, 0, 0.3)'
              }}>
                {/* Master toggle */}
                <div style={{
                  borderBottom: `1px solid ${COLORS.border.light}`,
                  paddingBottom: '8px',
                  marginBottom: '8px'
                }}>
                  <label style={{
                    display: 'flex',
                    alignItems: 'center',
                    padding: '4px 8px',
                    cursor: 'pointer',
                    fontSize: '12px',
                    color: COLORS.text.primary,
                    gap: '8px',
                    fontWeight: 600
                  }}>
                    <input
                      type="checkbox"
                      checked={showOrderflowSignals}
                      onChange={() => setShowOrderflowSignals(!showOrderflowSignals)}
                      style={{ cursor: 'pointer' }}
                    />
                    Show Signals
                  </label>
                </div>
                {/* Signal type checkboxes */}
                {[
                  { type: 'Absorption', label: 'Absorption', color: COLORS.signals.absorption, abbrev: 'ABS' },
                  { type: 'LSF', label: 'Liquidity Sweep Fade', color: COLORS.signals.lsf, abbrev: 'LSF' },
                  { type: 'OB Imb', label: 'Order Book Imbalance', color: COLORS.signals.obi, abbrev: 'OBI' },
                  { type: 'Delta Unwind', label: 'Delta Unwind', color: COLORS.signals.deltaUnwind, abbrev: 'DU' },
                  { type: 'Exhaustion', label: 'Exhaustion', color: COLORS.signals.exhaustion, abbrev: 'EXH' },
                  { type: 'Institutional', label: 'Institutional (trades)', color: COLORS.signals.institutional, abbrev: 'INS' },
                  { type: 'TF Div', label: 'Trade Flow Divergence', color: COLORS.signals.tradeFlowDiv, abbrev: 'TFD' },
                ].map(sig => (
                  <label
                    key={sig.type}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      padding: '4px 8px',
                      cursor: 'pointer',
                      fontSize: '12px',
                      color: COLORS.text.primary,
                      gap: '8px',
                      opacity: showOrderflowSignals ? 1 : 0.5
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={selectedSignalTypes.includes(sig.type)}
                      disabled={!showOrderflowSignals}
                      onChange={() => {
                        setSelectedSignalTypes(prev =>
                          prev.includes(sig.type)
                            ? prev.filter(t => t !== sig.type)
                            : [...prev, sig.type]
                        )
                      }}
                      style={{ cursor: showOrderflowSignals ? 'pointer' : 'not-allowed' }}
                    />
                    <span style={{ width: '12px', height: '12px', background: sig.color, borderRadius: '2px' }}></span>
                    <span>{sig.label}</span>
                    <span style={{ color: COLORS.text.muted, fontSize: '10px' }}>
                      ({orderflowSignals.filter(s => s.signal_type === sig.type).length})
                    </span>
                  </label>
                ))}
                {/* Select/Deselect All */}
                <div style={{
                  borderTop: `1px solid ${COLORS.border.light}`,
                  paddingTop: '8px',
                  marginTop: '8px',
                  display: 'flex',
                  gap: '8px'
                }}>
                  <button
                    onClick={() => setSelectedSignalTypes(['Absorption', 'LSF', 'OB Imb', 'Delta Unwind', 'Exhaustion', 'Institutional', 'TF Div'])}
                    disabled={!showOrderflowSignals}
                    style={{
                      flex: 1,
                      padding: '4px 8px',
                      fontSize: '11px',
                      cursor: showOrderflowSignals ? 'pointer' : 'not-allowed',
                      background: COLORS.background.secondary,
                      border: `1px solid ${COLORS.border.light}`,
                      borderRadius: '2px',
                      color: COLORS.text.secondary
                    }}
                  >
                    Select All
                  </button>
                  <button
                    onClick={() => setSelectedSignalTypes([])}
                    disabled={!showOrderflowSignals}
                    style={{
                      flex: 1,
                      padding: '4px 8px',
                      fontSize: '11px',
                      cursor: showOrderflowSignals ? 'pointer' : 'not-allowed',
                      background: COLORS.background.secondary,
                      border: `1px solid ${COLORS.border.light}`,
                      borderRadius: '2px',
                      color: COLORS.text.secondary
                    }}
                  >
                    Clear All
                  </button>
                </div>
              </div>
            )}
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
