"""
gmail_auth.py — One-time OAuth2 flow to obtain a Gmail refresh token.

Run this ONCE locally while logged into itookatuktuk@gmail.com:
    python tools/gmail_auth.py

It will open a browser, ask you to log in and authorise access, then print
the three values you need to add to .env (and Railway environment variables).

Prerequisites:
    1. Go to https://console.cloud.google.com/
    2. Create/select a project and enable the Gmail API
    3. Create OAuth 2.0 credentials (type: Desktop app)
    4. Download the JSON and save it as credentials.json in the project root
    5. Run this script
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def main():
    # Look for credentials.json in project root or current directory
    creds_path = None
    candidates = [
        Path("credentials.json"),
        Path(__file__).parent.parent / "credentials.json",
    ]
    for p in candidates:
        if p.exists():
            creds_path = p
            break

    if not creds_path:
        print("ERROR: credentials.json not found.")
        print()
        print("Steps to fix:")
        print("  1. Go to https://console.cloud.google.com/")
        print("  2. Enable the Gmail API for your project")
        print("  3. Create OAuth 2.0 credentials (Desktop app type)")
        print("  4. Download the JSON file and save it as credentials.json")
        print("     in the project root (next to this tools/ folder)")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: google-auth-oauthlib not installed.")
        print("Run: pip install google-auth-oauthlib")
        sys.exit(1)

    print(f"Using credentials from: {creds_path}")
    print("Opening browser for authorisation...")
    print("Make sure to log in as itookatuktuk@gmail.com\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n✅ Authorisation successful!\n")
    print("=" * 60)
    print("Add these to your .env file AND Railway environment variables:")
    print("=" * 60)
    print(f"GMAIL_CLIENT_ID={creds.client_id}")
    print(f"GMAIL_CLIENT_SECRET={creds.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 60)
    print()
    print("Once added to Railway, the email draft feature will activate")
    print("automatically on the next deploy.")


if __name__ == "__main__":
    main()
