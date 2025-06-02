from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException
from jose import jwt
from app.auth import decode_jwt_token
from app.mem0_agent import query_mem0
import asyncio
from typing import Dict, Set

router = APIRouter()

# Store connections by chat_id and user_id
active_connections: Dict[str, Dict[str, WebSocket]] = {}

async def get_user_from_token(token: str):
    return decode_jwt_token(token)

@router.websocket("/ws/chat/{chat_id}")
async def websocket_endpoint(websocket: WebSocket, chat_id: str):
    await websocket.accept()
    try:
        # Receive initial message with JWT token for auth
        data = await websocket.receive_json()
        print(f"Received data: {data}")
        token = data.get("jwt_token")
        if not token:
            await websocket.close(code=1008)
            return
        user = await get_user_from_token(token)
        user_id = user.get("user_id")

        # Initialize chat room if it doesn't exist
        if chat_id not in active_connections:
            active_connections[chat_id] = {}
        
        # Store connection for this user in this chat
        active_connections[chat_id][user_id] = websocket

        while True:
            data = await websocket.receive_json()
            query = data.get("message")
            if not query:
                continue
            
            # Query Mem0 + LLM
            reply = await query_mem0(user_id=user_id, query=query)

            # Send reply to all users in this chat
            for participant_socket in active_connections[chat_id].values():
                await participant_socket.send_json({
                    "message": reply,
                    "sender_id": user_id,
                    "chat_id": chat_id
                })

    except WebSocketDisconnect:
        # Remove user from chat room when they disconnect
        if chat_id in active_connections and user_id in active_connections[chat_id]:
            del active_connections[chat_id][user_id]
            # Remove chat room if empty
            if not active_connections[chat_id]:
                del active_connections[chat_id]
