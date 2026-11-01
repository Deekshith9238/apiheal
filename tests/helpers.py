import json

from apiheal.core.models import Outcome
from apiheal.repair.dependency_edit import TARGET_OPENAI
from apiheal.verification.process import ProcessResult
from apiheal.verification.runner import verify_candidate

LEGACY_IMAGE = "sha256:" + "1" * 64
TARGET_IMAGE = "sha256:" + "2" * 64


class FakeRunner:
    """Protocol simulator. Its passes are not evidence of real container execution."""

    def __init__(self, *, failure=None, malformed=False, mismatch=False, count=None):
        self.calls = []
        self.failure = failure
        self.malformed = malformed
        self.mismatch = mismatch
        self.count = count

    def check_available(self):
        pass

    def resolve_image(self, version):
        return LEGACY_IMAGE if version.startswith("0.") else TARGET_IMAGE

    def execute(self, image, snapshot, request):
        self.calls.append((image, request))
        action = request["action"]
        gate = (
            "baseline_contract"
            if image == LEGACY_IMAGE
            else ("sdk_contract" if action == "contract" else action)
        )
        if self.malformed:
            return ProcessResult(0, "not valid evidence")
        facts = []
        if action == "contract":
            facts = [
                {
                    "target": target,
                    "case": index,
                    "request": {"body": {"temperature": 0}},
                    "returned": f"case {index}",
                }
                for target in request["targets"]
                for index in range(3)
            ]
            if self.mismatch and image == TARGET_IMAGE:
                facts[0]["returned"] = "regression"
        if action == "expected_failure":
            facts = [
                {"target": target, "exception": "APIRemovedInV1"} for target in request["targets"]
            ]
        count = (
            (len(facts) if facts else 1)
            if action in {"contract", "expected_failure", "unit"}
            else None
        )
        if self.count is not None and action in {"contract", "expected_failure", "unit"}:
            count = self.count
        passed = gate != self.failure
        payload = {
            "schema_version": 1,
            "action": action,
            "outcome": "passed" if passed else "failed",
            "test_count": count,
            "facts": facts,
            "error": "Synthetic gate failure",
            "dependencies": {"openai": "0.28.1" if image == LEGACY_IMAGE else TARGET_OPENAI},
        }
        return ProcessResult(0 if passed else 1, json.dumps(payload), duration=0.01)


def verified_run(candidate_run):
    repository, baseline, candidate, result, store = candidate_run
    runner = FakeRunner()
    verify_candidate(baseline, candidate, result, store, runner)
    result.outcome = Outcome.VERIFIED
    return repository, candidate, result, store, runner
