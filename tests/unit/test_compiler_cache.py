from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tools.compiler_cache import (
    CompilerCacheError,
    CompilerCacheLock,
    cache_environment,
    install_pinned_cache,
    pinned_executable,
    resolve_compiler_cache,
    write_stats,
)


class CompilerCacheTests(unittest.TestCase):
    def test_install_verifies_archive_and_extracts_only_the_executable(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("sccache-v1/sccache.exe", b"cache-binary")
            bundle.writestr("sccache-v1/README", b"ignored")
        payload = archive.getvalue()
        lock = CompilerCacheLock(
            version="1.2.3",
            platform="test",
            archive_url="https://invalid.example/sccache.zip",
            archive_sha256=hashlib.sha256(payload).hexdigest(),
        )

        with tempfile.TemporaryDirectory() as temp_directory:
            tool_root = Path(temp_directory)

            def download(_url: str, destination: Path) -> None:
                Path(destination).write_bytes(payload)

            with mock.patch("urllib.request.urlretrieve", side_effect=download):
                executable = install_pinned_cache(lock=lock, tool_root=tool_root)

            self.assertEqual(executable.read_bytes(), b"cache-binary")
            self.assertEqual(executable, pinned_executable(lock, tool_root=tool_root))

    def test_install_rejects_unpinned_archive_content(self) -> None:
        lock = CompilerCacheLock("1", "test", "https://invalid.example/x", "0" * 64)
        with tempfile.TemporaryDirectory() as temp_directory:
            with mock.patch(
                "urllib.request.urlretrieve",
                side_effect=lambda _url, destination: Path(destination).write_bytes(b"wrong"),
            ):
                with self.assertRaisesRegex(CompilerCacheError, "hash mismatch"):
                    install_pinned_cache(lock=lock, tool_root=Path(temp_directory))

    def test_resolution_requires_the_locked_version(self) -> None:
        lock = CompilerCacheLock("1.2.3", "test", "unused", "unused")
        with tempfile.TemporaryDirectory() as temp_directory:
            executable = pinned_executable(lock, tool_root=Path(temp_directory))
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"binary")
            with mock.patch("tools.compiler_cache._reported_version", return_value="1.2.3"):
                resolved = resolve_compiler_cache(
                    "required", lock=lock, tool_root=Path(temp_directory)
                )
            self.assertEqual(resolved, executable.resolve())

    def test_environment_uses_repo_relative_content_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            environment = cache_environment({}, cache_directory=Path(temp_directory))

        self.assertEqual(environment["SCCACHE_CACHE_SIZE"], "20G")
        self.assertEqual(environment["SCCACHE_CLIENT_SIDE"], "1")
        self.assertTrue(Path(environment["SCCACHE_DIR"]).is_absolute())

    def test_telemetry_reports_nonduplicated_hit_rate_and_source_bytes(self) -> None:
        raw = {
            "stats": {
                "cache_hits": {"counts": {"C/C++": 3}, "adv_counts": {"C/C++": 3}},
                "cache_misses": {"counts": {"C/C++": 1}, "adv_counts": {"C/C++": 1}},
            }
        }
        with tempfile.TemporaryDirectory() as temp_directory:
            output = Path(temp_directory) / "stats.json"
            with mock.patch("tools.compiler_cache.read_stats", return_value=raw):
                payload = write_stats(
                    Path("sccache.exe"),
                    {},
                    output,
                    source_bytes_compiled=4096,
                    compiled_source_count=2,
                )

        self.assertEqual(payload["b2_recomp"]["cache_hit_count"], 3)
        self.assertEqual(payload["b2_recomp"]["cache_miss_count"], 1)
        self.assertEqual(payload["b2_recomp"]["cache_hit_rate"], 0.75)
        self.assertEqual(payload["b2_recomp"]["source_bytes_compiled"], 4096)


if __name__ == "__main__":
    unittest.main()
