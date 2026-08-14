#!/usr/bin/env python3
"""Tests for the statement tracker's two destinations (local archive and --graph).

The reconcile/grid logic is destination-agnostic; what these cover is the seam that
replaced the old "walk a synced folder" assumption -- the archive inventory, the
hyperlink flavour, and the SharePoint publish/archive steps. The Graph destination is
exercised against an in-memory fake library, so no credentials or network are needed.
"""

import os
import shutil
import sys
import tempfile
import types
import unittest


class FakeGraphClient:
    """Minimal stand-in for GraphClient: a dict keyed by path-relative-to-root."""

    root_folder = "Canoe"

    def __init__(self):
        self.lib: dict[str, bytes] = {}
        self.moves: list[tuple[str, str]] = []

    def list_tree(self, skip=None):
        skip = skip or set()
        files = [{"path": p, "name": p.rsplit("/", 1)[-1], "item_id": p,
                  "web_url": "https://sharepoint.example/" + p}
                 for p in self.lib if p.split("/")[0] not in skip]
        return {"files": files, "folders": []}

    def list_folder(self, rel_folder):
        rel = rel_folder.strip("/")
        out = []
        for path, data in self.lib.items():
            head, _, name = path.rpartition("/")
            if head == rel:
                out.append({"name": name, "path": path, "item_id": path, "web_url": "",
                            "is_folder": False, "size": len(data)})
        return out

    def download(self, rel_path):
        return self.lib.get(rel_path.strip("/"))

    def move(self, rel_path, dest_rel_folder, new_name=None):
        data = self.lib.pop(rel_path)
        name = new_name or rel_path.rsplit("/", 1)[-1]
        target = f"{dest_rel_folder.strip('/')}/{name}"
        if target in self.lib:
            raise AssertionError(f"move would overwrite {target}")
        self.lib[target] = data
        self.moves.append((rel_path, target))

    def upload(self, data, rel_folder, filename):
        self.lib[f"{rel_folder.strip('/')}/{filename}"] = data


# The tracker imports graph_client lazily, so a fake can be installed before then.
_fake_graph = types.ModuleType("graph_client")
_fake_graph.GraphClient = FakeGraphClient
_fake_graph.NON_DOCUMENT_FOLDERS = {"_statement_tracker"}
sys.modules.setdefault("graph_client", _fake_graph)

import config                                                        # noqa: E402
import statement_tracker as st                                       # noqa: E402


class LocalInventoryTests(unittest.TestCase):
    """The local walk must keep the exclusions the old build_archive_index had."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        for rel in ["Merrill/cust_jan__2.pdf",
                    "Merrill/.DS_Store",
                    "Acme Fund IV/Acme Q1 2025.pdf",
                    "Acme Fund IV/~$open.xlsx",
                    "Beta Partners/2025/Beta_annual__3.pdf",
                    "_statement_tracker/backend/statement_status.csv",
                    ".git/config"]:
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("x")
        self.inv = st.local_inventory(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _paths(self):
        return sorted(f["path"] for f in self.inv["files"])

    def test_tracker_output_and_dot_dirs_are_not_indexed(self):
        # A run must never index its own outputs, nor VCS/OS noise.
        self.assertEqual(self._paths(), ["Acme Fund IV/Acme Q1 2025.pdf",
                                         "Beta Partners/2025/Beta_annual__3.pdf",
                                         "Merrill/cust_jan__2.pdf"])

    def test_excel_lock_files_are_not_indexed(self):
        self.assertNotIn("Acme Fund IV/~$open.xlsx", self._paths())

    def test_nested_folders_are_recorded_for_the_fallback_link(self):
        self.assertIn("Beta Partners/2025", [d["path"] for d in self.inv["folders"]])
        self.assertNotIn("_statement_tracker", [d["path"] for d in self.inv["folders"]])

    def test_merrill_stems_strip_the_dedup_suffix(self):
        self.assertEqual(st.merrill_stems(self.inv["files"]), frozenset({"cust_jan"}))

    def test_merrill_only_matches_the_top_level_custodian_folder(self):
        # A fund folder that merely contains "Merrill" in a subpath is not the
        # custodian exclusion list.
        files = [{"path": "Acme/Merrill/x.pdf", "name": "x.pdf"}]
        self.assertEqual(st.merrill_stems(files), frozenset())


class ArchiveLinkTests(unittest.TestCase):
    def setUp(self):
        self.inv = {
            "files": [{"path": "Acme Fund IV/Acme Q1 2025.pdf", "name": "Acme Q1 2025.pdf",
                       "web_url": "https://sharepoint.example/acme.pdf"}],
            "folders": [{"path": "Acme Fund IV", "name": "Acme Fund IV",
                         "web_url": "https://sharepoint.example/folder"}],
        }

    def test_local_links_are_relative_to_the_workbook_folder_and_quoted(self):
        links = st.ArchiveLinks(self.inv, web=False)
        self.assertEqual(links.url({"doc_name": "Acme Q1 2025", "investment": "Acme Fund IV"}),
                         "../Acme%20Fund%20IV/Acme%20Q1%202025.pdf")

    def test_graph_links_are_absolute_sharepoint_urls(self):
        # An uploaded workbook is not inside the tree, so a relative path is useless.
        links = st.ArchiveLinks(self.inv, web=True)
        self.assertEqual(links.url({"doc_name": "Acme Q1 2025", "investment": "Acme Fund IV"}),
                         "https://sharepoint.example/acme.pdf")

    def test_unknown_document_falls_back_to_the_fund_folder(self):
        for web, expected in ((False, "../Acme%20Fund%20IV"),
                              (True, "https://sharepoint.example/folder")):
            links = st.ArchiveLinks(self.inv, web=web)
            self.assertEqual(links.url({"doc_name": "missing", "investment": "Acme Fund IV"}),
                             expected)

    def test_unknown_document_and_unknown_fund_yields_no_link(self):
        links = st.ArchiveLinks(self.inv, web=True)
        self.assertIsNone(links.url({"doc_name": "missing", "investment": "Ghost Fund"}))


class GraphDestTests(unittest.TestCase):
    def setUp(self):
        self.staging = tempfile.mkdtemp()
        self._sharepoint = config.sharepoint
        config.sharepoint = lambda: {"hostname": "example.sharepoint.com",
                                     "site_path": "/sites/Investment",
                                     "library": "Documents", "root_folder": "Canoe"}
        self.dest = st.GraphDest(self.staging)
        self.gc = self.dest.gc
        self.gc.lib["Merrill/cust_jan.pdf"] = b"x"
        self.gc.lib["Acme Fund IV/Acme Q1.pdf"] = b"x"

    def tearDown(self):
        config.sharepoint = self._sharepoint
        shutil.rmtree(self.staging, ignore_errors=True)

    def test_inventory_excludes_the_trackers_own_output_folder(self):
        self.gc.lib["_statement_tracker/Statement Tracker 2026-08-14.xlsx"] = b"g"
        paths = [f["path"] for f in self.dest.inventory()["files"]]
        self.assertEqual(sorted(paths), ["Acme Fund IV/Acme Q1.pdf", "Merrill/cust_jan.pdf"])

    def test_pull_schedule_reports_absence_on_a_first_run(self):
        self.assertFalse(self.dest.pull_schedule())

    def test_pull_schedule_overwrites_a_stale_local_copy(self):
        # The SharePoint copy is the one a person edits, so it must win.
        staged = os.path.join(self.dest.backend, st.SCHEDULE_FILE)
        with open(staged, "wb") as f:
            f.write(b"stale")
        self.gc.lib[f"_statement_tracker/backend/{st.SCHEDULE_FILE}"] = b"edited-in-sharepoint"
        self.assertTrue(self.dest.pull_schedule())
        with open(staged, "rb") as f:
            self.assertEqual(f.read(), b"edited-in-sharepoint")

    def test_pull_schedule_keeps_staged_copy_when_library_has_none(self):
        staged = os.path.join(self.dest.backend, st.SCHEDULE_FILE)
        with open(staged, "wb") as f:
            f.write(b"local-only")
        self.assertTrue(self.dest.pull_schedule())
        with open(staged, "rb") as f:
            self.assertEqual(f.read(), b"local-only")

    def test_previous_grids_are_swept_into_archive(self):
        self.gc.lib["_statement_tracker/Statement Tracker 2026-08-07.xlsx"] = b"old"
        self.dest.archive_old_grids()
        self.assertIn("_statement_tracker/Archive/Statement Tracker 2026-08-07.xlsx", self.gc.lib)
        self.assertNotIn("_statement_tracker/Statement Tracker 2026-08-07.xlsx", self.gc.lib)

    def test_sweep_does_not_overwrite_an_identically_named_archived_grid(self):
        name = "Statement Tracker 2026-08-14.xlsx"
        self.gc.lib[f"_statement_tracker/{name}"] = b"current"
        self.gc.lib[f"_statement_tracker/Archive/{name}"] = b"already-archived"
        self.dest.archive_old_grids()
        self.assertEqual(self.gc.lib[f"_statement_tracker/Archive/{name}"], b"already-archived")
        self.assertEqual(self.gc.lib["_statement_tracker/Archive/Statement Tracker 2026-08-14__2.xlsx"],
                         b"current")

    def test_sweep_leaves_non_grid_files_alone(self):
        self.gc.lib["_statement_tracker/README.txt"] = b"note"
        self.dest.archive_old_grids()
        self.assertIn("_statement_tracker/README.txt", self.gc.lib)

    def test_grid_name_avoids_names_used_in_either_folder(self):
        base = "Statement Tracker 2026-08-14"
        self.assertEqual(self.dest.grid_name(base), f"{base}.xlsx")
        self.gc.lib[f"_statement_tracker/Archive/{base}.xlsx"] = b"a"
        self.assertEqual(self.dest.grid_name(base), f"{base} (2).xlsx")
        self.gc.lib[f"_statement_tracker/{base} (2).xlsx"] = b"b"
        self.assertEqual(self.dest.grid_name(base), f"{base} (3).xlsx")

    def test_publish_uploads_staged_files_to_the_matching_folders(self):
        with open(os.path.join(self.dest.outdir, "grid.xlsx"), "wb") as f:
            f.write(b"GRID")
        with open(os.path.join(self.dest.backend, "statement_status.csv"), "w") as f:
            f.write("a,b\n")
        self.dest.publish([("grid.xlsx", "grid"),
                           (os.path.join(st.BACKEND, "statement_status.csv"), "csv")])
        self.assertEqual(self.gc.lib["_statement_tracker/grid.xlsx"], b"GRID")
        self.assertEqual(self.gc.lib["_statement_tracker/backend/statement_status.csv"], b"a,b\n")

    def test_publish_skips_entries_with_no_staged_file(self):
        self.dest.publish([("never_written.xlsx", "grid")])
        self.assertNotIn("_statement_tracker/never_written.xlsx", self.gc.lib)

    def test_runtime_state_is_staged_locally_and_never_published(self):
        # The metadata cache and digest state are runtime state: they belong on local
        # disk with the manifest, not in the team's library.
        for name in (st.CACHE_FILE, st.DIGEST_STATE):
            with open(os.path.join(self.dest.backend, name), "w") as f:
                f.write("{}")
        self.dest.publish([(os.path.join(st.BACKEND, "statement_status.csv"), "csv")])
        self.assertFalse([p for p in self.gc.lib if p.endswith(st.CACHE_FILE)])
        self.assertFalse([p for p in self.gc.lib if p.endswith(st.DIGEST_STATE)])


class FreeNameTests(unittest.TestCase):
    def test_suffixes_only_on_collision(self):
        self.assertEqual(st._free_name("g.xlsx", set()), "g.xlsx")
        self.assertEqual(st._free_name("g.xlsx", {"g.xlsx"}), "g__2.xlsx")
        self.assertEqual(st._free_name("g.xlsx", {"g.xlsx", "g__2.xlsx"}), "g__3.xlsx")


if __name__ == "__main__":
    unittest.main()
