"""WebSocket endpoint for real-time market data updates"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Set, Dict, Any
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from config import get_websocket_config

logger = logging.getLogger(__name__)
router = APIRouter()


class ConnectionManager:
    """Manages WebSocket connections and broadcasts updates"""

    def __init__(self):
        # Active connections by subscription key (e.g., "15M:MNQ")
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        # All connections for broadcast
        self.all_connections: Set[WebSocket] = set()
        self._config = get_websocket_config()

    async def connect(self, websocket: WebSocket, subscriptions: list[str] = None):
        """Accept a new WebSocket connection"""
        await websocket.accept()
        self.all_connections.add(websocket)

        # Subscribe to specific timeframes/symbols
        if subscriptions:
            for sub in subscriptions:
                if sub not in self.active_connections:
                    self.active_connections[sub] = set()
                self.active_connections[sub].add(websocket)

        logger.info(f"WebSocket connected. Total: {len(self.all_connections)}")

    def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection"""
        self.all_connections.discard(websocket)

        # Remove from all subscription groups
        for sub_key in list(self.active_connections.keys()):
            self.active_connections[sub_key].discard(websocket)
            if not self.active_connections[sub_key]:
                del self.active_connections[sub_key]

        logger.info(f"WebSocket disconnected. Total: {len(self.all_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        if not self.all_connections:
            return

        data = json.dumps(message, default=str)
        disconnected = set()

        for connection in self.all_connections:
            try:
                await connection.send_text(data)
            except Exception:
                disconnected.add(connection)

        # Clean up disconnected
        for conn in disconnected:
            self.disconnect(conn)

    async def send_to_subscribers(self, subscription_key: str, message: dict):
        """Send message to subscribers of a specific timeframe/symbol"""
        connections = self.active_connections.get(subscription_key, set())
        if not connections:
            return

        data = json.dumps(message, default=str)
        disconnected = set()

        for connection in connections:
            try:
                await connection.send_text(data)
            except Exception:
                disconnected.add(connection)

        # Clean up disconnected
        for conn in disconnected:
            self.disconnect(conn)

    async def send_bar_update(self, timeframe: str, symbol: str, bar: dict):
        """Send bar update to subscribers"""
        message = {
            "type": "bar_update",
            "timeframe": timeframe,
            "symbol": symbol,
            "data": bar,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self.send_to_subscribers(f"{timeframe}:{symbol}", message)

    async def send_bar_close(self, timeframe: str, symbol: str, bar: dict):
        """Send bar close notification to subscribers"""
        message = {
            "type": "bar_close",
            "timeframe": timeframe,
            "symbol": symbol,
            "data": bar,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self.send_to_subscribers(f"{timeframe}:{symbol}", message)

    async def send_signal(self, timeframe: str, symbol: str, signal: dict):
        """Send new signal to subscribers"""
        message = {
            "type": "signal",
            "timeframe": timeframe,
            "symbol": symbol,
            "data": signal,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self.send_to_subscribers(f"{timeframe}:{symbol}", message)

    async def send_regime_change(self, timeframe: str, symbol: str, regime: dict):
        """Send regime change notification to subscribers"""
        message = {
            "type": "regime_change",
            "timeframe": timeframe,
            "symbol": symbol,
            "data": regime,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self.send_to_subscribers(f"{timeframe}:{symbol}", message)


# Global connection manager
manager = ConnectionManager()


def get_manager() -> ConnectionManager:
    """Get the global connection manager"""
    return manager


@router.websocket("/live")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for live market data

    Connection flow:
    1. Client connects
    2. Client sends subscription message: {"action": "subscribe", "subscriptions": ["15M:MNQ", "1H:MNQ"]}
    3. Server pushes updates for subscribed timeframes/symbols

    Message types received from server:
    - bar_update: Current bar updated (intrabar)
    - bar_close: Bar completed
    - signal: New orderflow signal detected
    - regime_change: Market regime changed

    Example subscription message:
    {
        "action": "subscribe",
        "subscriptions": ["15M:MNQ", "1H:MNQ"]
    }
    """
    await manager.connect(websocket)
    config = get_websocket_config()

    try:
        while True:
            try:
                # Wait for messages from client with timeout for heartbeat
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=config.heartbeat_interval
                )

                message = json.loads(data)
                action = message.get("action")

                if action == "subscribe":
                    # Subscribe to timeframes/symbols
                    subscriptions = message.get("subscriptions", [])
                    for sub in subscriptions:
                        if sub not in manager.active_connections:
                            manager.active_connections[sub] = set()
                        manager.active_connections[sub].add(websocket)
                    logger.info(f"Client subscribed to: {subscriptions}")

                    # Send confirmation
                    await websocket.send_json({
                        "type": "subscribed",
                        "subscriptions": subscriptions
                    })

                elif action == "unsubscribe":
                    # Unsubscribe from timeframes/symbols
                    subscriptions = message.get("subscriptions", [])
                    for sub in subscriptions:
                        if sub in manager.active_connections:
                            manager.active_connections[sub].discard(websocket)

                elif action == "ping":
                    # Respond to ping
                    await websocket.send_json({"type": "pong"})

            except asyncio.TimeoutError:
                # Send heartbeat
                try:
                    await websocket.send_json({"type": "heartbeat"})
                except Exception:
                    break

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)
