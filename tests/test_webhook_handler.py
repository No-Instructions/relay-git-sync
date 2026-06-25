#!/usr/bin/env python3

from datetime import timezone

from webhook_handler import WebhookProcessor


class RelayClientStub:
    @staticmethod
    def extract_relay_id(doc_id: str) -> str:
        return "-".join(doc_id.split("-")[:5])

    @staticmethod
    def extract_document_id(doc_id: str) -> str:
        return "-".join(doc_id.split("-")[-5:])


class TestWebhookProcessor:
    def setup_method(self):
        self.processor = WebhookProcessor(RelayClientStub())
        self.doc_id = (
            "12345678-1234-1234-1234-123456789abc-"
            "87654321-4321-4321-4321-cba987654321"
        )

    def test_process_webhook_reads_relay_payload_timestamp(self):
        result = self.processor.process_webhook(
            {
                "eventType": "document.updated",
                "eventId": "evt_test",
                "payload": {
                    "doc_id": self.doc_id,
                    "timestamp": "2026-01-01T00:00:00Z",
                },
            }
        )

        assert result is not None
        assert result["relay_id"] == "12345678-1234-1234-1234-123456789abc"
        assert result["resource_id"] == "87654321-4321-4321-4321-cba987654321"
        assert result["timestamp"].isoformat() == "2026-01-01T00:00:00+00:00"

    def test_process_webhook_keeps_top_level_timestamp_fallback(self):
        result = self.processor.process_webhook(
            {
                "payload": {"doc_id": self.doc_id},
                "timestamp": 1767225600,
            }
        )

        assert result is not None
        assert result["timestamp"].tzinfo == timezone.utc

    def test_process_webhook_rejects_missing_timestamp(self):
        result = self.processor.process_webhook(
            {
                "payload": {"doc_id": self.doc_id},
            }
        )

        assert result is None
