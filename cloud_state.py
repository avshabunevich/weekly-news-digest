from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger("cloud_state")


class RunGuard:
    """Firestore-backed lease and completion marker for scheduled deliveries."""

    def __init__(self, workflow: str, key: str, lease_minutes: int = 30) -> None:
        self.workflow = workflow
        self.project = os.getenv("FIRESTORE_PROJECT_ID", "").strip()
        self.owner = os.getenv("CLOUD_RUN_EXECUTION", "").strip() or str(uuid.uuid4())
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", key)
        self.document_id = f"{workflow}-{safe_key}"
        self.lease_minutes = lease_minutes
        self._document = None

    def acquire(self) -> bool:
        if not self.project:
            return True
        from google.cloud import firestore

        client = firestore.Client(project=self.project)
        document = client.collection("automation_runs").document(self.document_id)
        transaction = client.transaction()
        now = datetime.now(timezone.utc)

        @firestore.transactional
        def reserve(txn):
            snapshot = document.get(transaction=txn)
            data = snapshot.to_dict() or {}
            if data.get("status") == "sent":
                return False
            lease_until = data.get("lease_until")
            if (
                data.get("status") == "running"
                and lease_until
                and lease_until > now
                and data.get("owner") != self.owner
            ):
                return False
            txn.set(
                document,
                {
                    "workflow": self.workflow,
                    "status": "running",
                    "owner": self.owner,
                    "started_at": now,
                    "lease_until": now + timedelta(minutes=self.lease_minutes),
                },
                merge=True,
            )
            return True

        acquired = reserve(transaction)
        self._document = document
        if not acquired:
            LOG.info("Delivery marker %s is already complete or leased", self.document_id)
        return acquired

    def mark_sent(self) -> None:
        if self._document is not None:
            self._document.set(
                {"status": "sent", "owner": self.owner, "sent_at": datetime.now(timezone.utc)},
                merge=True,
            )

    def mark_failed(self, error: BaseException) -> None:
        if self._document is not None:
            self._document.set(
                {
                    "status": "failed",
                    "owner": self.owner,
                    "failed_at": datetime.now(timezone.utc),
                    "error": str(error)[:1000],
                },
                merge=True,
            )
