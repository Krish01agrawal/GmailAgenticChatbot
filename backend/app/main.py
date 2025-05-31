from fastapi import FastAPI, Depends, HTTPException, status, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from app.auth import verify_google_token, create_jwt_token, decode_jwt_token
from app.oauth import generate_auth_url, exchange_code_for_tokens
from app.db import users_collection, emails_collection, chats_collection
from app.gmail import build_gmail_service, fetch_emails
from app.mem0_agent import upload_emails_to_mem0, query_mem0
from app.models import GoogleToken, GmailFetchPayload
from app.websocket import router as websocket_router
import logging
from bson import ObjectId
from pydantic import BaseModel
import os

# Configure basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Allow CORS from frontend origin (adjust as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://localhost:8001", "http://127.0.0.1:8000", "http://127.0.0.1:8001"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept", "Origin", "Referrer-Policy"],
    expose_headers=["*"]
)

app.include_router(websocket_router)

# Define a Pydantic model for the test query request body
class TestMem0QueryPayload(BaseModel):
    user_id: str
    query: str

class ChatHistoryRequest(BaseModel):
    jwt_token: str
    chat_id: str = None  # Optional, if not provided returns all chats for user

@app.post("/auth/google-login")
async def google_login(payload: GoogleToken):
    logger.info(f"Received request for /auth/google-login with token: {payload.token[:30]}...")
    try:
        logger.info("Verifying Google token...")
        raw_user_info_from_google = verify_google_token(payload.token)
        logger.info(f"Google token verified. Raw User info from Google: {raw_user_info_from_google}")

        user_id_from_google = raw_user_info_from_google["user_id"]
        logger.info(f"Checking user in database: {user_id_from_google}")
        
        # Fetch user from DB to get the version with _id (if it exists)
        user_in_db = await users_collection.find_one({"user_id": user_id_from_google})
        
        final_user_info_to_return = {}

        if not user_in_db:
            logger.info("User not found, creating new user with Google info...")
            # Use the info directly from Google for the first insert
            # MongoDB will add an _id field automatically
            insert_result = await users_collection.insert_one(raw_user_info_from_google.copy()) # Use a copy
            logger.info(f"New user created. Inserted ID: {insert_result.inserted_id}")
            # Fetch the newly created user to get all fields including the auto-generated _id
            final_user_info_to_return = await users_collection.find_one({"_id": insert_result.inserted_id})
            if not final_user_info_to_return:
                 logger.error("CRITICAL: User just inserted but not found by _id!")
                 final_user_info_to_return = raw_user_info_from_google # Fallback, but _id will be missing
        else:
            logger.info("User found in database.")
            final_user_info_to_return = user_in_db

        # Ensure _id (and any other ObjectId) is converted to string before returning
        serializable_user_info = convert_objectid_to_str(final_user_info_to_return)
        logger.info(f"Serializable user info for response: {serializable_user_info}")

        logger.info("Creating JWT token...")
        # Create JWT based on consistent user_id and email
        jwt_payload_data = {"user_id": serializable_user_info["user_id"], "email": serializable_user_info["email"]}
        jwt_token = create_jwt_token(jwt_payload_data)
        logger.info("JWT token created successfully.")
        
        {
            "jwt_token": jwt_token,
            "user": {
                "email": serializable_user_info["email"],
                "name": serializable_user_info["name"],
                "picture": serializable_user_info["picture"],
                "user_id": serializable_user_info["user_id"],
            },
        }
        return {"jwt_token": jwt_token, "user": serializable_user_info}
    except HTTPException as e:
        logger.error(f"HTTPException in google_login: {e.detail}", exc_info=True)
        raise 
    except Exception as e:
        logger.error(f"Unexpected error in google_login: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"An unexpected error occurred: {str(e)}"
        )

@app.post("/gmail/fetch")
async def gmail_fetch(payload: GmailFetchPayload):
    # Authenticate user with JWT
    logger.info("Received request for /gmail/fetch")
    try:
        logger.info("Decoding JWT token...")
        user = decode_jwt_token(payload.jwt_token)
        user_id = user.get("user_id")
        logger.info(f"JWT decoded. User ID: {user_id}")

        logger.info("Building Gmail service...")
        service = build_gmail_service(payload.access_token)
        logger.info("Fetching emails...")
        emails = await fetch_emails(service, max_results=100)
        logger.info(f"Fetched {len(emails)} emails.")

        # Store emails in MongoDB
        if emails:
            logger.info("Storing emails in MongoDB...")
            for email_item in emails:
                email_item['user_id'] = user_id
            await emails_collection.insert_many(emails)
            logger.info("Emails stored in MongoDB.")
        else:
            logger.info("No emails to store.")

        # Upload to Mem0 memory
        if emails:
            logger.info("Uploading emails to Mem0...")
            await upload_emails_to_mem0(user_id, emails)
            logger.info("Emails uploaded to Mem0.")
        else:
            logger.info("No emails to upload to Mem0.")

        return {"message": "Emails fetched and processed successfully", "count": len(emails)}
    except HTTPException as e:
        logger.error(f"HTTPException in gmail_fetch: {e.detail}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error in gmail_fetch: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"An unexpected error occurred during gmail fetch: {str(e)}"
        )

@app.post("/test/mem0-query")
async def test_mem0_query_endpoint(payload: TestMem0QueryPayload):
    logger.info(f"Received request for /test/mem0-query for user_id: {payload.user_id} with query: {payload.query}")
    try:
        # Note: This endpoint does not perform JWT authentication for simplicity in direct testing.
        # In a production scenario, you would likely want to protect this.
        results = await query_mem0(user_id=payload.user_id, query=payload.query)
        return results
    except Exception as e:
        logger.error(f"Error in /test/mem0-query endpoint: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}"
        )

# Add new OAuth routes after the existing CORS setup
@app.get("/auth/login")
async def login():
    """Redirect user to Google OAuth consent screen."""
    try:
        auth_url, state = generate_auth_url()
        logger.info(f"Redirecting user to Google OAuth with state: {state}")
        return RedirectResponse(url=auth_url)
    except Exception as e:
        logger.error(f"Error in /auth/login: {e}")
        raise HTTPException(status_code=500, detail=f"Authentication error: {e}")

@app.get("/auth/callback")
async def oauth_callback(code: str = None, state: str = None, error: str = None):
    """Handle OAuth callback from Google."""
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:8000")
    
    if error:
        logger.error(f"OAuth error: {error}")
        return RedirectResponse(url=f"{frontend_url}?error={error}")
    
    if not code or not state:
        logger.error("Missing code or state in OAuth callback")
        return RedirectResponse(url=f"{frontend_url}?error=missing_parameters")
    
    try:
        # Exchange code for tokens and user info
        credentials, user_info = exchange_code_for_tokens(code, state)
        logger.info(f"OAuth successful for user: {user_info['email']}")
        
        # Store or update user in database
        user_id_from_google = user_info["user_id"]
        user_in_db = await users_collection.find_one({"user_id": user_id_from_google})
        
        if not user_in_db:
            logger.info("Creating new user from OAuth...")
            # Store user info with OAuth tokens
            user_data = user_info.copy()
            user_data.update({
                "access_token": credentials.token,
                "refresh_token": credentials.refresh_token,
                "token_expiry": credentials.expiry.isoformat() if credentials.expiry else None
            })
            insert_result = await users_collection.insert_one(user_data)
            final_user_info = await users_collection.find_one({"_id": insert_result.inserted_id})
        else:
            logger.info("Updating existing user with new OAuth tokens...")
            # Update existing user with new tokens
            update_data = {
                "access_token": credentials.token,
                "refresh_token": credentials.refresh_token,
                "token_expiry": credentials.expiry.isoformat() if credentials.expiry else None,
                "name": user_info.get("name"),
                "picture": user_info.get("picture")
            }
            await users_collection.update_one(
                {"user_id": user_id_from_google},
                {"$set": update_data}
            )
            final_user_info = await users_collection.find_one({"user_id": user_id_from_google})
        
        # Create JWT token
        jwt_payload = {"user_id": user_info["user_id"], "email": user_info["email"]}
        jwt_token = create_jwt_token(jwt_payload)
        
        logger.info(f"OAuth completed successfully for user: {user_info['email']}")
        
        # Redirect to frontend with JWT token (no auto-email fetching)
        return RedirectResponse(url=f"{frontend_url}?token={jwt_token}&user={user_info['email']}")
        
    except Exception as e:
        logger.error(f"Error in OAuth callback: {e}")
        return RedirectResponse(url=f"{frontend_url}?error=auth_failed")

@app.post("/emails/fetch")
async def fetch_user_emails(authorization: str = Header(None)):
    """
    Fetch emails for authenticated user.
    Expects Authorization header with Bearer token.
    """
    try:
        # Validate authorization header
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
        
        token = authorization.split(" ")[1]
        user = decode_jwt_token(token)
        user_id = user.get("user_id")
        user_email = user.get("email")
        
        logger.info(f"Fetching emails for user: {user_email}")
        
        # Get user's stored access token from database
        user_in_db = await users_collection.find_one({"user_id": user_id})
        if not user_in_db or not user_in_db.get("access_token"):
            raise HTTPException(status_code=400, detail="User not found or no access token available")
        
        access_token = user_in_db["access_token"]
        
        # Build Gmail service and fetch emails
        logger.info("Building Gmail service...")
        service = build_gmail_service(access_token)
        
        logger.info("Fetching emails from Gmail...")
        emails = await fetch_emails(service, max_results=100)
        logger.info(f"Fetched {len(emails)} emails from Gmail")
        
        if emails:
            # Store emails in MongoDB
            logger.info("Storing emails in MongoDB...")
            for email_item in emails:
                email_item['user_id'] = user_id
            
            # Remove existing emails for this user to avoid duplicates
            await emails_collection.delete_many({"user_id": user_id})
            await emails_collection.insert_many(emails)
            logger.info("Emails stored in MongoDB")
            
            # Upload to Mem0
            logger.info("Uploading emails to Mem0...")
            await upload_emails_to_mem0(user_id, emails)
            logger.info("Emails uploaded to Mem0")
        
        return {
            "success": True,
            "message": f"Successfully fetched and processed {len(emails)} emails",
            "email_count": len(emails),
            "user_email": user_email
        }
        
    except HTTPException as e:
        logger.error(f"HTTPException in fetch_user_emails: {e.detail}")
        raise
    except Exception as e:
        logger.error(f"Error in fetch_user_emails: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch emails: {str(e)}"
        )

# Alternative endpoint with JWT in body (easier for frontend)
class EmailFetchRequest(BaseModel):
    jwt_token: str
    max_results: int = 100

@app.post("/emails/fetch-with-token")
async def fetch_user_emails_with_token(payload: EmailFetchRequest):
    """
    Fetch emails for authenticated user.
    Expects JWT token in request body.
    """
    try:
        # Decode JWT token
        user = decode_jwt_token(payload.jwt_token)
        user_id = user.get("user_id")
        user_email = user.get("email")
        
        logger.info(f"Fetching emails for user: {user_email}")
        
        # Get user's stored access token from database
        user_in_db = await users_collection.find_one({"user_id": user_id})
        if not user_in_db or not user_in_db.get("access_token"):
            raise HTTPException(status_code=400, detail="User not found or no access token available")
        
        access_token = user_in_db["access_token"]
        
        # Build Gmail service and fetch emails
        logger.info("Building Gmail service...")
        service = build_gmail_service(access_token)
        
        logger.info(f"Fetching emails from Gmail (max: {payload.max_results})...")
        emails = await fetch_emails(service, max_results=payload.max_results)
        logger.info(f"Fetched {len(emails)} emails from Gmail")
        
        if emails:
            # Store emails in MongoDB
            logger.info("Storing emails in MongoDB...")
            for email_item in emails:
                email_item['user_id'] = user_id
            
            # Remove existing emails for this user to avoid duplicates
            await emails_collection.delete_many({"user_id": user_id})
            await emails_collection.insert_many(emails)
            logger.info("Emails stored in MongoDB")
            
            # Upload to Mem0
            logger.info("Uploading emails to Mem0...")
            await upload_emails_to_mem0(user_id, emails)
            logger.info("Emails uploaded to Mem0")
        
        return {
            "success": True,
            "message": f"Successfully fetched and processed {len(emails)} emails",
            "email_count": len(emails),
            "user_email": user_email
        }
        
    except HTTPException as e:
        logger.error(f"HTTPException in fetch_user_emails_with_token: {e.detail}")
        raise
    except Exception as e:
        logger.error(f"Error in fetch_user_emails_with_token: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch emails: {str(e)}"
        )

def convert_objectid_to_str(data):
    """Recursively converts ObjectId instances in a dictionary or list to strings."""
    if isinstance(data, list):
        return [convert_objectid_to_str(item) for item in data]
    elif isinstance(data, dict):
        return {key: convert_objectid_to_str(value) for key, value in data.items()}
    elif isinstance(data, ObjectId):
        return str(data)
    return data

@app.post("/chat/history")
async def get_chat_history(payload: ChatHistoryRequest):
    """
    Get chat history for a user.
    If chat_id is provided, returns messages for that specific chat.
    Otherwise, returns all chats for the user.
    """
    try:
        # Decode JWT token
        user = decode_jwt_token(payload.jwt_token)
        user_id = user.get("user_id")
        user_email = user.get("email")
        
        logger.info(f"Fetching chat history for user: {user_email}, chat_id: {payload.chat_id}")
        
        # Build query filter
        query_filter = {"user_id": user_id}
        if payload.chat_id:
            query_filter["chat_id"] = payload.chat_id
        
        # Fetch chat messages sorted by timestamp
        chat_messages = await chats_collection.find(query_filter).sort("timestamp", 1).to_list(length=None)
        
        # Convert ObjectIds to strings
        serialized_messages = convert_objectid_to_str(chat_messages)
        
        logger.info(f"Found {len(serialized_messages)} chat messages")
        
        if payload.chat_id:
            # Return messages for specific chat
            return {
                "success": True,
                "chat_id": payload.chat_id,
                "messages": serialized_messages,
                "message_count": len(serialized_messages)
            }
        else:
            # Group messages by chat_id
            chats_by_id = {}
            for msg in serialized_messages:
                chat_id = msg.get("chat_id")
                if chat_id not in chats_by_id:
                    chats_by_id[chat_id] = []
                chats_by_id[chat_id].append(msg)
            
            return {
                "success": True,
                "user_id": user_id,
                "chats": chats_by_id,
                "total_chats": len(chats_by_id),
                "total_messages": len(serialized_messages)
            }
        
    except HTTPException as e:
        logger.error(f"HTTPException in get_chat_history: {e.detail}")
        raise
    except Exception as e:
        logger.error(f"Error in get_chat_history: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch chat history: {str(e)}"
        )
