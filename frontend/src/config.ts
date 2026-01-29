// Frontend Configuration
// All configurable values are centralized here for easy maintenance

// ============================================================================
// API Configuration
// ============================================================================
// Use relative URLs in production (empty string) to go through nginx proxy
// This avoids CORS issues and corporate proxy blocks on non-standard ports
const getApiBaseUrl = () => {
  const envUrl = import.meta.env.VITE_API_URL
  // Empty string or "/" means use relative URLs (same origin)
  if (envUrl === '' || envUrl === '/') return ''
  // Explicit URL provided
  if (envUrl) return envUrl
  // Default for local development
  return 'http://127.0.0.1:8000'
}

export const API_CONFIG = {
  baseUrl: getApiBaseUrl(),
  endpoints: {
    chart: '/api/v2/chart',
    supportResistance: '/api/regime/support-resistance',
    orderflowSignals: '/api/orderflow/signals',
    metrics: '/api/orderflow/metrics',
    advancedMetrics: '/api/orderflow/advanced',
    agentDecision: '/api/orderflow/agent',
    agentBias: '/api/orderflow/agent-bias',
  },
} as const

// Legacy export for backward compatibility
export const API_BASE_URL = API_CONFIG.baseUrl

// ============================================================================
// WebSocket Configuration
// ============================================================================
export const WEBSOCKET_CONFIG = {
  path: '/ws/live',
  reconnectAttempts: 5,
  reconnectDelayBase: 1000,  // ms, doubles each attempt
  heartbeatInterval: 25000,  // ms, server sends at 30s
} as const

// ============================================================================
// Symbol Configuration
// ============================================================================
export const SYMBOL_CONFIG = {
  displaySymbol: 'MNQ',
  backendSymbol: 'MNQ.FUT',
} as const

export function toBackendSymbol(display: string): string {
  return `${display}.FUT`
}

export function toDisplaySymbol(backend: string): string {
  return backend.replace('.FUT', '')
}

// ============================================================================
// Color Palette
// ============================================================================
export const COLORS = {
  // Primary sentiment colors
  bullish: '#10b981',
  bullishLight: '#22c55e',
  bullishLighter: '#86efac',
  bullishTransparent: '#10b98166',
  bearish: '#ef4444',
  bearishLight: '#f87171',
  bearishLighter: '#fca5a5',
  bearishTransparent: '#ef444466',
  neutral: '#6b7280',
  neutralLight: '#9ca3af',

  // UI colors
  background: {
    primary: '#0a0e27',
    secondary: '#131a35',
    tertiary: '#1a2138',
    button: '#1f2937',
    buttonHover: '#1e40af',
  },
  text: {
    primary: '#e8e9ed',
    secondary: '#9ca3af',
    muted: '#6b7280',
    white: '#ffffff',
  },
  border: {
    default: '#2d3748',
    light: '#4b5563',
    active: '#3b82f6',
  },

  // Chart colors
  chart: {
    background: '#131a35',
    backgroundLight: '#ffffff',
    text: '#9ca3af',
    textLight: '#333333',
    priceLine: '#ffffff',
    support: '#22c55e',
    resistance: '#ef4444',
    volumeUp: '#10b98166',
    volumeDown: '#ef444466',
  },

  // Indicator colors
  indicators: {
    ema12: '#ef4444',
    ema25: '#22c55e',
    ema20: '#fb923c',
    ema50: '#fbbf24',
    ema100: '#38bdf8',
    ema200: '#a78bfa',
    rvwap7: '#f87171',
    rvwap30: '#fb923c',
    rvwap90: '#38bdf8',
    rvwap200: '#a78bfa',
  },

  // Signal colors
  signals: {
    absorption: '#22c55e',
    lsf: '#3b82f6',
    lsfBearish: '#f97316',
    obi: '#a855f7',
    deltaUnwind: '#f59e0b',      // Amber - reversal signal
    exhaustion: '#ec4899',       // Pink - exhaustion signal
  },

  // Agent mode colors
  agentModes: {
    HIGH_BULLISH: '#22c55e',
    WEAK_BULLISH: '#86efac',
    NEUTRAL: '#fbbf24',
    WEAK_BEARISH: '#fca5a5',
    HIGH_BEARISH: '#ef4444',
    default: '#94a3b8',
  },

  // Alert/status colors
  alert: {
    normal: '#6b7280',
    elevated: '#f59e0b',
    high: '#ef4444',
  },

  // Toxicity colors (VPIN)
  toxicity: {
    low: '#10b981',
    moderate: '#eab308',
    high: '#f59e0b',
    extreme: '#ef4444',
  },
} as const

// ============================================================================
// Polling Intervals (milliseconds)
// ============================================================================
export const POLLING_INTERVALS = {
  orderflowMetrics: 2000,
  domSignals: 5000,
  advancedMetrics: 5000,
} as const

// ============================================================================
// Chart Configuration
// ============================================================================
export const CHART_CONFIG = {
  // Lazy loading
  initialLoad: 1000,
  loadMoreSize: 500,
  scrollThreshold: 50,

  // Dimensions
  defaultHeight: 500,
  fullscreenHeaderOffset: 80,
  lineWidth: 2,

  // Price scale margins
  priceScaleMargins: {
    top: 0.1,
    bottom: 0.2,
  },

  // Volume scale margins
  volumeScaleMargins: {
    top: 0.8,
    bottom: 0,
  },

  // Price format
  priceFormat: {
    precision: 2,
    minMove: 0.25,
  },

  // S/R line style (2 = dotted)
  srLineStyle: 2,

  // Resize timeouts for fullscreen transitions
  resizeTimeouts: [50, 150, 300],

  // Signal marker size
  markerSize: 1,

  // Signals fetch limit
  signalsLimit: 500,
} as const

// ============================================================================
// Thresholds
// ============================================================================
export const THRESHOLDS = {
  // RVOL thresholds
  rvol: {
    high: 1.5,
    low: 0.5,
    medium: 1.0,
    barWidthMultiplier: 40,
  },

  // VPIN threshold
  vpin: {
    alertThreshold: 0.7,
  },

  // Score thresholds for color bands
  score: {
    highBearish: 30,
    weakBearish: 45,
    neutral: 55,
    weakBullish: 70,
    midpoint: 50,
  },

  // S/R price range
  srRange: {
    default: 10,
    min: 0,
    max: 100,
    step: 5,
  },

  // Signal limits
  signals: {
    recentCount: 5,
    fetchLimit: 100,
  },
} as const

// ============================================================================
// Timeframes
// ============================================================================
export const TIMEFRAMES = ['5M', '15M', '1H', '4H', '1D'] as const
export type Timeframe = typeof TIMEFRAMES[number]

export const DEFAULT_TIMEFRAME: Timeframe = '1H'

// ============================================================================
// Indicator Definitions
// ============================================================================
export const INDICATORS = [
  { key: 'ema_12', color: COLORS.indicators.ema12, title: 'EMA(12)' },
  { key: 'ema_25', color: COLORS.indicators.ema25, title: 'EMA(25)' },
  { key: 'rvwap_7', color: COLORS.indicators.rvwap7, title: 'RVWAP(7)' },
  { key: 'rvwap_30', color: COLORS.indicators.rvwap30, title: 'RVWAP(30)' },
  { key: 'rvwap_90', color: COLORS.indicators.rvwap90, title: 'RVWAP(90)' },
  { key: 'rvwap_200', color: COLORS.indicators.rvwap200, title: 'RVWAP(200)' },
  { key: 'ema_20', color: COLORS.indicators.ema20, title: 'EMA(20)' },
  { key: 'ema_50', color: COLORS.indicators.ema50, title: 'EMA(50)' },
  { key: 'ema_100', color: COLORS.indicators.ema100, title: 'EMA(100)' },
  { key: 'ema_200', color: COLORS.indicators.ema200, title: 'EMA(200)' },
] as const

export const ALL_INDICATOR_KEYS = INDICATORS.map(i => i.key)

export const AVAILABLE_INDICATORS = [
  { key: 'trend', label: 'Trend (12/25)', color: COLORS.indicators.ema12, color2: COLORS.indicators.ema25, isCombo: true, keys: ['ema_12', 'ema_25'] },
  { key: 'rvwap_7', label: 'RVWAP(7)', color: COLORS.indicators.rvwap7 },
  { key: 'rvwap_30', label: 'RVWAP(30)', color: COLORS.indicators.rvwap30 },
  { key: 'rvwap_90', label: 'RVWAP(90)', color: COLORS.indicators.rvwap90 },
  { key: 'rvwap_200', label: 'RVWAP(200)', color: COLORS.indicators.rvwap200 },
  { key: 'ema_20', label: 'EMA(20)', color: COLORS.indicators.ema20 },
  { key: 'ema_50', label: 'EMA(50)', color: COLORS.indicators.ema50 },
  { key: 'ema_100', label: 'EMA(100)', color: COLORS.indicators.ema100 },
  { key: 'ema_200', label: 'EMA(200)', color: COLORS.indicators.ema200 },
] as const

// ============================================================================
// Component Score Weights (for display)
// ============================================================================
export const SCORE_WEIGHTS = {
  trend: { weight: 20, label: 'Trend' },
  intensity: { weight: 30, label: 'Intensity' },
  orderflow: { weight: 50, label: 'Orderflow' },
} as const

// ============================================================================
// UI Labels
// ============================================================================
export const LABELS = {
  appTitle: 'MNQ Regime Classifier',
  symbol: 'MNQ',
  panels: {
    orderFlow: 'Order Flow',
    advancedOrderflow: 'Advanced Orderflow',
    tradingAgent: 'Trading Agent',
    orderflowAnalysis: 'Orderflow Analysis',
    orderflowSignals: 'Orderflow Signals',
  },
  signals: {
    absorption: 'Absorption',
    lsf: 'LSF',
    obi: 'OBI',
    deltaUnwind: 'Delta Unwind',
    exhaustion: 'Exhaustion',
  },
  directions: {
    bullish: 'BULLISH',
    bearish: 'BEARISH',
    neutral: 'NEUTRAL',
  },
} as const

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Get color for agent mode
 */
export function getModeColor(mode: string): string {
  return COLORS.agentModes[mode as keyof typeof COLORS.agentModes] || COLORS.agentModes.default
}

/**
 * Get color for trading action
 */
export function getActionColor(action: string): string {
  if (action.includes('LONG') && !action.includes('EXIT')) return COLORS.bullishLight
  if (action.includes('SHORT') && !action.includes('EXIT')) return COLORS.bearish
  if (action.includes('EXIT')) return COLORS.signals.lsfBearish
  return COLORS.agentModes.default
}

/**
 * Get color based on bias direction
 */
export function getBiasColor(bias: string): string {
  if (bias.includes('BULLISH')) return COLORS.bullish
  if (bias.includes('BEARISH')) return COLORS.bearish
  return COLORS.neutral
}

/**
 * Get color for alert level
 */
export function getAlertColor(level: string): string {
  if (level === 'HIGH_ALERT') return COLORS.alert.high
  if (level === 'ELEVATED') return COLORS.alert.elevated
  return COLORS.alert.normal
}

/**
 * Get color for VPIN toxicity level
 */
export function getToxicityColor(level: string): string {
  return COLORS.toxicity[level.toLowerCase() as keyof typeof COLORS.toxicity] || COLORS.toxicity.low
}

/**
 * Get score gradient background for bias gauge
 */
export function getScoreGradient(score: number): string {
  const { highBearish, weakBearish, neutral, weakBullish } = THRESHOLDS.score
  const bgColor = COLORS.background.tertiary

  if (score <= highBearish) return `linear-gradient(90deg, ${COLORS.bearish} ${score}%, ${bgColor} ${score}%)`
  if (score <= weakBearish) return `linear-gradient(90deg, ${COLORS.signals.lsfBearish} ${score}%, ${bgColor} ${score}%)`
  if (score <= neutral) return `linear-gradient(90deg, ${COLORS.agentModes.NEUTRAL} ${score}%, ${bgColor} ${score}%)`
  if (score <= weakBullish) return `linear-gradient(90deg, ${COLORS.bullishLighter} ${score}%, ${bgColor} ${score}%)`
  return `linear-gradient(90deg, ${COLORS.bullishLight} ${score}%, ${bgColor} ${score}%)`
}

/**
 * Get direction icon
 */
export function getDirectionIcon(direction: string): string {
  switch (direction) {
    case 'BULLISH': return '\u25B2'
    case 'BEARISH': return '\u25BC'
    case 'NEUTRAL':
    default: return '\u25CF'
  }
}
