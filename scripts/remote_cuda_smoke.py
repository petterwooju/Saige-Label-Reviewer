"""Run a real, offline DINOv2 remote-queue smoke test on the local CUDA GPU."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image, ImageDraw

from saige_reviewer.remote_server import RemoteJobManager


OWNER = "smoke-test@saigeai.com"


def image_bytes(index: int, label: str) -> bytes:
    image = Image.new("RGB", (96, 96), (244, 244, 238))
    draw = ImageDraw.Draw(image)
    if label == "cat":
        offset = 12 + index
        draw.rectangle((offset, 18, 74, 78), fill=(194, 73, 55))
    else:
        offset = 44 + index
        draw.ellipse((18, 18, offset + 30, offset + 30), fill=(49, 91, 180))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=92)
    return output.getvalue()


def make_project(path: Path) -> bytes:
    files = []
    images: dict[str, bytes] = {}
    for index in range(6):
        label = "cat" if index < 3 else "dog"
        name = f"images/{index}.jpg"
        images[name] = image_bytes(index, label)
        files.append(
            {
                "filePath": name,
                "labelDataList": [
                    {
                        "labelId": index + 1,
                        "className": label,
                        "labelPosX": 8,
                        "labelPosY": 8,
                        "labelWidth": 80,
                        "labelHeight": 80,
                    }
                ],
            }
        )
    project = {
        "project": {
            "projectName": "remote CUDA smoke",
            "projectType": "det",
            "classInfos": [{"className": "cat"}, {"className": "dog"}],
            "projectFiles": files,
        }
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project.json", json.dumps(project))
        for name, payload in images.items():
            archive.writestr(name, payload)
    return path.read_bytes()


def main() -> None:
    workspace = ROOT / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="remote-cuda-smoke-", dir=workspace) as directory:
        root = Path(directory)
        payload = make_project(root / "smoke.visionproj")
        manager = RemoteJobManager(root / "service")
        try:
            count = (len(payload) + manager.chunk_size - 1) // manager.chunk_size
            job = manager.create_upload(OWNER, "smoke.visionproj", len(payload), count)
            for index in range(count):
                chunk = payload[index * manager.chunk_size:(index + 1) * manager.chunk_size]
                manager.write_chunk(
                    OWNER, job["id"], index, chunk, hashlib.sha256(chunk).hexdigest()
                )
            manager.complete_upload(OWNER, job["id"], hashlib.sha256(payload).hexdigest())
            manager._queue.join()
            result = manager.get_job(OWNER, job["id"])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["state"] != "completed":
                raise SystemExit("remote analysis did not complete")
            if result["summary"].get("device") != "cuda":
                raise SystemExit("remote analysis did not execute on CUDA")
        finally:
            manager.close()


if __name__ == "__main__":
    main()
