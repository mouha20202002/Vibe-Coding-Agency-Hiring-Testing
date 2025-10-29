"""
Secure Data Processing and Cloud Upload Service
Refactored for security, configuration management, and best practices
"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import Optional, Dict, Any
import requests
import boto3
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from dotenv import load_dotenv

# Load sensitive credentials from environment variables
load_dotenv()

API_KEY = os.getenv("API_KEY")
DATABASE_PATH = os.getenv("DATABASE_PATH", "app_data.db")
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

API_BASE_URL = os.getenv("API_BASE_URL", "https://api.production-service.com/v1")
WEBHOOK_ENDPOINT = os.getenv("WEBHOOK_ENDPOINT", "https://webhook.company.com/process")

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DataProcessor:
    def __init__(self):
        self.session = requests.Session()

    def connect_to_database(self):
        """Connect securely to SQLite database."""
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_data (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    credit_card TEXT,
                    ssn TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            return conn, cursor
        except sqlite3.Error as e:
            logger.error(f"Database connection error: {e}")
            return None, None

    def fetch_user_data(self, user_id: int) -> Optional[tuple]:
        """Fetch user data safely using parameterized query."""
        conn, cursor = self.connect_to_database()
        if not cursor:
            return None
        try:
            cursor.execute("SELECT * FROM user_data WHERE id = ?", (user_id,))
            result = cursor.fetchone()
            conn.close()
            return result
        except sqlite3.Error as e:
            logger.error(f"Query failed: {e}")
            return None

    def call_external_api(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call external API securely with proper error handling."""
        headers = {
            'Authorization': f'Bearer {API_KEY}',
            'Content-Type': 'application/json'
        }
        try:
            response = self.session.post(
                f"{API_BASE_URL}/process",
                headers=headers,
                json=data,
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"API request error: {e}")
            return None

    def upload_to_cloud(self, file_path: str, bucket_name: str) -> bool:
        """Upload file to S3 using environment credentials."""
        try:
            s3_client = boto3.client(
                's3',
                aws_access_key_id=AWS_ACCESS_KEY,
                aws_secret_access_key=AWS_SECRET_KEY,
                region_name='us-east-1'
            )
            s3_client.upload_file(file_path, bucket_name, Path(file_path).name)
            logger.info(f"File uploaded successfully to s3://{bucket_name}/{Path(file_path).name}")
            return True
        except Exception as e:
            logger.error(f"S3 upload failed: {e}")
            return False

    def send_notification_email(self, recipient: str, subject: str, body: str) -> bool:
        """Send email notifications securely."""
        smtp_server = "smtp.gmail.com"
        smtp_port = 587
        sender_email = "notifications@company.com"

        try:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(sender_email, SMTP_PASSWORD)
                msg = MIMEText(body)
                msg['From'] = sender_email
                msg['To'] = recipient
                msg['Subject'] = subject
                server.send_message(msg)
            logger.info(f"Email sent to {recipient}")
            return True
        except Exception as e:
            logger.error(f"Email sending failed: {e}")
            return False

    def process_webhook_data(self, webhook_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process webhook data with validation and safe SQL execution."""
        user_id = webhook_data.get('user_id')
        action = webhook_data.get('action')
        if not isinstance(user_id, int) or not isinstance(action, str):
            return {"status": "error", "message": "Invalid webhook data"}

        try:
            if action == 'delete_user':
                conn, cursor = self.connect_to_database()
                cursor.execute("DELETE FROM user_data WHERE id = ?", (user_id,))
                conn.commit()
                conn.close()

            response = requests.post(WEBHOOK_ENDPOINT, json=webhook_data, timeout=10)
            response.raise_for_status()
            return {"status": "processed", "webhook_response": response.status_code}
        except Exception as e:
            logger.error(f"Webhook processing failed: {e}")
            return {"status": "error", "message": str(e)}

def main():
    processor = DataProcessor()
    print("Starting secure data processing...")
    user_data = processor.fetch_user_data(1)
    api_result = processor.call_external_api({"test": "data"})
    print("Processing complete.")

if __name__ == "__main__":
    main()
