from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException
from jose import jwt
from app.auth import decode_jwt_token
from app.mem0_agent import query_mem0
from app.db import chats_collection
import asyncio
import json
from datetime import datetime

router = APIRouter()

# Store active connections with both user_id and chat_id tracking
active_connections = {}  # Structure: {user_id: {chat_id: websocket}}
connection_metadata = {}  # Structure: {websocket_id: {"user_id": str, "chat_id": str}}

async def get_user_from_token(token: str):
    return decode_jwt_token(token)

def get_websocket_id(websocket: WebSocket) -> str:
    """Generate unique ID for websocket connection"""
    return f"{id(websocket)}"

async def save_chat_message(user_id: str, chat_id: str, message: str, response: str):
    """Save chat conversation to MongoDB"""
    try:
        chat_document = {
            "user_id": user_id,
            "chat_id": chat_id,
            "timestamp": datetime.utcnow(),
            "user_message": message,
            "ai_response": response
        }
        await chats_collection.insert_one(chat_document)
    except Exception as e:
        print(f"Error saving chat to MongoDB: {e}")

@router.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    user_id = None
    chat_id = None
    websocket_id = get_websocket_id(websocket)
    
    try:
        # Receive initial message with JWT token for auth
        data = await websocket.receive_json()
        token = data.get("jwt_token")
        if not token:
            await websocket.close(code=1008)
            return
            
        user = await get_user_from_token(token)
        user_id = user.get("user_id")
        
        # Use user_id as default chat_id if not provided initially
        chat_id = data.get("chatId", user_id)
        
        # Store connection with both user_id and chat_id tracking
        if user_id not in active_connections:
            active_connections[user_id] = {}
        active_connections[user_id][chat_id] = websocket
        
        # Store metadata for this websocket
        connection_metadata[websocket_id] = {
            "user_id": user_id,
            "chat_id": chat_id
        }

        # Send confirmation with chat_id
        welcome_response = {
            "reply": ["Connected to chat. You can now ask questions about your emails!"],
            "error": False,
            "chatId": chat_id
        }
        await websocket.send_text(json.dumps(welcome_response))

        while True:
            data = await websocket.receive_json()
            user_message = data.get("message")
            received_chat_id = data.get("chatId")
            
            # Update chat_id if provided in message (allows switching chats)
            if received_chat_id:
                # Remove from old chat_id if different
                if received_chat_id != chat_id:
                    if chat_id in active_connections.get(user_id, {}):
                        del active_connections[user_id][chat_id]
                    chat_id = received_chat_id
                    # Add to new chat_id
                    if user_id not in active_connections:
                        active_connections[user_id] = {}
                    active_connections[user_id][chat_id] = websocket
                    connection_metadata[websocket_id]["chat_id"] = chat_id
            
            if not user_message:
                continue
                
            try:
                # Query Mem0 + LLM (your existing logic)
                ai_response_data = await query_mem0(user_id=user_id, query=user_message)
                
                # Extract the actual response text
                if isinstance(ai_response_data, dict) and "reply" in ai_response_data:
                    ai_response_text = ai_response_data["reply"][0] if ai_response_data["reply"] else "No response generated."
                else:
                    ai_response_text = str(ai_response_data)
                
                # Save chat to MongoDB
                await save_chat_message(user_id, chat_id, user_message, ai_response_text)
                
                # Send response back with chat ID
                response = {
                    "reply": [ai_response_text],
                    "error": False,
                    "chatId": chat_id
                }
                await websocket.send_text(json.dumps(response))
                
            except Exception as e:
                error_response = {
                    "reply": [f"Error processing your request: {str(e)}"],
                    "error": True,
                    "chatId": chat_id
                }
                await websocket.send_text(json.dumps(error_response))

    except WebSocketDisconnect:
        # Clean up connection
        if websocket_id in connection_metadata:
            metadata = connection_metadata[websocket_id]
            stored_user_id = metadata.get("user_id")
            stored_chat_id = metadata.get("chat_id")
            
            # Remove from active_connections
            if stored_user_id in active_connections and stored_chat_id in active_connections[stored_user_id]:
                del active_connections[stored_user_id][stored_chat_id]
                
                # Clean up empty user entry
                if not active_connections[stored_user_id]:
                    del active_connections[stored_user_id]
            
            # Remove metadata
            del connection_metadata[websocket_id]
    
    except Exception as e:
        # Handle any other errors
        try:
            error_response = {
                "reply": [f"Connection error: {str(e)}"],
                "error": True,
                "chatId": chat_id or user_id or "unknown"
            }
            await websocket.send_text(json.dumps(error_response))
        except:
            pass  # Connection might be closed
        
        # Clean up on error
        if websocket_id in connection_metadata:
            metadata = connection_metadata[websocket_id]
            stored_user_id = metadata.get("user_id")
            stored_chat_id = metadata.get("chat_id")
            
            if stored_user_id in active_connections and stored_chat_id in active_connections[stored_user_id]:
                del active_connections[stored_user_id][stored_chat_id]
                if not active_connections[stored_user_id]:
                    del active_connections[stored_user_id]
            
            del connection_metadata[websocket_id]

# Helper function to send message to specific chat
async def send_to_chat(user_id: str, chat_id: str, message: dict):
    """Send a message to a specific chat if connection exists"""
    try:
        if user_id in active_connections and chat_id in active_connections[user_id]:
            websocket = active_connections[user_id][chat_id]
            await websocket.send_text(json.dumps(message))
            return True
    except Exception as e:
        print(f"Error sending message to chat {chat_id}: {e}")
    return False

# Helper function to get all active chats for a user
def get_user_active_chats(user_id: str) -> list:
    """Get list of active chat IDs for a user"""
    if user_id in active_connections:
        return list(active_connections[user_id].keys())
    return []
