/**
 * Connection status indicator component
 * Shows WebSocket connection state in the header
 */

import type { ConnectionStatus as StatusType } from '../types/websocket'
import { COLORS } from '../config'
import './ConnectionStatus.css'

interface ConnectionStatusProps {
  status: StatusType
}

const statusConfig: Record<StatusType, { color: string; label: string; icon: string; pulse?: boolean }> = {
  disconnected: { color: COLORS.neutral, label: 'Offline', icon: '○' },
  connecting: { color: COLORS.alert.elevated, label: 'Connecting...', icon: '◐', pulse: true },
  connected: { color: COLORS.bullish, label: 'Live', icon: '●' },
  reconnecting: { color: COLORS.alert.elevated, label: 'Reconnecting...', icon: '◐', pulse: true },
  error: { color: COLORS.bearish, label: 'Error', icon: '✕' },
}

export default function ConnectionStatus({ status }: ConnectionStatusProps) {
  const config = statusConfig[status]

  return (
    <div className="connection-status" style={{ color: config.color }}>
      <span className={`status-icon ${config.pulse ? 'pulse' : ''}`}>{config.icon}</span>
      <span className="status-label">{config.label}</span>
    </div>
  )
}
