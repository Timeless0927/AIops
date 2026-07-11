"""Gateway runtime adapter for durable Diagnosis Request delivery."""

from __future__ import annotations

import logging
import threading

from .diagnosis_delivery import DiagnosisDelivery


def start_diagnosis_delivery(
    delivery: DiagnosisDelivery,
    *,
    interval_seconds: float = 1.0,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    stop = stop_event or threading.Event()

    def reconcile() -> None:
        while not stop.is_set():
            try:
                delivery.reconcile_due()
            except Exception:
                logging.exception("Diagnosis Request delivery failed")
            stop.wait(interval_seconds)

    thread = threading.Thread(target=reconcile, name="diagnosis-delivery", daemon=True)
    thread.start()
    return thread

