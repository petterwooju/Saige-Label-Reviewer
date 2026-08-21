import json
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from saige_reviewer.demo import make_demo
from saige_reviewer.session import MAX_HISTORY, Command, ReviewSession


class ReviewSessionTests(unittest.TestCase):
    @staticmethod
    def _unanalyzed_dataset():
        dataset = make_demo()
        for item in dataset.items:
            item.analysis_state = "not_analyzed"
            item.suspicion_score = None
            item.suggested_label = None
            item.metadata.pop("label_confidence", None)
            item.metadata.pop("neighbor_support", None)
        return dataset

    def test_stages_change_and_supports_undo_redo(self):
        session = ReviewSession(make_demo())
        item = session.dataset.items[0]
        original = item.label
        replacement = session.dataset.classes[1]
        session.update(item.id, label=replacement, status="modified")
        self.assertEqual((item.label, item.status), (replacement, "modified"))
        self.assertTrue(session.undo())
        self.assertEqual((item.label, item.status), (original, "pending"))
        self.assertTrue(session.redo())
        self.assertEqual((item.label, item.status), (replacement, "modified"))

    def test_invalid_class_is_rejected(self):
        session = ReviewSession(make_demo())
        with self.assertRaises(ValueError):
            session.update("0", label="不存在")

    def test_invalid_status_is_rejected(self):
        session = ReviewSession(make_demo())
        with self.assertRaises(ValueError):
            session.update("0", status="invalid")

    def test_export_contains_only_reviewed_items(self):
        session = ReviewSession(make_demo())
        session.update("0", status="correct")
        payload = session.export_payload()
        self.assertEqual(payload["format"], "saige-review-session/v1")
        self.assertEqual([change["id"] for change in payload["changes"]], ["0"])

    def test_export_includes_label_change_even_if_status_is_pending(self):
        session = ReviewSession(make_demo())
        session.update("0", label=session.dataset.classes[1])
        self.assertEqual([change["id"] for change in session.export_payload()["changes"]], ["0"])

    def test_browser_payload_omits_heavy_annotation_metadata(self):
        session = ReviewSession(make_demo())
        item = session.dataset.items[0]
        item.metadata.update({
            "contours": [[[float(index), float(index)] for index in range(1000)]],
            "labelContour": "large-legacy-value",
            "label_confidence": 0.75,
            "neighbor_support": 0.6,
        })
        public = session.payload()["items"][0]
        self.assertEqual(public["metadata"], {"label_confidence": 0.75, "neighbor_support": 0.6})
        self.assertNotIn("contours", json.dumps(public))

    def test_session_round_trip_restores_staged_changes_and_history(self):
        session = ReviewSession(make_demo())
        session.update("0", label=session.dataset.classes[1], status="modified")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            # Demo sessions intentionally have no source but the state object itself remains serializable.
            session.save(path)
            restored = ReviewSession(make_demo())
            self.assertTrue(restored.restore(path))
            self.assertEqual(restored.dataset.items[0].status, "modified")
            self.assertTrue(restored.undo())

    def test_id_index_and_incremental_summary_stay_consistent(self):
        session = ReviewSession(make_demo())
        item = session.dataset.items[0]
        self.assertIs(session._item(item.id), item)
        before = session.summary_payload()["status_counts"]["pending"]
        replacement = session.dataset.classes[1]
        session.update(item.id, label=replacement, status="modified")
        summary = session.summary_payload()
        self.assertEqual(summary["status_counts"]["pending"], before - 1)
        self.assertEqual(summary["status_counts"]["modified"], 1)
        self.assertEqual(summary["changed_count"], 1)
        self.assertEqual(session.undo(), item.id)
        self.assertEqual(session.summary_payload()["changed_count"], 0)
        self.assertEqual(session.redo(), item.id)
        self.assertEqual(session.summary_payload()["changed_count"], 1)

    def test_duplicate_ids_are_rejected_when_building_index(self):
        dataset = make_demo()
        dataset.items.append(dataset.items[0])
        with self.assertRaises(ValueError):
            ReviewSession(dataset)

    def test_save_skips_unchanged_human_state_and_ignores_analysis_fields(self):
        session = ReviewSession(self._unanalyzed_dataset())
        item = session.dataset.items[0]
        item.suspicion_score = 88
        item.suggested_label = session.dataset.classes[0]
        item.analysis_state = "analyzed"
        item.metadata.update(label_confidence=0.12, neighbor_support=0.34)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            self.assertTrue(session.save(path))
            self.assertFalse(session.save(path))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["items"], {})
            restored = ReviewSession(self._unanalyzed_dataset())
            self.assertTrue(restored.restore(path))
            self.assertIsNone(restored.dataset.items[0].suspicion_score)
            self.assertIsNone(restored.dataset.items[0].suggested_label)
            self.assertEqual(restored.dataset.items[0].analysis_state, "not_analyzed")

    def test_corrupt_item_state_is_rejected_without_partial_mutation(self):
        session = ReviewSession(make_demo())
        session.update("0", status="correct")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            session.save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["items"]["1"] = {"label": session.dataset.items[1].label, "status": "corrupt"}
            path.write_text(json.dumps(payload), encoding="utf-8")
            restored = ReviewSession(make_demo())
            self.assertFalse(restored.restore(path))
            self.assertTrue(all(item.status == "pending" for item in restored.dataset.items))

    def test_invalid_history_is_discarded_but_valid_item_state_is_restored(self):
        session = ReviewSession(make_demo())
        session.update("0", status="correct")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            session.save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["undo"][0]["item_id"] = "missing"
            path.write_text(json.dumps(payload), encoding="utf-8")
            restored = ReviewSession(make_demo())
            self.assertTrue(restored.restore(path))
            self.assertEqual(restored.dataset.items[0].status, "correct")
            self.assertFalse(restored.undo())

    def test_analysis_updates_are_validated_before_any_item_is_changed(self):
        session = ReviewSession(make_demo())
        updates = [{
            "id": item.id,
            "x": index + 0.1,
            "y": index + 0.2,
            "suspicion_score": 25.0,
            "suggested_label": item.label,
            "label_confidence": 0.75,
            "neighbor_support": 0.8,
        } for index, item in enumerate(session.dataset.items)]
        invalid = updates[:-1]
        original = [(item.x, item.y, item.analysis_state) for item in session.dataset.items]
        with self.assertRaises(ValueError):
            session.apply_analysis_updates(invalid)
        self.assertEqual([(item.x, item.y, item.analysis_state) for item in session.dataset.items], original)
        self.assertEqual(session.apply_analysis_updates(updates), len(session.dataset.items))
        self.assertTrue(all(item.analysis_state == "analyzed" for item in session.dataset.items))

    def test_external_preview_digest_is_private_and_stale_clear_is_session_wide(self):
        dataset = self._unanalyzed_dataset()
        dataset.source_type = "srproj:detection"
        session = ReviewSession(dataset)
        original_coordinates = [(item.x, item.y) for item in dataset.items]
        digest = "a" * 64
        updates = [{
            "id": item.id,
            "x": index + 100.0,
            "y": index + 200.0,
            "suspicion_score": 25.0,
            "suggested_label": item.label,
            "label_confidence": 0.75,
            "neighbor_support": 0.8,
            "_analysis_preview_sha256": digest,
        } for index, item in enumerate(dataset.items)]
        session.apply_analysis_updates(updates)
        self.assertTrue(all(
            item.metadata["_analysis_preview_sha256"] == digest for item in dataset.items
        ))
        self.assertNotIn("_analysis_preview_sha256", json.dumps(session.payload()))
        self.assertEqual(session.clear_analysis_results(), len(dataset.items))
        self.assertEqual([(item.x, item.y) for item in dataset.items], original_coordinates)
        self.assertTrue(all(
            item.analysis_state == "not_analyzed" and item.suspicion_score is None and
            item.suggested_label is None and "_analysis_preview_sha256" not in item.metadata
            for item in dataset.items
        ))
        self.assertEqual(session.summary_payload()["analysis_state_counts"]["analyzed"], 0)

    def test_review_mutations_can_be_rolled_back_without_losing_redo_history(self):
        session = ReviewSession(make_demo())
        session.update("0", status="correct")
        session.undo()
        self.assertTrue(session.redo_stack)
        mutation = session.stage_update("0", status="uncertain")
        session.rollback_review(mutation)
        self.assertEqual(session.dataset.items[0].status, "pending")
        self.assertFalse(session.undo_stack)
        self.assertTrue(session.redo_stack)
        undo_redo = session.stage_redo()
        session.rollback_review(undo_redo)
        self.assertEqual(session.dataset.items[0].status, "pending")
        self.assertFalse(session.undo_stack)
        self.assertTrue(session.redo_stack)

    def test_history_is_bounded_and_failed_save_can_restore_evicted_entry(self):
        session = ReviewSession(make_demo())
        item = session.dataset.items[0]
        old = Command(item.id, item.label, item.label, "pending", "correct")
        session.undo_stack = [old] * MAX_HISTORY
        mutation = session.stage_update(item.id, status="uncertain")
        self.assertEqual(len(session.undo_stack), MAX_HISTORY)
        self.assertIs(mutation.dropped_undo, old)
        session.rollback_review(mutation)
        self.assertEqual(len(session.undo_stack), MAX_HISTORY)
        self.assertIs(session.undo_stack[0], old)
        self.assertEqual(item.status, "pending")

    def test_failed_atomic_replace_leaves_state_dirty_and_retryable(self):
        session = ReviewSession(make_demo())
        session.update("0", status="correct")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            with patch("saige_reviewer.session.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    session.save(path)
            self.assertFalse(path.exists())
            self.assertFalse(list(Path(directory).glob("*.tmp-*")))
            self.assertTrue(session.save(path))
            self.assertTrue(path.exists())

    def test_failed_state_write_cleans_temporary_file(self):
        session = ReviewSession(make_demo())
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            with patch("pathlib.Path.write_text", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    session.save(path)
            self.assertFalse(path.exists())
            self.assertFalse(list(Path(directory).glob("*.tmp-*")))

    def test_old_placeholder_scores_are_cleared_during_restore(self):
        session = ReviewSession(self._unanalyzed_dataset())
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            session.save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["items"]["0"] = {
                "label": session.dataset.items[0].label,
                "status": "pending",
                "suspicion_score": 91.5,
                "suggested_label": session.dataset.classes[1],
                "analysis_metadata": {},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            restored = ReviewSession(self._unanalyzed_dataset())
            self.assertTrue(restored.restore(path))
            item = restored.dataset.items[0]
            self.assertEqual(item.analysis_state, "not_analyzed")
            self.assertIsNone(item.suspicion_score)
            self.assertIsNone(item.suggested_label)

    def test_inconsistent_not_analyzed_metadata_is_cleared(self):
        session = ReviewSession(self._unanalyzed_dataset())
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            session.save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["items"]["0"] = {
                "label": session.dataset.items[0].label,
                "status": "pending",
                "analysis_state": "not_analyzed",
                "suspicion_score": 50,
                "suggested_label": session.dataset.classes[0],
                "analysis_metadata": {"label_confidence": 0.5, "neighbor_support": 0.5},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            restored = ReviewSession(self._unanalyzed_dataset())
            self.assertTrue(restored.restore(path))
            item = restored.dataset.items[0]
            self.assertIsNone(item.suspicion_score)
            self.assertIsNone(item.suggested_label)
            self.assertNotIn("label_confidence", item.metadata)
            self.assertNotIn("neighbor_support", item.metadata)

    def test_legacy_v1_state_is_rejected_instead_of_guessing_item_identity(self):
        session = ReviewSession(make_demo())
        session.update("0", status="correct")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            session.save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["format"] = "saige-review-state/v1"
            payload.pop("layout_hash")
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(ReviewSession(make_demo()).restore(path))

    def test_layout_hash_rejects_reordered_or_metadata_changed_items(self):
        session = ReviewSession(make_demo())
        session.update("0", status="correct")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "state.json"
            session.save(path)
            reordered = make_demo()
            reordered.items.reverse()
            self.assertFalse(ReviewSession(reordered).restore(path))
            changed = make_demo()
            changed.items[0].metadata["bbox"] = [1, 2, 3, 4]
            self.assertFalse(ReviewSession(changed).restore(path))

    def test_layout_hash_ignores_runtime_analysis_metadata(self):
        original = make_demo()
        changed = make_demo()
        changed.items[0].metadata.update(label_confidence=0.1, neighbor_support=0.2,
                                         analysis_cache_key="runtime")
        self.assertEqual(ReviewSession(original).layout_hash, ReviewSession(changed).layout_hash)


if __name__ == "__main__":
    unittest.main()
