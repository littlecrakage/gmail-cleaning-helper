"""Google OAuth2 authentication for Gmail API."""

import os
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://mail.google.com/",  # full access, required for permanent batchDelete
]

TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")


def get_credentials() -> Credentials:
    """Load or refresh credentials, triggering OAuth flow if needed."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    "credentials.json not found.\n"
                    "Download it from Google Cloud Console:\n"
                    "  1. Go to https://console.cloud.google.com/\n"
                    "  2. Create a project and enable the Gmail API\n"
                    "  3. Create OAuth 2.0 credentials (Desktop app)\n"
                    "  4. Download and save as credentials.json in this directory"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())

    return creds
