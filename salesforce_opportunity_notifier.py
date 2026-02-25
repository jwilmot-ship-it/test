"""
Salesforce Reseller Opportunity Notifier
========================================
A webhook server that listens for new Salesforce opportunities created with a
reseller and notifies the associated Canva Account Executive via email and
Slack DM.

Setup
-----
1. Install dependencies:  pip install -r requirements.txt
2. Copy .env.example to .env and fill in all credentials.
3. Run the server:       python salesforce_opportunity_notifier.py
4. Expose the server publicly (e.g. via ngrok) and configure Salesforce to
   call the /webhook/opportunity-created endpoint whenever an Opportunity is
   created (see "Salesforce Configuration" below).

Salesforce Configuration
------------------------
Create a Flow (or Outbound Message) that fires on Opportunity creation when
the Reseller__c field is populated.  The outbound HTTP call should POST JSON:

    {
        "opportunityId": "<Salesforce Opportunity Id>",
        "secret":        "<WEBHOOK_SECRET from .env>"
    }

Required custom Salesforce fields
----------------------------------
On the Opportunity object:
  - Reseller__c          : Lookup to Account (the reseller account)
  - Account_Executive__c : Lookup to User   (the owning Canva AE)

The AE's Salesforce User record must have a valid Email address.  That email
is used to locate the AE's Slack account via Slack's users.lookupByEmail API.
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from simple_salesforce import Salesforce, SalesforceAuthenticationFailed
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (sourced from environment / .env)
# ---------------------------------------------------------------------------
SALESFORCE_USERNAME = os.getenv("SALESFORCE_USERNAME")
SALESFORCE_PASSWORD = os.getenv("SALESFORCE_PASSWORD")
SALESFORCE_SECURITY_TOKEN = os.getenv("SALESFORCE_SECURITY_TOKEN")
SALESFORCE_DOMAIN = os.getenv("SALESFORCE_DOMAIN", "login")  # "test" for sandbox

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


# ---------------------------------------------------------------------------
# Salesforce helpers
# ---------------------------------------------------------------------------

def get_salesforce_client() -> Salesforce:
    """Return an authenticated simple_salesforce client."""
    return Salesforce(
        username=SALESFORCE_USERNAME,
        password=SALESFORCE_PASSWORD,
        security_token=SALESFORCE_SECURITY_TOKEN,
        domain=SALESFORCE_DOMAIN,
    )


def get_opportunity(sf: Salesforce, opportunity_id: str) -> dict | None:
    """
    Fetch the Opportunity and its related AE / Reseller data from Salesforce.

    Adjust the field names below if your org uses different API names.
    """
    query = f"""
        SELECT
            Id, Name,
            Amount, StageName, CloseDate, Type,
            Account.Name,
            Reseller__c, Reseller__r.Name,
            Account_Executive__c,
            Account_Executive__r.Name,
            Account_Executive__r.Email
        FROM Opportunity
        WHERE Id = '{opportunity_id}'
        LIMIT 1
    """
    result = sf.query(query)
    if result["totalSize"] == 0:
        return None
    return result["records"][0]


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _build_email(ae_name: str, ae_email: str, opp: dict) -> MIMEMultipart:
    reseller = (opp.get("Reseller__r") or {}).get("Name", "Unknown")
    account = (opp.get("Account") or {}).get("Name", "Unknown")
    amount = opp.get("Amount") or 0

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Canva] New Reseller Opportunity: {opp['Name']}"
    msg["From"] = EMAIL_FROM
    msg["To"] = ae_email

    plain = f"""\
Hi {ae_name},

A new reseller opportunity has been created in Salesforce that is associated with you.

Opportunity : {opp['Name']}
Account     : {account}
Reseller    : {reseller}
Amount      : ${amount:,.2f}
Stage       : {opp.get('StageName', 'N/A')}
Close Date  : {opp.get('CloseDate', 'N/A')}

Please review this opportunity in Salesforce and coordinate with the reseller.

Best regards,
Canva Sales Operations
"""

    html = f"""\
<html>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:#7B2FBE;padding:24px;border-radius:8px 8px 0 0;">
    <h2 style="color:#fff;margin:0;">New Reseller Opportunity</h2>
  </div>
  <div style="background:#fafafa;padding:24px;border:1px solid #e0e0e0;border-radius:0 0 8px 8px;">
    <p>Hi <strong>{ae_name}</strong>,</p>
    <p>A new reseller opportunity has been created in Salesforce that is associated with you.</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0;">
      <tr style="background:#f0f0f0;">
        <td style="padding:10px;font-weight:bold;width:40%;">Opportunity</td>
        <td style="padding:10px;">{opp['Name']}</td>
      </tr>
      <tr>
        <td style="padding:10px;font-weight:bold;">Account</td>
        <td style="padding:10px;">{account}</td>
      </tr>
      <tr style="background:#f0f0f0;">
        <td style="padding:10px;font-weight:bold;">Reseller</td>
        <td style="padding:10px;">{reseller}</td>
      </tr>
      <tr>
        <td style="padding:10px;font-weight:bold;">Amount</td>
        <td style="padding:10px;">${amount:,.2f}</td>
      </tr>
      <tr style="background:#f0f0f0;">
        <td style="padding:10px;font-weight:bold;">Stage</td>
        <td style="padding:10px;">{opp.get('StageName', 'N/A')}</td>
      </tr>
      <tr>
        <td style="padding:10px;font-weight:bold;">Close Date</td>
        <td style="padding:10px;">{opp.get('CloseDate', 'N/A')}</td>
      </tr>
    </table>
    <p>Please review this opportunity in Salesforce and coordinate with the reseller.</p>
    <p style="color:#666;font-size:.9em;">Best regards,<br>Canva Sales Operations</p>
  </div>
</body>
</html>
"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    return msg


def send_email(ae_name: str, ae_email: str, opp: dict) -> None:
    """Send an HTML + plain-text email to the Account Executive."""
    msg = _build_email(ae_name, ae_email, opp)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(EMAIL_FROM, ae_email, msg.as_string())
    log.info("Email sent to %s (%s)", ae_name, ae_email)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _slack_blocks(opp: dict) -> list:
    reseller = (opp.get("Reseller__r") or {}).get("Name", "Unknown")
    account = (opp.get("Account") or {}).get("Name", "Unknown")
    amount = opp.get("Amount") or 0

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":bell: New Reseller Opportunity Created"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "A new reseller opportunity has been created in Salesforce that is associated with you.",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Opportunity:*\n{opp['Name']}"},
                {"type": "mrkdwn", "text": f"*Account:*\n{account}"},
                {"type": "mrkdwn", "text": f"*Reseller:*\n{reseller}"},
                {"type": "mrkdwn", "text": f"*Amount:*\n${amount:,.2f}"},
                {"type": "mrkdwn", "text": f"*Stage:*\n{opp.get('StageName', 'N/A')}"},
                {"type": "mrkdwn", "text": f"*Close Date:*\n{opp.get('CloseDate', 'N/A')}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Please review this opportunity in Salesforce and coordinate with the reseller.",
                }
            ],
        },
    ]


def send_slack_dm(ae_email: str, ae_name: str, opp: dict) -> None:
    """
    Look up the AE's Slack user by email, then send a DM.

    Requires the Slack bot to have the users:read.email and chat:write scopes.
    """
    client = WebClient(token=SLACK_BOT_TOKEN)

    # Resolve email → Slack user ID
    user_response = client.users_lookupByEmail(email=ae_email)
    slack_user_id = user_response["user"]["id"]

    client.chat_postMessage(
        channel=slack_user_id,
        text=f"New Reseller Opportunity: {opp['Name']}",
        blocks=_slack_blocks(opp),
    )
    log.info("Slack DM sent to %s (Slack user %s)", ae_name, slack_user_id)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.route("/webhook/opportunity-created", methods=["POST"])
def opportunity_created():
    """
    Receives a POST from Salesforce whenever an Opportunity with a reseller is
    created.  Expected JSON body:

        {
            "opportunityId": "<18-char Salesforce ID>",
            "secret":        "<WEBHOOK_SECRET>"          // optional but recommended
        }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    # Optional shared-secret check
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    opportunity_id = data.get("opportunityId") or data.get("Id")
    if not opportunity_id:
        return jsonify({"error": "opportunityId is required"}), 400

    log.info("Received webhook for opportunity %s", opportunity_id)

    # --- Fetch from Salesforce ---
    try:
        sf = get_salesforce_client()
    except SalesforceAuthenticationFailed as exc:
        log.error("Salesforce authentication failed: %s", exc)
        return jsonify({"error": "Salesforce authentication failed"}), 500

    opp = get_opportunity(sf, opportunity_id)
    if not opp:
        return jsonify({"error": f"Opportunity {opportunity_id} not found"}), 404

    # Skip if no reseller is attached
    if not opp.get("Reseller__c"):
        log.info("Opportunity %s has no reseller — skipping", opportunity_id)
        return jsonify({"message": "No reseller attached; notification skipped"}), 200

    ae = opp.get("Account_Executive__r")
    if not ae:
        return jsonify({"error": "Opportunity has no Account Executive set"}), 400

    ae_name = ae.get("Name", "Account Executive")
    ae_email = ae.get("Email")
    if not ae_email:
        return jsonify({"error": "Account Executive has no email address in Salesforce"}), 400

    # --- Send notifications ---
    errors = []

    try:
        send_email(ae_name, ae_email, opp)
    except Exception as exc:
        log.error("Email notification failed: %s", exc)
        errors.append(f"Email failed: {exc}")

    try:
        send_slack_dm(ae_email, ae_name, opp)
    except SlackApiError as exc:
        log.error("Slack notification failed: %s", exc.response["error"])
        errors.append(f"Slack failed: {exc.response['error']}")
    except Exception as exc:
        log.error("Slack notification failed: %s", exc)
        errors.append(f"Slack failed: {exc}")

    if errors:
        return jsonify({"message": "Partially processed", "errors": errors}), 207

    return jsonify({"message": "Notifications sent successfully"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
