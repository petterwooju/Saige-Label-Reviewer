import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "saige_reviewer" / "static"


class RemoteUploadFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")

    def test_full_workbench_contains_remote_projects_and_resumable_upload(self):
        for value in (
            'id="remoteProjectsDialog"',
            'id="remoteUploadDialog"',
            'webkitdirectory multiple',
            'id="editLock"',
            'id="cancelAnalysis"',
        ):
            self.assertIn(value, self.html)
        self.assertIn("createOrResumeUpload", self.script)
        self.assertIn("X-Chunk-SHA256", self.script)
        self.assertIn("SaigeUploadSha256.hashFile", self.script)
        self.assertIn("refreshRemoteItems", self.script)
        self.assertIn("feature-sample", self.script)
        self.assertIn("/references?", self.script)
        self.assertIn("remoteQueueOffset", self.script)
        self.assertIn("changeRemotePage", self.script)
        self.assertIn("上一页", self.script)
        self.assertIn("下一页", self.script)
        self.assertIn("runtime.mode==='remote'?!remoteProjectId", self.script)
        self.assertIn("只读原件永不覆盖", self.script)

    def test_streaming_sha256_matches_standard_vectors_across_chunk_boundaries(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available on PATH")
        module = STATIC / "upload-sha256.js"
        program = r"""
require(process.argv[1]);
const assert=require('node:assert/strict');
const S=globalThis.SaigeUploadSha256.Sha256;
let empty=new S();
assert.equal(empty.hex(),'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
const bytes=new TextEncoder().encode('abc');
let split=new S(); split.update(bytes.subarray(0,1)); split.update(bytes.subarray(1));
assert.equal(split.hex(),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
let long=new S(); const value=new Uint8Array(1000000).fill(97);
for(let offset=0;offset<value.length;offset+=7777)long.update(value.subarray(offset,offset+7777));
assert.equal(long.hex(),'cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0');
"""
        result = subprocess.run(
            [node, "-e", program, str(module)],
            capture_output=True,
            text=True,
            # Hosted Windows runners can be heavily throttled. Keep the full
            # one-million-byte NIST vector while allowing enough wall time for
            # the pure JavaScript implementation on a slow shared runner.
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
