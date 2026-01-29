/**
 * Zustand store for WebSocket connection management
 * Provides singleton connection with auto-reconnect and subscription handling
 */

import { create } from 'zustand'
import type { ConnectionStatus, WebSocketMessage } from '../types/websocket'
import { API_CONFIG, WEBSOCKET_CONFIG } from '../config'

interface WebSocketState {
  status: ConnectionStatus
  socket: WebSocket | null
  subscriptions: Set<string>
  lastMessage: WebSocketMessage | null
  error: string | null
  reconnectAttempts: number
}

interface WebSocketActions {
  connect: () => void
  disconnect: () => void
  subscribe: (subscriptionKey: string) => void
  unsubscribe: (subscriptionKey: string) => void
  sendPing: () => void
}

interface WebSocketStore extends WebSocketState, WebSocketActions {}

// Timer refs stored outside store to avoid serialization issues
let reconnectTimeout: ReturnType<typeof setTimeout> | null = null
let heartbeatInterval: ReturnType<typeof setInterval> | null = null

const clearTimers = () => {
  if (reconnectTimeout) {
    clearTimeout(reconnectTimeout)
    reconnectTimeout = null
  }
  if (heartbeatInterval) {
    clearInterval(heartbeatInterval)
    heartbeatInterval = null
  }
}

export const useWebSocketStore = create<WebSocketStore>((set, get) => {
  const scheduleReconnect = () => {
    const { reconnectAttempts } = get()
    if (reconnectAttempts >= WEBSOCKET_CONFIG.reconnectAttempts) {
      set({ status: 'error', error: 'Max reconnection attempts reached' })
      return
    }

    const delay = WEBSOCKET_CONFIG.reconnectDelayBase * Math.pow(2, reconnectAttempts)
    set({ status: 'reconnecting', reconnectAttempts: reconnectAttempts + 1 })

    reconnectTimeout = setTimeout(() => {
      get().connect()
    }, delay)
  }

  const startHeartbeat = () => {
    heartbeatInterval = setInterval(() => {
      get().sendPing()
    }, WEBSOCKET_CONFIG.heartbeatInterval)
  }

  return {
    // Initial state
    status: 'disconnected',
    socket: null,
    subscriptions: new Set(),
    lastMessage: null,
    error: null,
    reconnectAttempts: 0,

    connect: () => {
      const { socket, status } = get()
      if (socket && (status === 'connected' || status === 'connecting')) {
        return
      }

      clearTimers()
      set({ status: 'connecting', error: null })

      // Build WebSocket URL
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      let wsUrl: string
      if (API_CONFIG.baseUrl) {
        // If baseUrl is set (dev), extract host from it
        try {
          const apiHost = new URL(API_CONFIG.baseUrl).host
          wsUrl = `${protocol}//${apiHost}${WEBSOCKET_CONFIG.path}`
        } catch {
          // Fallback to same origin
          wsUrl = `${protocol}//${window.location.host}${WEBSOCKET_CONFIG.path}`
        }
      } else {
        // Production: use same origin (nginx proxy)
        wsUrl = `${protocol}//${window.location.host}${WEBSOCKET_CONFIG.path}`
      }

      console.log('[WebSocket] Connecting to:', wsUrl)
      const ws = new WebSocket(wsUrl)

      ws.onopen = () => {
        console.log('[WebSocket] Connected')
        set({ status: 'connected', socket: ws, reconnectAttempts: 0, error: null })
        startHeartbeat()

        // Re-subscribe to any existing subscriptions
        const { subscriptions } = get()
        if (subscriptions.size > 0) {
          const subs = Array.from(subscriptions)
          console.log('[WebSocket] Re-subscribing to:', subs)
          ws.send(JSON.stringify({
            action: 'subscribe',
            subscriptions: subs
          }))
        }
      }

      ws.onmessage = (event) => {
        try {
          const message: WebSocketMessage = JSON.parse(event.data)
          set({ lastMessage: message })

          // Log non-heartbeat messages
          if (message.type !== 'heartbeat' && message.type !== 'pong') {
            console.log('[WebSocket] Message:', message.type, message.timeframe)
          }
        } catch (e) {
          console.error('[WebSocket] Failed to parse message:', e)
        }
      }

      ws.onclose = (event) => {
        console.log('[WebSocket] Closed:', event.code, event.reason)
        clearTimers()
        set({ socket: null })

        if (!event.wasClean && get().status !== 'disconnected') {
          scheduleReconnect()
        } else {
          set({ status: 'disconnected' })
        }
      }

      ws.onerror = (error) => {
        console.error('[WebSocket] Error:', error)
        set({ error: 'WebSocket connection error' })
      }

      set({ socket: ws })
    },

    disconnect: () => {
      const { socket } = get()
      clearTimers()
      if (socket) {
        socket.close(1000, 'Client disconnect')
      }
      set({
        status: 'disconnected',
        socket: null,
        subscriptions: new Set(),
        error: null,
        reconnectAttempts: 0
      })
    },

    subscribe: (subscriptionKey: string) => {
      const { socket, subscriptions, status } = get()
      if (subscriptions.has(subscriptionKey)) return

      const newSubscriptions = new Set(subscriptions)
      newSubscriptions.add(subscriptionKey)
      set({ subscriptions: newSubscriptions })

      if (socket && status === 'connected') {
        console.log('[WebSocket] Subscribing to:', subscriptionKey)
        socket.send(JSON.stringify({
          action: 'subscribe',
          subscriptions: [subscriptionKey]
        }))
      }
    },

    unsubscribe: (subscriptionKey: string) => {
      const { socket, subscriptions, status } = get()
      if (!subscriptions.has(subscriptionKey)) return

      const newSubscriptions = new Set(subscriptions)
      newSubscriptions.delete(subscriptionKey)
      set({ subscriptions: newSubscriptions })

      if (socket && status === 'connected') {
        console.log('[WebSocket] Unsubscribing from:', subscriptionKey)
        socket.send(JSON.stringify({
          action: 'unsubscribe',
          subscriptions: [subscriptionKey]
        }))
      }
    },

    sendPing: () => {
      const { socket, status } = get()
      if (socket && status === 'connected') {
        socket.send(JSON.stringify({ action: 'ping' }))
      }
    },
  }
})
