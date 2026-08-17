#!/usr/bin/env python3

import logging
import queue
import threading
import time
from typing import Dict, Optional, Tuple

from models import SyncRequest, SyncResult, SyncState
from sync_engine import SyncEngine

logger = logging.getLogger(__name__)

DOCUMENT_CHANGE = "document_change"


class OperationsQueue:
    """Thread-safe queue for processing sync requests with git commit coordination"""

    def __init__(self, sync_engine: SyncEngine, commit_interval: int = 10):
        self.sync_engine = sync_engine
        self.commit_interval = commit_interval
        self.request_queue = queue.Queue()
        self.sync_state = SyncState()
        self._document_changes: Dict[Tuple[str, str], dict] = {}
        self._document_changes_lock = threading.Lock()

        # Start worker thread and git commit timer
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

        self.commit_timer_thread = threading.Thread(target=self._commit_timer_loop, daemon=True)
        self.commit_timer_thread.start()

    def enqueue_sync_request(self, request: SyncRequest):
        """Add a sync request to the processing queue"""
        print(f"Enqueuing sync request for resource: {request.resource} at {request.timestamp}")
        self.request_queue.put(request)

    def enqueue_document_change(self, change_data: dict):
        """Coalesce queued notifications for the same Relay resource."""
        key = (change_data["relay_id"], change_data["resource_id"])
        with self._document_changes_lock:
            already_queued = key in self._document_changes
            existing = self._document_changes.get(key, {})
            merged = {**existing, **change_data}
            if "subdoc_snapshot" in existing and "subdoc_snapshot" not in change_data:
                merged["subdoc_snapshot"] = existing["subdoc_snapshot"]
                merged["baseline_only"] = existing.get("baseline_only", False)
            self._document_changes[key] = merged

        if already_queued:
            logger.debug(
                "Coalesced document change for relay=%s resource=%s",
                key[0],
                key[1],
            )
            return

        print(
            f"Enqueuing document change for relay: {change_data['relay_id']}, resource: {change_data['resource_id']} at {change_data['timestamp']}"
        )
        self.request_queue.put((DOCUMENT_CHANGE, key))

    def _take_document_change(self, key: Tuple[str, str]) -> Optional[dict]:
        with self._document_changes_lock:
            return self._document_changes.pop(key, None)

    def _worker_loop(self):
        """Main worker loop that processes sync requests"""
        while True:
            try:
                # Get next request from queue (blocks until available)
                request = self.request_queue.get(timeout=1.0)
                result = None

                try:
                    if isinstance(request, SyncRequest):
                        result = self._process_with_state_management(request)
                    elif (
                        isinstance(request, tuple)
                        and len(request) == 2
                        and request[0] == DOCUMENT_CHANGE
                    ):
                        change_data = self._take_document_change(request[1])
                        if change_data is not None:
                            result = self.sync_engine.process_document_change(
                                change_data["relay_id"],
                                change_data["resource_id"],
                                change_data["timestamp"],
                                subdoc_snapshot=change_data.get("subdoc_snapshot"),
                                baseline_only=change_data.get("baseline_only", False),
                            )
                    else:
                        logger.warning(f"Unknown request type: {type(request)}")

                    if result and result.success and result.operations:
                        self.sync_state.has_changes = True
                finally:
                    self.request_queue.task_done()

            except queue.Empty:
                # Timeout - continue loop
                continue
            except Exception as e:
                logger.error(f"Error in worker loop: {e}")

    def _process_with_state_management(self, request: SyncRequest) -> SyncResult:
        """Process sync request with proper state management"""
        with self.sync_state.sync_lock:
            try:
                self.sync_state.is_syncing = True

                # Process the sync request
                result = self.sync_engine.process_sync_request(request)

                # Add operations to tracking
                if result.operations:
                    self.sync_state.pending_operations.extend(result.operations)

                    # Mark completed operations
                    for op in result.operations:
                        if op.completed:
                            self.sync_state.completed_operations.append(op)

                return result

            except Exception as e:
                logger.error(f"Error processing sync request: {e}")
                return SyncResult(
                    relay_id=request.relay_id,
                    folder_id=None,
                    operations=[],
                    success=False,
                    error=str(e),
                )
            finally:
                self.sync_state.is_syncing = False

    def _commit_timer_loop(self):
        """Background timer for git commits"""
        while True:
            time.sleep(self.commit_interval)
            self._maybe_commit_changes()

    def _maybe_commit_changes(self):
        """Commit changes to git repositories if there are any"""
        # deferred_deletions_pending keeps the timer alive past has_changes
        # resets: a commit of unrelated work returns True and clears the flag
        # while young unpaired deletions are still being held back, and with
        # no later events the deferrals would otherwise never be released.
        if not (
            self.sync_state.has_changes
            or self.sync_engine.persistence_manager.deferred_deletions_pending
        ):
            return

        try:
            # Use the persistence manager from sync engine to commit changes
            committed = self.sync_engine.persistence_manager.commit_changes()

            if committed:
                # Reset change flag
                self.sync_state.has_changes = False
                self.sync_state.last_git_commit = time.time()

        except Exception as e:
            logger.error(f"Error in commit timer: {e}")

    def wait_for_empty_queue(self, timeout: Optional[float] = None):
        """Wait for all queued requests to be processed"""
        try:
            # Block until all tasks are done
            if timeout:
                # Python's join() doesn't support timeout, so we implement our own
                start_time = time.time()
                while not self.request_queue.empty():
                    if time.time() - start_time > timeout:
                        return False
                    time.sleep(0.1)
                return True
            else:
                self.request_queue.join()
                return True
        except Exception as e:
            logger.error(f"Error waiting for queue to empty: {e}")
            return False

    def get_queue_size(self) -> int:
        """Get current queue size"""
        return self.request_queue.qsize()

    def get_sync_state(self) -> SyncState:
        """Get current sync state"""
        return self.sync_state
