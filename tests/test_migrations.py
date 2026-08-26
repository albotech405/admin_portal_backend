# NOTE: This suite covers pure functions only (file discovery, pending-migration
# diffing, bootstrap-seed selection), per this repo's existing
# unittest-on-pure-functions convention (no live-DB mocking framework exists here).
# run_migrations() and its I/O helpers (_ensure_ledger_table, _load_applied_filenames,
# _seed_bootstrap, _apply_one) require a live psycopg2 connection to a real Postgres
# instance and are NOT exercised by any test in this repo -- no psycopg2.connect is
# mocked to fake coverage of that path. The first real run of this feature should be
# watched via Render's deploy logs (look for the "[migrations]" log lines).

import unittest
import tempfile
from pathlib import Path

from app.core.migrations import (
    _discover_sql_files,
    _pending_migrations,
    _files_to_seed,
    BOOTSTRAP_APPLIED_FILENAMES,
    SQL_DIR,
)


class DiscoverSqlFilesTests(unittest.TestCase):
    def test_returns_sorted_sql_filenames_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "20260101_b.sql").write_text("-- b")
            (base / "20260101_a.sql").write_text("-- a")
            (base / "not_sql.txt").write_text("ignore me")
            self.assertEqual(_discover_sql_files(base), ["20260101_a.sql", "20260101_b.sql"])

    def test_returns_empty_list_for_missing_directory(self) -> None:
        self.assertEqual(_discover_sql_files(Path("/does/not/exist")), [])

    def test_ignores_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "20260101_a.sql").write_text("-- a")
            (base / "subdir").mkdir()
            self.assertEqual(_discover_sql_files(base), ["20260101_a.sql"])


class PendingMigrationsTests(unittest.TestCase):
    def test_excludes_already_applied_files(self) -> None:
        self.assertEqual(_pending_migrations(["a.sql", "b.sql", "c.sql"], {"a.sql"}), ["b.sql", "c.sql"])

    def test_preserves_sorted_order_of_input(self) -> None:
        all_files = ["20260101_x.sql", "20260102_y.sql"]
        self.assertEqual(_pending_migrations(all_files, set()), all_files)

    def test_empty_when_everything_applied(self) -> None:
        self.assertEqual(_pending_migrations(["a.sql", "b.sql"], {"a.sql", "b.sql"}), [])

    def test_empty_when_no_files_exist(self) -> None:
        self.assertEqual(_pending_migrations([], set()), [])


class FilesToSeedTests(unittest.TestCase):
    def test_seeds_nothing_when_ledger_already_has_rows(self) -> None:
        self.assertEqual(_files_to_seed(list(BOOTSTRAP_APPLIED_FILENAMES), ledger_is_empty=False), [])

    def test_seeds_only_files_present_on_disk_and_in_bootstrap_list(self) -> None:
        all_files = ["20260507_admin_control_panel_rpc.sql", "20270101_future_new_file.sql"]
        self.assertEqual(_files_to_seed(all_files, ledger_is_empty=True), ["20260507_admin_control_panel_rpc.sql"])

    def test_seeds_nothing_when_ledger_empty_but_no_bootstrap_files_on_disk(self) -> None:
        self.assertEqual(_files_to_seed(["20270101_brand_new.sql"], ledger_is_empty=True), [])

    def test_bootstrap_list_matches_files_currently_in_sql_dir(self) -> None:
        # Guards against BOOTSTRAP_APPLIED_FILENAMES drifting from sql/'s real
        # contents. If this fails, a file predating this feature is missing from
        # the bootstrap list (it would get executed instead of skipped).
        on_disk = {p.name for p in SQL_DIR.glob("*.sql")}
        self.assertTrue(BOOTSTRAP_APPLIED_FILENAMES.issubset(on_disk))


if __name__ == "__main__":
    unittest.main()
