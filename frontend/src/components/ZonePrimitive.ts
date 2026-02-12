/**
 * Custom primitive for drawing Supply/Demand zones as filled rectangles
 * that start from the zone formation time and extend to the right edge.
 */
import type {
  ISeriesPrimitive,
  ISeriesPrimitivePaneView,
  ISeriesPrimitivePaneRenderer,
  SeriesPrimitivePaneViewZOrder,
  Time,
  IChartApi,
  ISeriesApi,
} from 'lightweight-charts'
import { COLORS } from '../config'

export interface ZoneData {
  zone_type: string    // "DEMAND" or "SUPPLY"
  price_low: number
  price_high: number
  formed_at: number    // Unix timestamp (seconds)
  quality: number
  timeframe: string
  status: string       // "UNTESTED", "HELD", or "BROKEN"
  times_tested: number
}

interface ZonePoints {
  x1: number
  x2: number
  y1: number
  y2: number
  isDemand: boolean
  quality: number
  status: string
  times_tested: number
}

class ZonePaneRenderer implements ISeriesPrimitivePaneRenderer {
  private _points: ZonePoints | null

  constructor(points: ZonePoints | null) {
    this._points = points
  }

  draw(target: any): void {
    if (!this._points) return

    const { x1, x2, y1, y2, isDemand, status } = this._points

    // Use useMediaCoordinateSpace for proper rendering
    target.useMediaCoordinateSpace(({ context: ctx }: { context: CanvasRenderingContext2D }) => {
      // Calculate rectangle dimensions
      const left = Math.max(0, x1)
      const right = x2
      const top = Math.min(y1, y2)
      const bottom = Math.max(y1, y2)
      const width = right - left
      const height = bottom - top

      if (width <= 0 || height <= 0) return

      // Zone styling based on status
      const isHeld = status === 'HELD'
      const isBroken = status === 'BROKEN'
      const isReclaimed = status === 'RECLAIMED'

      // Broken zones are very faded, held/reclaimed zones are stronger, untested in between
      // Reclaimed = was broken but price came back, now acting as zone again
      let fillColor: string
      if (isBroken) {
        fillColor = isDemand
          ? 'rgba(34, 197, 94, 0.04)'   // Very faded green
          : 'rgba(239, 68, 68, 0.04)'    // Very faded red
      } else if (isHeld || isReclaimed) {
        fillColor = isDemand
          ? 'rgba(34, 197, 94, 0.15)'   // Light green
          : 'rgba(239, 68, 68, 0.15)'    // Light red
      } else {
        fillColor = isDemand
          ? 'rgba(34, 197, 94, 0.10)'   // Lighter green
          : 'rgba(239, 68, 68, 0.10)'    // Lighter red
      }

      // Fill the zone
      ctx.fillStyle = fillColor
      ctx.fillRect(left, top, width, height)

      // Draw top and bottom border lines (thin for less clutter)
      ctx.strokeStyle = isDemand ? COLORS.zones.demand : COLORS.zones.supply
      ctx.lineWidth = isBroken ? 0.5 : 1

      // Broken zones get dashed lines, reclaimed get dotted, active solid
      if (isBroken) {
        ctx.setLineDash([4, 4])
        ctx.globalAlpha = 0.3
      } else if (isReclaimed) {
        ctx.setLineDash([2, 2])  // Shorter dash for reclaimed
        ctx.globalAlpha = 0.7
      } else {
        ctx.globalAlpha = 0.6
      }

      ctx.beginPath()
      ctx.moveTo(left, top)
      ctx.lineTo(right, top)
      ctx.moveTo(left, bottom)
      ctx.lineTo(right, bottom)
      ctx.stroke()

      // Draw vertical line at zone start
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(left, top)
      ctx.lineTo(left, bottom)
      ctx.stroke()

      // Reset line dash and alpha
      ctx.setLineDash([])
      ctx.globalAlpha = 1.0
    })
  }
}

class ZonePaneView implements ISeriesPrimitivePaneView {
  private _source: ZonePrimitive

  constructor(source: ZonePrimitive) {
    this._source = source
  }

  renderer(): ISeriesPrimitivePaneRenderer {
    return new ZonePaneRenderer(this._source.getPoints())
  }

  zOrder(): SeriesPrimitivePaneViewZOrder {
    return 'bottom' // Draw behind candles
  }
}

export class ZonePrimitive implements ISeriesPrimitive<Time> {
  private _chart: IChartApi
  private _series: ISeriesApi<'Candlestick'>
  private _zone: ZoneData
  private _paneViews: ISeriesPrimitivePaneView[]
  private _adjustedTime: number  // Cached adjusted timestamp

  constructor(chart: IChartApi, series: ISeriesApi<'Candlestick'>, zone: ZoneData) {
    this._chart = chart
    this._series = series
    this._zone = zone
    this._paneViews = [new ZonePaneView(this)]
    // Pre-calculate adjusted time once (doesn't change)
    this._adjustedTime = this._adjustToLocal(zone.formed_at)
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._paneViews
  }

  // Adjust UTC timestamp to local time (same as chart bars)
  // Skip for 1D since daily bars stay at midnight UTC
  private _adjustToLocal(utcTimestamp: number): number {
    if (this._zone.timeframe === '1D') {
      return utcTimestamp
    }
    const date = new Date(utcTimestamp * 1000)
    const offsetMinutes = date.getTimezoneOffset()
    return utcTimestamp - (offsetMinutes * 60)
  }

  getPoints(): ZonePoints | null {
    const timeScale = this._chart.timeScale()

    // Use cached adjusted time
    const x1 = timeScale.timeToCoordinate(this._adjustedTime as Time)

    // Zone timestamp is outside the visible chart range - silently return null
    // This is expected for older zones when chart is zoomed in to recent data
    if (x1 === null) {
      return null
    }

    // Get chart width for right edge
    const chartWidth = this._chart.timeScale().width()

    // Convert prices to coordinates
    const y1 = this._series.priceToCoordinate(this._zone.price_high)
    const y2 = this._series.priceToCoordinate(this._zone.price_low)

    if (y1 === null || y2 === null) return null

    return {
      x1,
      x2: chartWidth,
      y1,
      y2,
      isDemand: this._zone.zone_type === 'DEMAND',
      quality: this._zone.quality,
      status: this._zone.status || 'UNTESTED',
      times_tested: this._zone.times_tested || 0,
    }
  }

  updateAllViews(): void {
    (this._paneViews as ZonePaneView[]) = [new ZonePaneView(this)]
  }

  attached(): void {
    // Called when primitive is attached to the series
  }

  detached(): void {
    // Called when primitive is detached from the series
  }

  // Return null to prevent zones from affecting auto-scale
  // This fixes scroll jankiness when zones are enabled - let candles drive the scale
  autoscaleInfo() {
    return null
  }
}
