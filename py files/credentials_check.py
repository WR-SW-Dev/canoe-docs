#!/usr/bin/env python3
"""
credentials_check.py -- Verify Canoe API credentials are configured correctly.
Does NOT download documents. Just fetches a token and confirms success.
"""

import canoe_auth


def main():
    print("Checking Canoe API credentials...")
    print("Requesting access token (tries client_credentials, then password grant)...")
    data = canoe_auth.fetch_token()
    print(f"  auth flow used : {canoe_auth._token_cache.flow}")
    token_preview = data["access_token"][:12] + "..."
    print(f"  token preview  : {token_preview}")
    expires_in = data.get("expires_in", "unknown")
    print(f"  expires in     : {expires_in}s")
    if expires_in == "unknown":
        print("  NOTE: response did not include expires_in field.")
    print("\nCredentials are valid. Token received successfully.")
    print("\nNext step: run 'python canoe_downloader.py --dry-run'")


if __name__ == "__main__":
    main()
