"""The single definition of where run artifacts live."""

from pathlib import Path

LEDGER_FILENAME = "cost_ledger.sqlite"


def ledger_path(root: Path = Path("outputs")) -> Path:
    return root / LEDGER_FILENAME


class RunLayout:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def manifest_file(self) -> Path:
        return self.run_dir / "manifest.json"

    def job_dir(self, stream_id: str, arm: str) -> Path:
        return self.run_dir / stream_id / arm

    def _session_file(self, stream_id: str, arm: str, kind: str, session: int) -> Path:
        return self.job_dir(stream_id, arm) / kind / f"session_{session:03d}.jsonl"

    def results_file(self, stream_id: str, arm: str, session: int) -> Path:
        return self._session_file(stream_id, arm, "results", session)

    def probes_file(self, stream_id: str, arm: str, session: int) -> Path:
        return self._session_file(stream_id, arm, "probes", session)

    def consolidation_file(self, stream_id: str, arm: str, session: int) -> Path:
        return self._session_file(stream_id, arm, "consolidations", session)

    def status_file(self, stream_id: str, arm: str) -> Path:
        return self.job_dir(stream_id, arm) / "status.json"

    def transcripts_dir(self, stream_id: str, arm: str) -> Path:
        return self.job_dir(stream_id, arm) / "transcripts"

    def transcript_file(self, stream_id: str, arm: str, session: int) -> Path:
        return self.transcripts_dir(stream_id, arm) / f"session_{session:03d}.txt"

    def memory_dir(self, stream_id: str, arm: str) -> Path:
        return self.job_dir(stream_id, arm) / "memory"

    def index_dir(self, stream_id: str, arm: str) -> Path:
        return self.job_dir(stream_id, arm) / "index"

    def snapshot_dir(self, stream_id: str, arm: str, session: int) -> Path:
        return self.job_dir(stream_id, arm) / "snapshots" / f"session_{session:03d}"
