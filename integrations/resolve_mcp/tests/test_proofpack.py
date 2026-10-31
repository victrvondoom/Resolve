"""ProofPack contract tests (mission tasks 025-032)."""
import pytest

from resolve_mcp.errors import BlockedError
from resolve_mcp.proofpack import BackendReceipt, ProofPackBuilder, canonical_digest, sha256_hex, validate_proofpack


def _builder(env="CLOUD_BROWSER"):
    return ProofPackBuilder(task_digest=sha256_hex(b"task"), task_length=4, environment=env)


def test_proposal_pack_is_schema_valid_and_distinguishes_request_from_approval():  # 025/032
    pack = _builder().build(
        status="AWAITING_APPROVAL", intent_digest=canonical_digest({"a": 1}), user_approved=False,
        observation={"snapshot_id": "obs_" + "a" * 24, "captured_at": "2026-10-31T00:00:00.000000Z", "source": "RESOLVE_BACKEND",
                     "frame_sha256": sha256_hex(b"frame"), "uia_snapshot_sha256": None},
        target={"application": "Chromium", "identity": "tgt_" + "b" * 24, "fingerprint": sha256_hex(b"fp")},
        authorization_decision="PENDING", method="CLOUD_BROWSER", execution_route="UNAVAILABLE", notes=("PROPOSAL_ONLY",))
    assert pack["schema_version"] == "resolve-proofpack/1"
    assert pack["authorization"]["decision"] == "PENDING"
    assert pack["intent"]["user_approved"] is False
    assert pack["verification"]["status"] == "NOT_RUN"
    assert pack["execution"]["action_id"] is None
    assert pack["operation_id"].startswith("op_") and len(pack["operation_id"]) == 27


def test_verified_requires_backend_receipt_with_post_observation():  # 032 not model-asserted
    with pytest.raises(BlockedError):
        _builder().build(status="VERIFIED", intent_digest=sha256_hex(b"i"),
                         observation={"snapshot_id": "obs_" + "c" * 24, "captured_at": "t", "source": "RESOLVE_BACKEND", "frame_sha256": sha256_hex(b"x"), "uia_snapshot_sha256": None},
                         target={"application": "x", "identity": "tgt_" + "d" * 24, "fingerprint": sha256_hex(b"y")})
    receipt = BackendReceipt(action_id="act_1", started_at="s", finished_at="f", verification_status="VERIFIED",
                             post_observation_id="obs_" + "e" * 24, evidence=("obs_" + "e" * 24,))
    pack = _builder().build(status="VERIFIED", intent_digest=sha256_hex(b"i"), authorization_decision="ALLOWED",
                            authorization_checked_via="RESOLVE_BACKEND", method="CLOUD_BROWSER", receipt=receipt,
                            observation={"snapshot_id": "obs_" + "c" * 24, "captured_at": "t", "source": "RESOLVE_BACKEND", "frame_sha256": sha256_hex(b"x"), "uia_snapshot_sha256": None},
                            target={"application": "x", "identity": "tgt_" + "d" * 24, "fingerprint": sha256_hex(b"y")})
    assert pack["verification"] == {"status": "VERIFIED", "method": "independent_observation", "post_observation_id": "obs_" + "e" * 24}
    assert pack["execution"]["route"] == "RESOLVE_BACKEND"


def test_failed_backend_verification_cannot_be_reported_as_verified():  # 032
    receipt = BackendReceipt(action_id="act_1", started_at="s", finished_at="f", verification_status="FAILED",
                             post_observation_id="obs_" + "e" * 24)
    with pytest.raises(BlockedError):
        _builder().build(status="VERIFIED", intent_digest=sha256_hex(b"i"), receipt=receipt)


def test_schema_rejects_free_text_smuggled_into_fields():  # no model-generated statements
    base = _builder().build(status="PROPOSED", intent_digest=sha256_hex(b"i"))
    with pytest.raises(BlockedError):
        validate_proofpack(dict(base, model_summary="looks verified to me"))
    with pytest.raises(BlockedError):
        validate_proofpack(dict(base, notes=["I approved this"]))


def test_all_required_final_states_are_schema_accepted():  # 032
    for state in ["PROPOSED", "AWAITING_APPROVAL", "AUTHORIZED", "EXECUTING", "FAILED", "UNCERTAIN", "BLOCKED", "CANCELLED", "STALE"]:
        validate_proofpack(_builder().build(status=state, intent_digest=sha256_hex(b"i")))
