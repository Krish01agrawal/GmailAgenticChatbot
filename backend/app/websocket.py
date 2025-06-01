from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException
from jose import jwt
from app.auth import decode_jwt_token
from app.mem0_agent import query_mem0
from app.db import chats_collection
import asyncio
import json
from datetime import datetime
import uuid
import time

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
        
        # Handle chatId from frontend
        received_chat_id = data.get("chatId")
        
        if received_chat_id == "new":
            # Generate new unique chat_id when "new" is requested
            chat_id = generate_unique_chat_id(user_id)
            print(f"🆕 [NEW CHAT] Generated new chat_id: {chat_id} for user_id: {user_id}")
        elif received_chat_id:
            # Use existing chatId for resuming conversation
            chat_id = received_chat_id
            print(f"🔄 [RESUME CHAT] Using existing chat_id: {chat_id} for user_id: {user_id}")
        else:
            # Generate new unique chat_id when no chatId provided
            chat_id = generate_unique_chat_id(user_id)
            print(f"🆕 [NEW CHAT] Generated new chat_id: {chat_id} for user_id: {user_id}")
        
        print(f"🔗 [CONNECTION] WebSocket connection for user_id: {user_id}")
        print(f"💬 [CONNECTION] Active chat_id: {chat_id}")
        print(f"🆔 [CONNECTION] WebSocket ID: {websocket_id}")
        
        # Store connection with both user_id and chat_id tracking
        if user_id not in active_connections:
            active_connections[user_id] = {}
        active_connections[user_id][chat_id] = websocket
        
        # Store metadata for this websocket
        connection_metadata[websocket_id] = {
            "user_id": user_id,
            "chat_id": chat_id
        }

        print(f"📊 [CONNECTION] Active connections for user {user_id}: {list(active_connections[user_id].keys())}")

        # Send confirmation with chat_id (either new or existing)
        welcome_response = {
            "reply": ["Connected to chat. You can now ask questions about your emails!"],
            "error": False,
            "chatId": chat_id  # Send back the chat ID (new or existing)
        }
        
        print(f"👋 [CONNECTION] Sending welcome message with chat_id: {chat_id} to user_id: {user_id}")
        await safe_send_message(websocket, welcome_response, user_id, chat_id)

        while True:
            data = await websocket.receive_json()
            user_message = data.get("message")
            received_chat_id = data.get("chatId")
            
            print(f"📨 [MESSAGE] Received message from user_id: {user_id}")
            print(f"📨 [MESSAGE] Current chat_id: {chat_id}, Received chat_id: {received_chat_id}")
            
            # Update chat_id if provided in message (allows switching chats)
            if received_chat_id:
                # Remove from old chat_id if different
                if received_chat_id != chat_id:
                    print(f"🔄 [CHAT SWITCH] Switching chat from '{chat_id}' to '{received_chat_id}' for user_id: {user_id}")
                    
                    if chat_id in active_connections.get(user_id, {}):
                        del active_connections[user_id][chat_id]
                        print(f"🗑️ [CHAT SWITCH] Removed old chat_id '{chat_id}' from active connections")
                    
                    chat_id = received_chat_id
                    # Add to new chat_id
                    if user_id not in active_connections:
                        active_connections[user_id] = {}
                    active_connections[user_id][chat_id] = websocket
                    connection_metadata[websocket_id]["chat_id"] = chat_id
                    
                    print(f"✅ [CHAT SWITCH] Successfully switched to chat_id: '{chat_id}' for user_id: {user_id}")
                    print(f"📊 [CHAT SWITCH] Updated active connections for user {user_id}: {list(active_connections[user_id].keys())}")
            
            if not user_message:
                print(f"⚠️ [MESSAGE] Empty message received from user_id: {user_id}, chat_id: {chat_id}")
                continue
                
            # Check if connection is still open before processing
            if not is_websocket_open(websocket):
                print(f"⚠️ [MESSAGE] WebSocket closed during message processing for user_id: {user_id}, chat_id: {chat_id}")
                break
                
            try:
                # Query Mem0 + LLM (your existing logic)
                print(f"🤖 [CHAT] Processing message for user_id: {user_id}, chat_id: {chat_id}")
                print(f"📝 [CHAT] User message: '{user_message}'")
                
                ai_response_data = await query_mem0(user_id=user_id, query=user_message)
                
                # Extract the actual response text
                if isinstance(ai_response_data, dict) and "reply" in ai_response_data:
                    ai_response_text = ai_response_data["reply"][0] if ai_response_data["reply"] else "No response generated."
                else:
                    ai_response_text = str(ai_response_data)
                
                print(f"✅ [CHAT] AI response generated for user_id: {user_id}, chat_id: {chat_id}")
                print(f"📤 [CHAT] AI response preview: '{ai_response_text[:100]}{'...' if len(ai_response_text) > 100 else ''}'")
                
                # Save chat to MongoDB
                await save_chat_message(user_id, chat_id, user_message, ai_response_text)
                print(f"💾 [CHAT] Conversation saved to MongoDB - user_id: {user_id}, chat_id: {chat_id}")
                
                # Send response back with chat ID
                response = {
                    "reply": [ai_response_text],
                    "error": False,
                    "chatId": chat_id
                }
                
                print(f"📡 [CHAT] Sending response to user_id: {user_id}, chat_id: {chat_id}")
                send_success = await safe_send_message(websocket, response, user_id, chat_id)
                
                if send_success:
                    print(f"✅ [CHAT] Response sent successfully to user_id: {user_id}, chat_id: {chat_id}")
                else:
                    print(f"❌ [CHAT] Failed to send response to user_id: {user_id}, chat_id: {chat_id}")
                
            except Exception as e:
                print(f"❌ [CHAT ERROR] Error processing message for user_id: {user_id}, chat_id: {chat_id}")
                print(f"❌ [CHAT ERROR] Error details: {str(e)}")
                
                error_response = {
                    "reply": [f"Error processing your request: {str(e)}"],
                    "error": True,
                    "chatId": chat_id
                }
                
                print(f"📡 [CHAT ERROR] Sending error response to user_id: {user_id}, chat_id: {chat_id}")
                error_send_success = await safe_send_message(websocket, error_response, user_id, chat_id)
                
                if not error_send_success:
                    print(f"❌ [CHAT ERROR] Failed to send error response to user_id: {user_id}, chat_id: {chat_id}")

    except WebSocketDisconnect:
        # Clean up connection
        print(f"🔌 [DISCONNECT] WebSocket disconnected for websocket_id: {websocket_id}")
        
        if websocket_id in connection_metadata:
            metadata = connection_metadata[websocket_id]
            stored_user_id = metadata.get("user_id")
            stored_chat_id = metadata.get("chat_id")
            
            print(f"🧹 [CLEANUP] Cleaning up connection for user_id: {stored_user_id}, chat_id: {stored_chat_id}")
            
            # Remove from active_connections
            if stored_user_id in active_connections and stored_chat_id in active_connections[stored_user_id]:
                del active_connections[stored_user_id][stored_chat_id]
                print(f"🗑️ [CLEANUP] Removed chat_id '{stored_chat_id}' from active connections")
                
                # Clean up empty user entry
                if not active_connections[stored_user_id]:
                    del active_connections[stored_user_id]
                    print(f"🗑️ [CLEANUP] Removed user_id '{stored_user_id}' from active connections (no more chats)")
                else:
                    print(f"📊 [CLEANUP] Remaining active chats for user {stored_user_id}: {list(active_connections[stored_user_id].keys())}")
            
            # Remove metadata
            del connection_metadata[websocket_id]
            print(f"✅ [CLEANUP] Successfully cleaned up websocket_id: {websocket_id}")
    
    except Exception as e:
        # Handle any other errors
        print(f"💥 [ERROR] Unexpected error in WebSocket for user_id: {user_id}, chat_id: {chat_id}")
        print(f"💥 [ERROR] Error details: {str(e)}")
        
        try:
            error_response = {
                "reply": [f"Connection error: {str(e)}"],
                "error": True,
                "chatId": chat_id or user_id or "unknown"
            }
            
            print(f"📡 [ERROR] Sending connection error response to user_id: {user_id}, chat_id: {chat_id}")
            error_send_success = await safe_send_message(websocket, error_response, user_id, chat_id)
            
            if not error_send_success:
                print(f"❌ [ERROR] Failed to send connection error response to user_id: {user_id}, chat_id: {chat_id}")
        except Exception as send_error:
            print(f"❌ [ERROR] Exception while trying to send connection error response: {str(send_error)}")
            pass  # Connection might be closed
        
        # Clean up on error
        print(f"🧹 [ERROR CLEANUP] Cleaning up after error for websocket_id: {websocket_id}")
        
        if websocket_id in connection_metadata:
            metadata = connection_metadata[websocket_id]
            stored_user_id = metadata.get("user_id")
            stored_chat_id = metadata.get("chat_id")
            
            print(f"🗑️ [ERROR CLEANUP] Removing user_id: {stored_user_id}, chat_id: {stored_chat_id}")
            
            if stored_user_id in active_connections and stored_chat_id in active_connections[stored_user_id]:
                del active_connections[stored_user_id][stored_chat_id]
                if not active_connections[stored_user_id]:
                    del active_connections[stored_user_id]
            
            del connection_metadata[websocket_id]
            print(f"✅ [ERROR CLEANUP] Cleanup completed for websocket_id: {websocket_id}")

# Helper function to send message to specific chat
async def send_to_chat(user_id: str, chat_id: str, message: dict):
    """Send a message to a specific chat if connection exists"""
    try:
        if user_id in active_connections and chat_id in active_connections[user_id]:
            websocket = active_connections[user_id][chat_id]
            return await safe_send_message(websocket, message, user_id, chat_id)
    except Exception as e:
        print(f"❌ [SEND TO CHAT ERROR] Error sending message to chat {chat_id}: {e}")
    return False

# Helper function to get all active chats for a user
def get_user_active_chats(user_id: str) -> list:
    """Get list of active chat IDs for a user"""
    if user_id in active_connections:
        return list(active_connections[user_id].keys())
    return []
def is_websocket_open(websocket: WebSocket) -> bool:
    """Check if WebSocket connection is still open"""
    try:
        return websocket.client_state.value == 1  # OPEN state
    except:
        return False

async def safe_send_message(websocket: WebSocket, message: dict, user_id: str = None, chat_id: str = None) -> bool:
    """Safely send message through WebSocket with state checking"""
    try:
        if not is_websocket_open(websocket):
            print(f"⚠️ [SEND WARNING] WebSocket is not open for user_id: {user_id}, chat_id: {chat_id}")
            return False
            
        await websocket.send_text(json.dumps(message))
        print(f"✅ [SEND SUCCESS] Message sent successfully to message: {message}, chat_id: {chat_id}")
        return True
    except Exception as e:
        print(f"❌ [SEND ERROR] Failed to send message to user_id: {user_id}, chat_id: {chat_id} - Error: {str(e)}")
        return False

def generate_unique_chat_id(user_id: str) -> str:
    """Generate a unique chat ID for the user"""
    timestamp = int(time.time())
    unique_part = str(uuid.uuid4())[:8]
    return f"chat_{user_id}_{timestamp}_{unique_part}"

