#!/usr/bin/env python3
"""
Gong Forecasting Tool

Forecasts deal likelihood based on:
  - Number of conversations (calls) with a customer
  - Number of unique connections (contacts) at the customer

Usage:
    python gong_forecast.py --account-name "Acme Corp" --days 90
    python gong_forecast.py --account-id "abc123" --days 60
"""

import math
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

GONG_BASE_URL = "https://api.gong.io"
GONG_ACCESS_KEY = os.getenv("GONG_ACCESS_KEY")
GONG_ACCESS_SECRET = os.getenv("GONG_ACCESS_SECRET")

# Weights for the two signals (must sum to 1.0)
CONVERSATION_WEIGHT = 0.5
CONNECTION_WEIGHT = 0.5

# Saturation points – diminishing returns kick in after these counts
CONVERSATION_SATURATION = 15  # ~15 calls reaches ~63 % of max conv score
CONNECTION_SATURATION = 8     # ~8 contacts reaches ~63 % of max conn score


# ---------------------------------------------------------------------------
# Gong API client
# ---------------------------------------------------------------------------

class GongClient:
    """Thin wrapper around the Gong REST API v2."""

    def __init__(self, access_key: str, access_secret: str):
        self.session = requests.Session()
        self.session.auth = (access_key, access_secret)
        self.session.headers.update({"Content-Type": "application/json"})

    def find_account_by_name(self, name: str) -> Optional[dict]:
        """Search for a CRM account by name; returns the first match or None."""
        resp = self.session.get(
            f"{GONG_BASE_URL}/v2/crm/entities/accounts",
            params={"filter.name": name},
        )
        resp.raise_for_status()
        accounts = resp.json().get("accounts", [])
        return accounts[0] if accounts else None

    def get_calls_for_account(
        self, account_id: str, from_date: datetime, to_date: datetime
    ) -> list:
        """Return all calls linked to *account_id* within the given date range."""
        calls = []
        cursor = None

        while True:
            payload = {
                "filter": {
                    "fromDateTime": from_date.isoformat(),
                    "toDateTime": to_date.isoformat(),
                    "accountIds": [account_id],
                },
                "contentSelector": {
                    "context": "Extended",
                    "exposedFields": {
                        "parties": True,
                        "content": {"topics": False, "trackers": False},
                        "interaction": False,
                        "collaboration": False,
                        "media": False,
                    },
                },
            }
            if cursor:
                payload["cursor"] = cursor

            resp = self.session.post(
                f"{GONG_BASE_URL}/v2/calls/extensive", json=payload
            )
            resp.raise_for_status()
            data = resp.json()

            calls.extend(data.get("calls", []))

            cursor = data.get("records", {}).get("cursor")
            if not cursor:
                break

        return calls

    def get_contacts_for_account(self, account_id: str) -> list:
        """Return all CRM contacts (connections) associated with *account_id*."""
        contacts = []
        cursor = None

        while True:
            params = {"filter.accountId": account_id}
            if cursor:
                params["cursor"] = cursor

            resp = self.session.get(
                f"{GONG_BASE_URL}/v2/crm/entities/contacts", params=params
            )
            resp.raise_for_status()
            data = resp.json()

            contacts.extend(data.get("contacts", []))

            cursor = data.get("records", {}).get("cursor")
            if not cursor:
                break

        return contacts


# ---------------------------------------------------------------------------
# Forecasting logic
# ---------------------------------------------------------------------------

def _score_signal(value: int, saturation: int) -> float:
    """
    Map a raw count to a 0–100 score using an exponential saturation curve.

    Reaches ~63 at the saturation point and ~95 at 3× saturation, giving
    meaningful credit for early activity while capping runaway values.
    """
    if value <= 0:
        return 0.0
    return (1 - math.exp(-value / saturation)) * 100


def compute_forecast(num_conversations: int, num_connections: int) -> dict:
    """
    Compute a weighted forecast score from conversation and connection counts.

    Returns a dict with:
        score             – overall 0–100 forecast score
        tier              – "High" / "Medium" / "Low" / "At Risk"
        conversation_score – 0–100 signal score for conversations
        connection_score   – 0–100 signal score for connections
        num_conversations  – raw conversation count
        num_connections    – raw connection count
    """
    conv_score = _score_signal(num_conversations, CONVERSATION_SATURATION)
    conn_score = _score_signal(num_connections, CONNECTION_SATURATION)

    total = CONVERSATION_WEIGHT * conv_score + CONNECTION_WEIGHT * conn_score

    if total >= 75:
        tier = "High"
    elif total >= 45:
        tier = "Medium"
    elif total >= 20:
        tier = "Low"
    else:
        tier = "At Risk"

    return {
        "score": round(total, 1),
        "tier": tier,
        "conversation_score": round(conv_score, 1),
        "connection_score": round(conn_score, 1),
        "num_conversations": num_conversations,
        "num_connections": num_connections,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_report(account_label: str, result: dict, lookback_days: int) -> None:
    sep = "─" * 50
    print(f"\n{sep}")
    print(f"  Gong Forecast Report")
    print(sep)
    print(f"  Account        : {account_label}")
    print(f"  Lookback       : {lookback_days} days")
    print(sep)
    print(f"  Conversations  : {result['num_conversations']}")
    print(f"  Connections    : {result['num_connections']}")
    print(sep)
    print(f"  Conv Score     : {result['conversation_score']} / 100")
    print(f"  Conn Score     : {result['connection_score']} / 100")
    print(f"  Forecast Score : {result['score']} / 100")
    print(f"  Tier           : {result['tier']}")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Forecast deal likelihood using Gong conversation and connection data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--account-id", metavar="ID",
                          help="Gong CRM account ID")
    id_group.add_argument("--account-name", metavar="NAME",
                          help="CRM account name (searches Gong for first match)")

    parser.add_argument(
        "--days", type=int, default=90,
        help="Number of days to look back for conversations (default: 90)",
    )

    args = parser.parse_args()

    if not GONG_ACCESS_KEY or not GONG_ACCESS_SECRET:
        print(
            "Error: GONG_ACCESS_KEY and GONG_ACCESS_SECRET must be set.\n"
            "Copy .env.example to .env and fill in your Gong API credentials.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = GongClient(GONG_ACCESS_KEY, GONG_ACCESS_SECRET)

    # Resolve account identifier
    if args.account_name:
        print(f"Searching Gong for account: '{args.account_name}' ...")
        account = client.find_account_by_name(args.account_name)
        if not account:
            print(f"Error: No account found matching '{args.account_name}'.", file=sys.stderr)
            sys.exit(1)
        account_id = account["id"]
        account_label = f"{account.get('name', account_id)} (ID: {account_id})"
        print(f"Found: {account_label}")
    else:
        account_id = args.account_id
        account_label = account_id

    # Build date window
    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=args.days)

    print(f"Fetching conversations (last {args.days} days) ...")
    calls = client.get_calls_for_account(account_id, from_date, to_date)

    print("Fetching connections ...")
    contacts = client.get_contacts_for_account(account_id)

    result = compute_forecast(len(calls), len(contacts))
    print_report(account_label, result, args.days)


if __name__ == "__main__":
    main()
