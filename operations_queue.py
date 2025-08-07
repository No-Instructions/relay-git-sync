#!/usr/bin/env python3

import queue
import threading
import time
import logging
from typing import Optional
from models import SyncRequest, SyncResult, SyncState
from sync_engine import SyncEngine

logger = logging.getLogger(__name__)


class OperationsQueue:
    """Thread-safe queue for processing sync requests with git commit coordination"""
    
    def __init__(self, sync_engine: SyncEngine, commit_interval: int = 10):
        self.sync_engine = sync_engine
        self.commit_interval = commit_interval
        self.request_queue = queue.Queue()
        self.sync_state = SyncState()
        
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
        """Add a document change notification to the processing queue"""
        print(f"Enqueuing document change for relay: {change_data['relay_id']}, resource: {change_data['resource_id']} at {change_data['timestamp']}")
        self.request_queue.put(change_data)
    
    def _worker_loop(self):
        """Main worker loop that processes sync requests"""
        while True:
            try:
                # Get next request from queue (blocks until available)
                request = self.request_queue.get(timeout=1.0)
                
                # Process the request (could be SyncRequest or document change data)
                if isinstance(request, SyncRequest):
                    result = self._process_with_state_management(request)
                elif isinstance(request, dict) and 'relay_id' in request:
                    # Handle document change data with individual UUIDs
                    result = self.sync_engine.process_document_change(
                        request['relay_id'],
                        request['resource_id'],  # Individual UUID, not compound ID
                        request['timestamp']
                    )
                else:
                    logger.warning(f"Unknown request type: {type(request)}")
                    continue
                
                # Mark queue task as done
                self.request_queue.task_done()
                
                # If operations were performed, mark that we have changes
                if result.success and result.operations:
                    self.sync_state.has_changes = True
                    
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
                    error=str(e)
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
        if not self.sync_state.has_changes:
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