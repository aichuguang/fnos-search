from __future__ import annotations

import unittest
from dataclasses import dataclass

from fnos_media_import.transfer.recovery import (
    RecoveryScanResult,
    TransferRecoveryRuntime,
    TransferRecoveryScanner,
)


@dataclass
class _Execution:
    job_id: int
    phase: str


class _Executions:
    def __init__(self, rows):
        self.rows = rows

    def list_due(self, **_kwargs):
        return list(self.rows)


class _Queue:
    def __init__(self):
        self.calls = []
        self.created = True

    def enqueue(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        created, self.created = self.created, False
        return 11, created


class TransferRecoveryTests(unittest.TestCase):
    def test_requeues_due_execution_with_stable_key(self):
        queue = _Queue()
        scanner = TransferRecoveryScanner(
            _Executions([_Execution(7, "transferring")]), queue
        )
        first = scanner.scan_once()
        second = scanner.scan_once()
        self.assertEqual(first.enqueued, 1)
        self.assertEqual(second.already_queued, 1)
        self.assertEqual(queue.calls[0][0][0], "import_transfer_execute")
        self.assertEqual(queue.calls[0][0][2], "import-transfer:7")
        self.assertEqual(queue.calls[0][0][1]["job_id"], 7)

    def test_manual_review_is_not_requeued(self):
        queue = _Queue()
        scanner = TransferRecoveryScanner(
            _Executions([_Execution(8, "manual_review"), _Execution(9, "completed")]), queue
        )
        result = scanner.scan_once()
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.skipped_terminal, 2)
        self.assertFalse(queue.calls)

    def test_runtime_records_scan_result_and_is_idempotently_started(self):
        class Scanner:
            calls = 0

            def scan_once(self, *, limit=100):
                self.calls += 1
                return RecoveryScanResult(scanned=2, enqueued=1, already_queued=1)

        scanner = Scanner()
        runtime = TransferRecoveryRuntime(scanner, interval_seconds=60, batch_size=25)
        result = runtime.run_once()
        self.assertEqual(result.enqueued, 1)
        status = runtime.status()
        self.assertEqual(status["run_count"], 1)
        self.assertEqual(status["batch_size"], 25)
        self.assertEqual(status["last_result"]["already_queued"], 1)

        self.assertTrue(runtime.start())
        self.assertFalse(runtime.start())
        runtime.shutdown()

    def test_runtime_keeps_error_visible(self):
        class Scanner:
            @staticmethod
            def scan_once(*, limit=100):
                del limit
                raise RuntimeError("database unavailable")

        runtime = TransferRecoveryRuntime(Scanner(), interval_seconds=60)
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            runtime.run_once()
        status = runtime.status()
        self.assertEqual(status["run_count"], 1)
        self.assertIn("database unavailable", status["last_error"])


if __name__ == "__main__":
    unittest.main()
