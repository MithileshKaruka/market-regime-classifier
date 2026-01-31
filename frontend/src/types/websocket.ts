/**
 * WebSocket message types and interfaces
 * Matches backend protocol from app/api/websocket.py
 */

export type WebSocketMessageType =
  | 'bar_update'
  | 'bar_close'
  | 'signal'
  | 'regime_change'
  | 'large_trade'
  | 'subscribed'
  | 'pong'
  | 'heartbeat'

export interface BarData {
  timestamp: string  // ISO format from backend
  open: number
  high: number
  low: number
  close: number
  volume: number
  instant_delta?: number
  dom_imbalance?: number
  total_bid_depth?: number
  total_ask_depth?: number
  cvd?: number
  tick_count?: number
  // Trade flow metrics (from actual trades, not quote inference)
  trade_flow_ratio?: number  // 0.0=all sells, 1.0=all buys
  buy_trades?: number        // Count of buy aggressor trades
  sell_trades?: number       // Count of sell aggressor trades
  large_trade_count?: number // Count of institutional-sized trades (>=50 contracts)
}

export interface LargeTradeData {
  timestamp: string
  price: number
  size: number
  side: 'A' | 'B'  // 'A'=ask (buy aggressor), 'B'=bid (sell aggressor)
  direction: 'BUY' | 'SELL' | 'UNKNOWN'
  delta: number
}

export interface SignalData {
  timestamp: number
  signal_type: 'Absorption' | 'LSF' | 'OB Imb' | 'Delta Unwind' | 'Exhaustion'
  direction: 'BULLISH' | 'BEARISH'
  price: number
  strength: number
  details: string
}

export interface RegimeData {
  regime: string
  confidence: number
  key_signal: string
  dom_imbalance: number
  delta: number
  timestamp: string
}

export interface WebSocketMessage {
  type: WebSocketMessageType
  timeframe?: string
  symbol?: string
  data?: BarData | SignalData | RegimeData | LargeTradeData
  timestamp?: string
  subscriptions?: string[]
}

export type ConnectionStatus =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'error'
