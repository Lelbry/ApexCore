"""AES-256 и SHA-1 throughput.

Оба теста замеряют скорость обработки больших буферов в МБ/с:
сколько байт данных в секунду удаётся зашифровать AES-256 / захэшировать
SHA-1. Реализация опирается на библиотеки, которые на современных x86-64
дистрибутивах автоматически задействуют аппаратные инструкции:

- ``cryptography.hazmat.primitives.ciphers`` → OpenSSL → AES-NI
  (специальные инструкции AESENC/AESDEC, ускоряющие AES в 5-10 раз).
- ``hashlib.sha1`` → OpenSSL → SHA-NI на CPU 2017+ (Goldmont, Ryzen),
  fallback на оптимизированную SSE-реализацию.

Это даёт цифры, сопоставимые с замерами AIDA64 / OpenSSL speed.

Источники
---------
NIST FIPS 197 (2001). Advanced Encryption Standard (AES). National
Institute of Standards and Technology, Federal Information Processing
Standards Publication 197.
DOI: https://doi.org/10.6028/NIST.FIPS.197

NIST FIPS 180-4 (2015). Secure Hash Standard (SHS). National Institute
of Standards and Technology.
DOI: https://doi.org/10.6028/NIST.FIPS.180-4

Gueron, S. (2010). Intel Advanced Encryption Standard (AES) New
Instructions Set. Intel White Paper.
https://www.intel.com/content/dam/doc/white-paper/advanced-encryption-standard-new-instructions-set-paper.pdf
"""

from __future__ import annotations

import hashlib
import os
import threading

from apexcore.domain.models import MicroBenchResult
from apexcore.infrastructure.microbench.base import time_loop

# Размер буфера на одну итерацию. 16 МБ достаточно, чтобы:
# - амортизировать overhead Python/FFI вызова,
# - не помещаться целиком в L2 (выходим за уровень кеша между итерациями).
BUFFER_MB = 16

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTOGRAPHY = False


class Aes256Bench:
    """AES-256-CBC throughput через ``cryptography`` (OpenSSL + AES-NI)."""

    name = "aes_256"
    category = "crypto"
    unit = "MB/s"

    def is_available(self) -> bool:
        return HAVE_CRYPTOGRAPHY

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        if not HAVE_CRYPTOGRAPHY:
            return MicroBenchResult(
                name=self.name,
                category=self.category,
                value=0.0,
                unit=self.unit,
                duration_actual_sec=0.0,
                error="cryptography не установлен",
            )

        key = os.urandom(32)  # AES-256 → 256-битный ключ
        iv = os.urandom(16)
        # Размер кратен размеру блока AES (16 байт) — без padding.
        plaintext = os.urandom(BUFFER_MB * 1024 * 1024)
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))

        def work() -> None:
            enc = cipher.encryptor()
            enc.update(plaintext)
            enc.finalize()

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        total_bytes = iterations * len(plaintext)
        mbps = total_bytes / elapsed / 1e6
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=mbps,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={
                "mode": "AES-256-CBC",
                "buffer_mb": BUFFER_MB,
                "backend": "cryptography/openssl",
                "source_file": "crypto.py",
            },
        )


class Sha1Bench:
    """SHA-1 throughput через ``hashlib`` (OpenSSL backend)."""

    name = "sha1"
    category = "crypto"
    unit = "MB/s"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        buf = os.urandom(BUFFER_MB * 1024 * 1024)

        def work() -> None:
            h = hashlib.sha1(buf)
            h.digest()

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        total_bytes = iterations * len(buf)
        mbps = total_bytes / elapsed / 1e6
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=mbps,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={"buffer_mb": BUFFER_MB, "backend": "hashlib/openssl", "source_file": "crypto.py"},
        )
