import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from effect_graph import EffectDependencyGraph, NodeType, EdgeType


class EffectGraphTests(unittest.TestCase):
    def test_complete_verified_chain(self):
        graph = EffectDependencyGraph("m_101", "usr_alice", "ws_finance")

        # 1. Intent
        i1 = graph.add_node("intent_1", NodeType.INTENT, {"text": "Send Q3 audit report to Alex Morgan"})

        # 2. Entity & Version
        e1 = graph.add_node("ent_alex", NodeType.ENTITY, {"stable_id": "alex-morgan-fin", "display_name": "Alex Morgan"})
        v1 = graph.add_node("ver_q3_v4", NodeType.VERSION, {"entity_id": "ent_alex", "version": "v4", "sha256": "abcdef123456"})
        graph.add_edge("intent_1", "ent_alex", EdgeType.BINDS_TO)
        graph.add_edge("ent_alex", "ver_q3_v4", EdgeType.DEPENDS_ON)

        # 3. Action & Approval
        digest = "digest_approved_v4"
        act1 = graph.add_node("act_dispatch", NodeType.ACTION, {"operation": "send_outbox"})
        app1 = graph.add_node("app_human", NodeType.APPROVAL, {"digest": digest, "authorized_by": "usr_alice"})
        graph.add_edge("act_dispatch", "app_human", EdgeType.AUTHORIZED_BY)

        # 4. Effect & Evidence
        eff1 = graph.add_node("eff_outbox", NodeType.EFFECT, {"target_entity_id": "alex-morgan-fin", "expected_digest": digest})
        ev1 = graph.add_node("ev_db_oracle", NodeType.EVIDENCE, {"oracle": "sqlite_truth_verifier", "verified": True})
        graph.add_edge("eff_outbox", "app_human", EdgeType.AUTHORIZED_BY)
        graph.add_edge("ev_db_oracle", "eff_outbox", EdgeType.CONFIRMS)

        ok, reason, summary = graph.verify_mission()
        self.assertTrue(ok)
        self.assertEqual(summary["status"], "CONTINUITY_PROVEN")
        self.assertEqual(len(summary["verified_effects"]), 1)

    def test_cross_tenant_leak_detection(self):
        graph = EffectDependencyGraph("m_102", "usr_alice", "ws_finance")
        graph.add_node("intent_1", NodeType.INTENT, {"text": "Send report"})
        # Leak from Bob
        graph.add_node("ent_bob", NodeType.ENTITY, {"display_name": "Bob"}, user_id="usr_bob")

        ok, reason, _ = graph.verify_mission()
        self.assertFalse(ok)
        self.assertIn("Cross-tenant entity leakage detected", reason)

    def test_contradiction_edge_refuses_mission(self):
        graph = EffectDependencyGraph("m_103", "usr_alice", "ws_finance")
        graph.add_node("intent_1", NodeType.INTENT, {"text": "Send report"})
        graph.add_node("eff_1", NodeType.EFFECT, {"target": "alex"})
        graph.add_node("obs_1", NodeType.OBSERVATION, {"ui_state": "focus_stolen"})
        graph.add_edge("obs_1", "eff_1", EdgeType.CONTRADICTS, {"reason": "focus stolen by popup during click"})

        ok, reason, _ = graph.verify_mission()
        self.assertFalse(ok)
        self.assertIn("Contradiction detected", reason)

    def test_unapproved_effect_rejected(self):
        graph = EffectDependencyGraph("m_104", "usr_alice", "ws_finance")
        graph.add_node("intent_1", NodeType.INTENT, {"text": "Execute payment"})
        graph.add_node("eff_1", NodeType.EFFECT, {"target": "unapproved"})

        ok, reason, _ = graph.verify_mission()
        self.assertFalse(ok)
        self.assertIn("without an AUTHORIZED_BY approval edge", reason)


if __name__ == '__main__':
    unittest.main()

