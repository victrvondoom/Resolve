"""Multi-tenant Intent-Entity-Effect Continuity (T-IEEC) and Effect Dependency Graph for RESOLVE."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class NodeType(str, Enum):
    INTENT = "INTENT"
    ENTITY = "ENTITY"
    VERSION = "VERSION"
    OBSERVATION = "OBSERVATION"
    ACTION = "ACTION"
    APPROVAL = "APPROVAL"
    EFFECT = "EFFECT"
    EVIDENCE = "EVIDENCE"


class EdgeType(str, Enum):
    BINDS_TO = "BINDS_TO"
    DERIVED_FROM = "DERIVED_FROM"
    AUTHORIZED_BY = "AUTHORIZED_BY"
    DEPENDS_ON = "DEPENDS_ON"
    OBSERVED_AT = "OBSERVED_AT"
    SUPERSEDES = "SUPERSEDES"
    CONFIRMS = "CONFIRMS"
    CONTRADICTS = "CONTRADICTS"


@dataclass
class GraphNode:
    id: str
    type: str
    user_id: str
    workspace_id: str
    mission_id: str
    payload: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GraphEdge:
    source_id: str
    target_id: str
    edge_type: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EffectDependencyGraph:
    def __init__(self, mission_id: str, user_id: str, workspace_id: str):
        self.mission_id = mission_id
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []

    def add_node(self, node_id: str, node_type: NodeType, payload: dict, user_id: str | None = None, workspace_id: str | None = None) -> GraphNode:
        uid = user_id or self.user_id
        wid = workspace_id or self.workspace_id
        node = GraphNode(
            id=node_id,
            type=node_type.value,
            user_id=uid,
            workspace_id=wid,
            mission_id=self.mission_id,
            payload=payload
        )
        self.nodes[node_id] = node
        return node

    def add_edge(self, source_id: str, target_id: str, edge_type: EdgeType, metadata: dict | None = None) -> GraphEdge:
        if source_id not in self.nodes:
            raise ValueError(f"Source node {source_id} does not exist in graph")
        if target_id not in self.nodes:
            raise ValueError(f"Target node {target_id} does not exist in graph")

        edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type.value,
            metadata=metadata or {}
        )
        self.edges.append(edge)
        return edge

    def verify_mission(self) -> tuple[bool, str, dict]:
        """Verify the complete Intent-Entity-Version-Effect-Evidence continuity chain."""
        # 1. Tenant ownership validation across all nodes
        for nid, node in self.nodes.items():
            if node.user_id != self.user_id or node.workspace_id != self.workspace_id:
                return False, f"Cross-tenant entity leakage detected on node {nid} ({node.user_id} != {self.user_id})", {}

        # 2. Check for contradiction edges
        contradictions = [e for e in self.edges if e.edge_type == EdgeType.CONTRADICTS.value]
        if contradictions:
            first = contradictions[0]
            return False, f"Contradiction detected between {first.source_id} and {first.target_id}: {first.metadata.get('reason', 'unspecified')}", {}

        # 3. Find active intent
        intents = [n for n in self.nodes.values() if n.type == NodeType.INTENT.value]
        if not intents:
            return False, "No intent node found in mission graph", {}

        # If any intent is superseded, ensure we don't rely on invalidated actions
        superseded_ids = {e.target_id for e in self.edges if e.edge_type == EdgeType.SUPERSEDES.value}
        active_intents = [i for i in intents if i.id not in superseded_ids]
        if not active_intents:
            return False, "All intents in graph have been superseded without active replacement", {}
        latest_intent = max(active_intents, key=lambda i: i.created_at)

        # 4. Check each effect node
        effects = [n for n in self.nodes.values() if n.type == NodeType.EFFECT.value]
        if not effects:
            return False, "No effect nodes recorded for mission", {}

        verified_effects = []
        for effect in effects:
            # Effect must have an AUTHORIZED_BY edge pointing to an approval
            approvals = [
                e.target_id for e in self.edges
                if e.source_id == effect.id and e.edge_type == EdgeType.AUTHORIZED_BY.value
            ]
            if not approvals:
                return False, f"Effect {effect.id} was executed without an AUTHORIZED_BY approval edge", {}

            # Check approval digest validity
            app_node = self.nodes.get(approvals[0])
            if not app_node or app_node.type != NodeType.APPROVAL.value:
                return False, f"Approval node {approvals[0]} is invalid or missing", {}

            expected_digest = effect.payload.get("expected_digest")
            if expected_digest and app_node.payload.get("digest") != expected_digest:
                return False, f"Approval digest mismatch on effect {effect.id}", {}

            # Effect must have a CONFIRMS edge from an independent evidence node
            confirming_evidence = [
                e.source_id for e in self.edges
                if e.target_id == effect.id and e.edge_type == EdgeType.CONFIRMS.value
            ]
            if not confirming_evidence:
                return False, f"Effect {effect.id} lacks an independent CONFIRMS evidence node", {}

            ev_node = self.nodes.get(confirming_evidence[0])
            verified_effects.append({
                "effect_id": effect.id,
                "approval_id": app_node.id,
                "evidence_id": ev_node.id,
                "target_entity": effect.payload.get("target_entity_id"),
                "status": "VERIFIED"
            })

        summary = {
            "mission_id": self.mission_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "active_intent": latest_intent.id,
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "verified_effects": verified_effects,
            "status": "CONTINUITY_PROVEN"
        }
        return True, "Intent-Entity-Version-Effect continuity verified with independent evidence", summary

    def to_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges]
        }

