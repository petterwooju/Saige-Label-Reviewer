"""Import completed v0.0.1 remote visionproj jobs into the v0.1.0 project store.

The command is dry-run by default. It never deletes or modifies the legacy root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saige_reviewer.remote_projects import (  # noqa: E402
    CHUNK_SIZE,
    RemoteProjectService,
    RemoteRuntimeConfig,
    _json_text,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def legacy_jobs(root: Path):
    for job_file in sorted((root / "jobs").glob("*/job.json")):
        value = json.loads(job_file.read_text(encoding="utf-8"))
        source = job_file.parent / "input.visionproj"
        if value.get("state") == "completed" and source.is_file():
            yield job_file.parent, value, source


def import_analysis(service: RemoteProjectService, project_id: str, results: Path, actor: str):
    if not results.is_file():
        return False
    source = sqlite3.connect(results)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute("SELECT * FROM items ORDER BY id").fetchall()
    finally:
        source.close()
    with service._lock:
        session = service._load_session_locked(project_id)
        if len(rows) != len(session.dataset.items):
            return False
        updates = [
            {
                "id": row["id"],
                "x": row["x"],
                "y": row["y"],
                "suspicion_score": row["suspicion_score"],
                "suggested_label": row["suggested_label"],
                "label_confidence": row["label_confidence"],
                "neighbor_support": row["neighbor_support"],
            }
            for row in rows
        ]
        mutation = session.stage_analysis_updates(updates)
        run_id = uuid.uuid4().hex
        now = time.time()
        try:
            with service._connect() as connection:
                connection.executemany(
                    "INSERT INTO analysis_items VALUES(?,?,?)",
                    ((project_id, update["id"], _json_text(update)) for update in updates),
                )
                for update in updates:
                    service._update_project_item(
                        connection, project_id, session._items_by_id[str(update["id"])]
                    )
                connection.execute(
                    "INSERT INTO analysis_runs VALUES(?,?,?,'completed',?,?,1,?,NULL,?,?,?, ?,?)",
                    (
                        run_id,
                        project_id,
                        actor,
                        _json_text({"migrated_from": "v0.0.1"}),
                        0,
                        "已迁移旧版分析结果",
                        _json_text({"migrated_from": "v0.0.1", "requires_reanalysis": True}),
                        now,
                        now,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE projects SET revision=1,updated_at=? WHERE id=?",
                    (now, project_id),
                )
                service._audit(
                    connection,
                    actor,
                    "project.migrate-analysis",
                    project_id,
                    1,
                    details={"legacy": "v0.0.1", "requires_reanalysis": True},
                )
        except Exception:
            session.rollback_analysis(mutation)
            raise
    return True


def import_job(service: RemoteProjectService, job_root: Path, job: dict, source: Path):
    actor = str(job.get("owner") or "legacy@saigeai.com").strip().lower()
    digest = sha256(source)
    with service._connect() as connection:
        existing = connection.execute(
            "SELECT id FROM projects WHERE source_sha256=? AND uploader=?",
            (digest, actor),
        ).fetchone()
    if existing:
        return existing["id"], False, False
    upload = service.create_upload(
        actor,
        {
            "name": Path(str(job.get("filename") or source.name)).stem,
            "format": "visionproj",
            "total_size": source.stat().st_size,
            "file_count": 1,
            "primary_path": f"project/{source.name}",
        },
    )
    registered = service.register_files(
        actor,
        upload["id"],
        [
            {
                "logical_path": f"project/{source.name}",
                "role": "primary",
                "size": source.stat().st_size,
                "chunk_count": max(1, (source.stat().st_size + CHUNK_SIZE - 1) // CHUNK_SIZE),
                "sha256": digest,
            }
        ],
    )
    file_id = registered["files"][0]["file_id"]
    with source.open("rb") as stream:
        index = 0
        while True:
            payload = stream.read(CHUNK_SIZE)
            if not payload and index:
                break
            service.write_chunk(
                actor,
                upload["id"],
                file_id,
                index,
                payload,
                hashlib.sha256(payload).hexdigest(),
            )
            index += 1
            if not payload:
                break
    project = service.complete_upload(actor, upload["id"])["project"]
    analysis = import_analysis(service, project["id"], job_root / "results.sqlite3", actor)
    return project["id"], True, analysis


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--storage-root", type=Path, default=Path(r"E:\remote\SaigeLabelReviewer"))
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    jobs = list(legacy_jobs(arguments.legacy_root.resolve()))
    print(f"Completed legacy projects discovered: {len(jobs)}")
    if not arguments.apply:
        print("Dry run only. Re-run with --apply after local acceptance.")
        return
    service = RemoteProjectService(RemoteRuntimeConfig.load(arguments.storage_root), start_workers=False)
    try:
        for job_root, job, source in jobs:
            project_id, created, analysis = import_job(service, job_root, job, source)
            print(
                f"{project_id}: {'imported' if created else 'already present'}; "
                f"legacy analysis {'marked for reanalysis' if analysis else 'not imported'}"
            )
    finally:
        service.close()


if __name__ == "__main__":
    main()
