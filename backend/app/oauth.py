from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
import os
import json
import secrets
from typing import Dict
import logging

logger = logging.getLogger(__name__)

# Load environment variables
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8001/auth/callback")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000")

print(f"GOOGLE_CLIENT_SECRET: {GOOGLE_CLIENT_SECRET}")


# Scopes needed for Gmail access
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/gmail.readonly'
]

# Store for temporary state tokens (in production, use Redis or database)
auth_states: Dict[str, dict] = {}

def create_oauth_flow() -> Flow:
    """Create and configure OAuth2 flow."""
    try:
        client_config = {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI]
            }
        }
        
        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI
        )
        return flow
    except Exception as e:
        logger.error(f"Failed to create OAuth flow: {e}")
        raise HTTPException(status_code=500, detail=f"OAuth configuration error: {e}")

def generate_auth_url() -> tuple[str, str]:
    """Generate authorization URL and state token."""
    try:
        flow = create_oauth_flow()
        
        # Generate a secure random state token
        state = secrets.token_urlsafe(32)
        
        # Store state for verification
        auth_states[state] = {
            "created_at": "now",  # In production, use datetime
            "used": False
        }
        
        # Generate authorization URL
        authorization_url, _ = flow.authorization_url(
            access_type='offline',  # Get refresh token
            include_granted_scopes='true',
            prompt='consent',  # Force consent to ensure refresh token
            state=state
        )
        
        logger.info(f"Generated auth URL for state: {state}")
        return authorization_url, state
        
    except Exception as e:
        logger.error(f"Failed to generate auth URL: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate auth URL: {e}")

def exchange_code_for_tokens(code: str, state: str) -> tuple[Credentials, dict]:
    """Exchange authorization code for tokens and user info."""
    try:
        # Verify state token
        if state not in auth_states or auth_states[state]["used"]:
            raise HTTPException(status_code=400, detail="Invalid or expired state token")
        
        # Mark state as used
        auth_states[state]["used"] = True
        
        flow = create_oauth_flow()
        flow.fetch_token(code=code)
        
        credentials = flow.credentials
        
        # Get user info from ID token
        if credentials.id_token:
            from google.auth.transport import requests
            from google.oauth2 import id_token
            
            idinfo = id_token.verify_oauth2_token(
                credentials.id_token, 
                requests.Request(), 
                GOOGLE_CLIENT_ID
            )
            
            user_info = {
                "user_id": idinfo['sub'],
                "email": idinfo['email'],
                "name": idinfo.get('name'),
                "picture": idinfo.get('picture')
            }
            
            logger.info(f"Successfully exchanged code for tokens for user: {user_info['email']}")
            return credentials, user_info
        else:
            raise HTTPException(status_code=400, detail="No ID token received")
            
    except Exception as e:
        logger.error(f"Failed to exchange code for tokens: {e}")
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")

def cleanup_expired_states():
    """Clean up expired state tokens (call this periodically)."""
    # In production, implement proper cleanup based on timestamps
    pass 