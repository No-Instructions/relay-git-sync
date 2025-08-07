#!/usr/bin/env python3

import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from dateutil.parser import isoparse
from models import SyncRequest
from relay_client import RelayClient

logger = logging.getLogger(__name__)


class WebhookProcessor:
    """Process webhook payloads and convert them to sync requests"""
    
    def __init__(self, relay_client: RelayClient):
        self.relay_client = relay_client
    
    def process_webhook(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process webhook payload and return document change data"""
        try:
            doc_id = payload.get('doc_id')
            timestamp = payload.get('timestamp')
            
            if not doc_id:
                logger.warning("Webhook payload missing doc_id")
                return None
                
            if timestamp is None:
                logger.warning("Webhook payload missing timestamp")
                return None
            
            # Extract relay_id and document_id from compound doc_id
            relay_id = self.relay_client.extract_relay_id(doc_id)
            document_id = self.relay_client.extract_document_id(doc_id)
            
            # Parse ISO 8601 timestamp to timezone-aware datetime
            if isinstance(timestamp, str):
                timestamp_dt = isoparse(timestamp)
            else:
                # Fallback for numeric timestamps
                timestamp_dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
            
            return {
                'relay_id': relay_id,
                'resource_id': document_id,  # Individual UUID, not compound ID
                'timestamp': timestamp_dt
            }
            
        except Exception as e:
            logger.error(f"Error processing webhook payload: {e}")
            logger.error(f"Webhook processing traceback: {traceback.format_exc()}")
            return None
    
    def parse_webhook_body(self, body: bytes) -> Optional[Dict[str, Any]]:
        """Parse webhook request body and extract payload"""
        try:
            # Parse JSON payload
            webhook_data = json.loads(body.decode('utf-8'))
            payload = webhook_data.get("payload", {})
            return payload
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in webhook body: {e}")
            return None
        except Exception as e:
            logger.error(f"Error parsing webhook body: {e}")
            return None