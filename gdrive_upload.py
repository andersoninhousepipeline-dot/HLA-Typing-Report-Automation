"""
gdrive_upload.py
Google Drive upload helper for HLA Report Generator.

Setup (one-time):
  1. Go to https://console.cloud.google.com/
  2. Create a project → Enable "Google Drive API"
  3. Create OAuth 2.0 credentials (Desktop app) → download as credentials.json
  4. Place credentials.json in the same folder as this file.
  5. First run will open a browser for Google sign-in and save token.json.
"""

import os
import io
from pathlib import Path

SCOPES     = ["https://www.googleapis.com/auth/drive.file"]
_BASE_DIR  = Path(__file__).parent
CREDS_FILE = _BASE_DIR / "credentials.json"
TOKEN_FILE = _BASE_DIR / "token.json"


def is_configured() -> bool:
    """Return True if credentials.json exists (Drive upload is possible)."""
    return CREDS_FILE.exists()


def get_drive_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not CREDS_FILE.exists():
        raise FileNotFoundError(
            f"Google Drive credentials not found.\n"
            f"Please download credentials.json from Google Cloud Console\n"
            f"and place it at:\n  {CREDS_FILE}"
        )

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


def upload_pdf(file_path: str, file_name: str) -> tuple:
    """
    Upload PDF to Google Drive and make it publicly viewable.
    Returns (file_id, share_url).
    """
    from googleapiclient.http import MediaFileUpload

    service  = get_drive_service()
    metadata = {"name": file_name, "mimeType": "application/pdf"}
    media    = MediaFileUpload(file_path, mimetype="application/pdf", resumable=True)

    f = service.files().create(body=metadata, media_body=media, fields="id").execute()
    file_id = f["id"]

    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    share_url = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    return file_id, share_url


def update_pdf(file_id: str, file_path: str) -> None:
    """Replace the content of an existing Drive file with the QR-embedded version."""
    from googleapiclient.http import MediaFileUpload

    service = get_drive_service()
    media   = MediaFileUpload(file_path, mimetype="application/pdf", resumable=True)
    service.files().update(fileId=file_id, media_body=media).execute()
