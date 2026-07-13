"""Deterministic Model Provider assembly for directed tests."""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from pathlib import Path

from diagnosis_service.model_provider import ModelProviderConfiguration
from diagnosis_service.model_provider_crypto import CredentialCipher, read_encryption_key
from diagnosis_service.model_provider_repository import ModelProviderRepository


def build_test_model_provider(
    db_path: Path,
    key_path: Path,
    *,
    clock: Callable[[], float] = time.time,
) -> ModelProviderConfiguration:
    ids = itertools.count(1)
    nonces = itertools.count(1)
    return ModelProviderConfiguration(
        ModelProviderRepository(db_path),
        CredentialCipher(
            read_encryption_key(key_path),
            nonce_source=lambda size: next(nonces).to_bytes(size, "big"),
        ),
        clock=clock,
        revision_id=lambda: f"model-provider:test-{next(ids)}",
        verification_nonce=lambda: f"verification-nonce-{next(ids):044d}",
    )
