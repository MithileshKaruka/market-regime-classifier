/**
 * Custom hook for WebSocket subscriptions
 * Provides component-level subscription management with automatic cleanup
 */

import { useEffect, useRef } from 'react'
import { useWebSocketStore } from '../stores/webSocketStore'
import type { BarData, SignalData, RegimeData } from '../types/websocket'
import { SYMBOL_CONFIG } from '../config'

interface UseWebSocketOptions {
  timeframe: string
  symbol?: string
  onBarUpdate?: (data: BarData) => void
  onBarClose?: (data: BarData) => void
  onSignal?: (data: SignalData) => void
  onRegimeChange?: (data: RegimeData) => void
}

export function useWebSocket(options: UseWebSocketOptions) {
  const {
    timeframe,
    symbol = SYMBOL_CONFIG.backendSymbol,
    onBarUpdate,
    onBarClose,
    onSignal,
    onRegimeChange,
  } = options

  const { status, lastMessage, subscribe, unsubscribe, connect } = useWebSocketStore()

  // Store callbacks in refs to avoid re-subscriptions when callbacks change
  const callbacksRef = useRef({ onBarUpdate, onBarClose, onSignal, onRegimeChange })
  callbacksRef.current = { onBarUpdate, onBarClose, onSignal, onRegimeChange }

  const subscriptionKey = `${timeframe}:${symbol}`

  // Subscribe on mount, unsubscribe on unmount or when subscription changes
  useEffect(() => {
    subscribe(subscriptionKey)

    return () => {
      unsubscribe(subscriptionKey)
    }
  }, [subscriptionKey, subscribe, unsubscribe])

  // Handle incoming messages
  useEffect(() => {
    if (!lastMessage) return

    const { type, timeframe: msgTimeframe, symbol: msgSymbol, data } = lastMessage

    // Debug: log all bar messages
    if (type === 'bar_update' || type === 'bar_close') {
      console.log(`[useWebSocket] Received ${type}: tf=${msgTimeframe} sym=${msgSymbol}, expecting tf=${timeframe} sym=${symbol}`)
    }

    // Only process messages for our subscription
    if (msgTimeframe !== timeframe || msgSymbol !== symbol) {
      if (type === 'bar_update' || type === 'bar_close') {
        console.log(`[useWebSocket] FILTERED OUT - mismatch`)
      }
      return
    }

    console.log(`[useWebSocket] Processing ${type} for ${timeframe}:${symbol}`)
    const callbacks = callbacksRef.current

    switch (type) {
      case 'bar_update':
        callbacks.onBarUpdate?.(data as BarData)
        break
      case 'bar_close':
        callbacks.onBarClose?.(data as BarData)
        break
      case 'signal':
        callbacks.onSignal?.(data as SignalData)
        break
      case 'regime_change':
        callbacks.onRegimeChange?.(data as RegimeData)
        break
    }
  }, [lastMessage, timeframe, symbol])

  return {
    status,
    isConnected: status === 'connected',
    isReconnecting: status === 'reconnecting',
    connect,
  }
}
