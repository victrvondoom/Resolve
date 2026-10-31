"""Creative and Educational workspace adapter for RESOLVE.

Supports deterministic procedural asset generation (cu_holodev) and UI walkthrough
guidance generation (cu_handyman) with independent disk and structural assertions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CreativeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MeshSpec:
    name: str
    dimensions: tuple[float, float, float]  # width, height, depth in units
    segments: int
    material: str


@dataclass(frozen=True)
class WalkthroughStep:
    step_number: int
    target_element_id: str
    bounding_box: tuple[int, int, int, int]  # x, y, width, height
    narration: str
    action_type: str


class CreativeAdapter:
    """Generates validated 3D/vector artifacts and UI walkthroughs."""

    def __init__(self, output_dir: Path | None = None):
        self.out = Path(output_dir or Path.cwd() / 'data' / 'creative')
        self.out.mkdir(parents=True, exist_ok=True)

    def generate_procedural_mesh(self, spec: MeshSpec, filename: str) -> dict[str, Any]:
        """Generate a deterministic procedural Wavefront OBJ asset for Holodev."""
        if any(d <= 0 for d in spec.dimensions):
            raise CreativeError('INVALID_DIMENSIONS', 'All dimensions must be strictly positive')
        if spec.segments < 1:
            raise CreativeError('INVALID_SEGMENTS', 'Segments must be at least 1')
        if not filename or '/' in filename or '\\' in filename or not filename.endswith('.obj'):
            raise CreativeError('INVALID_FILENAME', 'Filename must be a safe relative .obj filename')

        dest = self.out / filename
        w, h, d = spec.dimensions
        hw, hh, hd = w / 2.0, h / 2.0, d / 2.0

        # Construct deterministic cuboid vertices and faces
        vertices = [
            (-hw, -hh, -hd), (hw, -hh, -hd), (hw, hh, -hd), (-hw, hh, -hd),
            (-hw, -hh, hd), (hw, -hh, hd), (hw, hh, hd), (-hw, hh, hd)
        ]
        faces = [
            (1, 2, 3, 4), (5, 8, 7, 6), (1, 5, 6, 2),
            (2, 6, 7, 3), (3, 7, 8, 4), (5, 1, 4, 8)
        ]

        lines = [f"# RESOLVE Holodev Procedural Mesh: {spec.name}", f"o {spec.name}"]
        for vx, vy, vz in vertices:
            lines.append(f"v {vx:.4f} {vy:.4f} {vz:.4f}")
        for f in faces:
            lines.append(f"f {f[0]} {f[1]} {f[2]} {f[3]}")

        content = '\n'.join(lines) + '\n'
        raw = content.encode('utf-8')
        dest.write_bytes(raw)
        file_sha = hashlib.sha256(raw).hexdigest()

        return {
            'filename': filename,
            'path': str(dest),
            'vertex_count': len(vertices),
            'face_count': len(faces),
            'sha256': file_sha,
            'size_bytes': len(raw),
        }

    def compile_walkthrough(self, app_title: str, steps: list[WalkthroughStep]) -> dict[str, Any]:
        """Compile a step-by-step element guidance plan for Handyman."""
        if not steps:
            raise CreativeError('EMPTY_WALKTHROUGH', 'Walkthrough must have at least one step')

        compiled_steps = []
        for s in steps:
            x, y, w, h = s.bounding_box
            if w <= 0 or h <= 0 or x < 0 or y < 0:
                raise CreativeError('INVALID_BOUNDS', f'Step {s.step_number} has invalid bounding box')
            compiled_steps.append({
                'step': s.step_number,
                'target': s.target_element_id,
                'bounds': {'x': x, 'y': y, 'w': w, 'h': h},
                'center': {'x': x + w // 2, 'y': y + h // 2},
                'narration': s.narration,
                'action': s.action_type,
            })

        plan_digest = hashlib.sha256(json.dumps(compiled_steps, sort_keys=True).encode()).hexdigest()
        return {
            'app_title': app_title,
            'step_count': len(compiled_steps),
            'steps': compiled_steps,
            'plan_digest': plan_digest,
        }

