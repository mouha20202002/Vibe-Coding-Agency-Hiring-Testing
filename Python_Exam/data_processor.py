"""
Data Processing and Cloud Upload Service (secure refactor)

Usage:
  - Install deps: pip install -r requirements.txt
  - Create a .env from .env.example with real values
  - Run: python data_processor.py
"""

import os
import logging
import json
import hmac
import hashlib
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import smtplib
from email.mime.text import MIMEText

# Optionally load .env in development
try:
    from dotenv import load_dotenv  # pip install python-dotenv
    load_dotenv()
except Exception:
    pass  # not required in production if env provided by system

# Configuration from environment variables (no secrets in code)
API_KEY = os.environ.get("API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")  # e.g. postgresql://user:pass@host:port/dbname
API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.production-service.com/v1")
WEBHOOK_ENDPOINT = os.environ.get("WEBHOOK_ENDPOINT")  # outgoing webhook target (if used)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # for validating incoming webhooks (HMAC)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_BUCKET = os.environ.get("S3_BUCKET")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "noreply@example.com")

# Logging (do NOT log secrets)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("DataProcessor")

# HTTP session with retries and timeouts
def create_requests_session(timeout: int = 10, max_retries: int = 3) -> requests.Session:
    session = requests.Session()
    retries = Retry(total=max_retries, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "DataProcessor/1.0"})
    session.request_timeout = timeout
    return session

class DataProcessor:
    def __init__(self, db_url: Optional[str] = None):
        self.api_key = API_KEY
        self.api_base = API_BASE_URL
        self.session = create_requests_session()
        self.db_url = db_url or DATABASE_URL
        self.db_engine: Optional[Engine] = None
        if self.db_url:
            try:
                # create_engine will not expose password in logs; still be careful with env
                self.db_engine = create_engine(self.db_url, pool_pre_ping=True)
                logger.info("Database engine created")
            except Exception as e:
                logger.error(f"Failed to create DB engine: {e}")
                self.db_engine = None
        else:
            logger.warning("No DATABASE_URL provided; DB operations will be disabled")

        # boto3 client created lazily to prefer IAM role or env-based credentials
        self._s3_client = None

    # ------------------
    # Database utilities
    # ------------------
    def get_db_connection(self):
        if not self.db_engine:
            raise RuntimeError("No database engine configured")
        conn = self.db_engine.connect()
        return conn

    def ensure_table(self):
        """Create minimal table if not exists (only if using a local/test DB)."""
        if not self.db_engine:
            logger.debug("DB engine not configured; skipping ensure_table")
            return
        create_sql = text(
            """
            CREATE TABLE IF NOT EXISTS user_data (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                password_hash TEXT,
                credit_card_encrypted TEXT,
                ssn_encrypted TEXT,
                created_at TIMESTAMP DEFAULT now()
            )
            """
        )
        try:
            with self.get_db_connection() as conn:
                conn.execute(create_sql)
                logger.info("Ensured user_data table exists (if DB supports DDL)")
        except SQLAlchemyError as e:
            logger.error(f"Failed to ensure table: {e}")

    def fetch_user_data(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Fetch user data safely using a parameterized query."""
        if not self.db_engine:
            logger.error("Database not configured")
            return None
        query = text("SELECT id, username, password_hash, credit_card_encrypted, ssn_encrypted, created_at FROM user_data WHERE id = :uid")
        try:
            with self.get_db_connection() as conn:
                result = conn.execute(query, {"uid": user_id}).fetchone()
                if result:
                    # return a dict without exposing raw secrets
                    return dict(result._mapping)
                return None
        except SQLAlchemyError as e:
            logger.error(f"DB query failed: {e}")
            return None

    # ------------------
    # External API call
    # ------------------
    def call_external_api(self, data: dict, timeout: int = 10) -> Optional[dict]:
        """Call external API with timeout, retries and proper error handling."""
        if not self.api_key:
            logger.error("No API_KEY configured for external API calls")
            return None

        url = f"{self.api_base}/process"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self.session.post(url, headers=headers, json=data, timeout=timeout, verify=True)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            logger.error(f"External API returned error: {e} (status {getattr(e.response, 'status_code', 'N/A')})")
            return None
        except requests.RequestException as e:
            logger.error(f"Request to external API failed: {e}")
            return None

    # ------------------
    # S3 Upload
    # ------------------
    def get_s3_client(self):
        if self._s3_client:
            return self._s3_client
        # boto3 will use env vars / AWS credentials file / IAM role automatically
        session = boto3.session.Session(region_name=AWS_REGION)
        self._s3_client = session.client("s3")
        return self._s3_client

    def upload_to_s3(self, file_path: str, bucket: Optional[str] = None, key: Optional[str] = None) -> bool:
        bucket = bucket or S3_BUCKET
        if not bucket:
            logger.error("S3 bucket not configured")
            return False

        key = key or os.path.basename(file_path)
        s3 = self.get_s3_client()
        try:
            s3.upload_file(Filename=file_path, Bucket=bucket, Key=key)
            logger.info(f"Uploaded {file_path} to s3://{bucket}/{key}")
            return True
        except (BotoCoreError, ClientError) as e:
            logger.error(f"S3 upload failed: {e}")
            return False

    # ------------------
    # Email Notification
    # ------------------
    def send_notification_email(self, recipient: str, subject: str, body: str) -> bool:
        if not SMTP_USER or not SMTP_PASSWORD:
            logger.error("SMTP credentials not configured")
            return False

        msg = MIMEText(body)
        msg["From"] = EMAIL_FROM
        msg["To"] = recipient
        msg["Subject"] = subject

        try:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
            server.quit()
            logger.info(f"Sent notification email to {recipient}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False

    # ------------------
    # Webhook processing (with optional HMAC validation)
    # ------------------
    @staticmethod
    def _is_valid_hmac(secret: str, payload: bytes, signature_header: str) -> bool:
        """
        Validate HMAC-SHA256 signature header expected format: sha256=HEX
        """
        if not secret or not signature_header:
            return False
        try:
            prefix, signature = signature_header.split("=", 1)
            if prefix.lower() not in ("sha256",):
                return False
            expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        except Exception:
            return False

    def process_webhook_data(self, webhook_body: dict, signature_header: Optional[str] = None) -> Dict[str, Any]:
        """
        Process incoming webhook JSON safely.
        If WEBHOOK_SECRET is set, validate the signature (HMAC SHA256).
        """
        try:
            raw_payload = json.dumps(webhook_body).encode("utf-8")
            if WEBHOOK_SECRET:
                if not self._is_valid_hmac(WEBHOOK_SECRET, raw_payload, signature_header or ""):
                    logger.warning("Invalid webhook signature")
                    return {"status": "error", "message": "invalid signature"}

            user_id = webhook_body.get("user_id")
            action = webhook_body.get("action")

            # Basic validation
            if user_id is None or not isinstance(user_id, int):
                return {"status": "error", "message": "invalid user_id"}

            if action == "delete_user":
                # Use a parameterized delete
                if not self.db_engine:
                    logger.error("DB not configured; cannot delete user")
                    return {"status": "error", "message": "db not configured"}
                delete_sql = text("DELETE FROM user_data WHERE id = :uid")
                try:
                    with self.get_db_connection() as conn:
                        result = conn.execute(delete_sql, {"uid": user_id})
                        logger.info(f"Deleted rows: {result.rowcount} for user {user_id}")
                except SQLAlchemyError as e:
                    logger.error(f"Failed to delete user {user_id}: {e}")
                    return {"status": "error", "message": "db error"}

            # Optionally forward webhook to another internal endpoint with timeout and retries
            if WEBHOOK_ENDPOINT:
                try:
                    resp = self.session.post(WEBHOOK_ENDPOINT, json=webhook_body, timeout=10, verify=True)
                    logger.info(f"Forwarded webhook to {WEBHOOK_ENDPOINT}, status {resp.status_code}")
                    return {"status": "processed", "forward_status": resp.status_code}
                except requests.RequestException as e:
                    logger.error(f"Failed to forward webhook: {e}")
                    return {"status": "processed", "forward_error": str(e)}

            return {"status": "processed"}

        except Exception as e:
            logger.error(f"Webhook processing failed: {e}")
            return {"status": "error", "message": str(e)}


# ------------------
# Main demo
# ------------------
def main():
    dp = DataProcessor()
    # Example: ensure table (useful for dev/test)
    try:
        dp.ensure_table()
    except Exception:
        logger.debug("Ensure table skipped or failed (non-fatal)")

    # Demo safe fetch
    user = dp.fetch_user_data(1)
    logger.info(f"Fetched user: {'found' if user else 'none'}")

    # Demo API call
    api_res = dp.call_external_api({"test": "data"})
    logger.info(f"External API result: {'ok' if api_res else 'none'}")

    # Demo upload (will fail if no file or bucket configured)
    # dp.upload_to_s3("/path/to/file.txt")

    # Demo send email
    # dp.send_notification_email("someone@example.com", "Test", "Body")

    # Demo webhook processing
    example_webhook = {"user_id": 1, "action": "noop"}
    res = dp.process_webhook_data(example_webhook)
    logger.info(f"Webhook result: {res}")

if __name__ == "__main__":
    main()
