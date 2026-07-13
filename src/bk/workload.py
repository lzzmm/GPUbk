from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence


_PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?$", re.IGNORECASE)
_TRAIN_WORDS = {"train", "trainer", "training", "pretrain", "pretraining", "finetune", "finetuning", "sft"}
_EVAL_WORDS = {"eval", "evaluate", "evaluation", "test", "benchmark", "bench"}
_DATA_WORDS = {"preprocess", "tokenize", "convert", "prepare", "dataset", "data"}
_PROFILE_WORDS = {"profile", "profiler", "nsys", "ncu"}
_SERVICE_MARKERS = (
    "api_server",
    "serve",
    "server",
    "tritonserver",
    "text-generation-launcher",
    "ollama",
)


@dataclass(frozen=True)
class WorkloadDescriptor:
    """Privacy-safe, extensible description of a GPU workload."""

    launcher: str
    entrypoint_kind: str
    kind: str
    framework: str
    execution: str
    source: str
    confidence: int
    label: str
    signature: str

    def as_dict(self) -> dict:
        return {
            "launcher": self.launcher,
            "entrypoint_kind": self.entrypoint_kind,
            "kind": self.kind,
            "framework": self.framework,
            "execution": self.execution,
            "source": self.source,
            "confidence": self.confidence,
            "label": self.label,
        }


def describe_workload(command: str, managed_summary: Optional[str] = None) -> WorkloadDescriptor:
    """Classify a command without storing paths, argument values, or command text."""
    argv = _split_command(command)
    if not argv:
        return WorkloadDescriptor("unknown", "unknown", "unknown", "unknown", "unknown", "observed", 0, "?", "unknown")

    executable = _basename(argv[0])
    lowered = [item.lower() for item in argv]
    launcher, execution = _launcher(executable, lowered)
    entrypoint_kind, entrypoint = _entrypoint(executable, argv, launcher)
    framework = _framework(executable, lowered, launcher)
    kind = _kind(executable, entrypoint, lowered, framework)
    if managed_summary:
        managed = describe_workload(managed_summary)
        if managed.launcher not in {"unknown", "native"}:
            launcher = managed.launcher
        if managed.entrypoint_kind not in {"unknown", "runtime", "binary"}:
            entrypoint_kind = managed.entrypoint_kind
        if managed.framework != "unknown":
            framework = managed.framework
        if managed.kind != "unknown":
            kind = managed.kind
        if managed.execution != "unknown":
            execution = managed.execution
    confidence = _confidence(kind, framework, launcher, entrypoint_kind)
    source = "inferred" if confidence >= 50 else "coarse"
    label = _safe_label(_display_entrypoint(entrypoint) or executable)

    if managed_summary:
        managed_label = _safe_label(managed_summary)
        if managed_label:
            label = managed_label
            source = "managed"
            confidence = max(confidence, 90)

    signature_material = "\x1f".join(
        (
            argv[0],
            launcher,
            entrypoint_kind,
            entrypoint,
            label if managed_summary else "",
            kind,
            framework,
            execution,
            source,
        )
    )
    signature = hashlib.sha256(signature_material.encode("utf-8", errors="replace")).hexdigest()
    return WorkloadDescriptor(
        launcher=launcher,
        entrypoint_kind=entrypoint_kind,
        kind=kind,
        framework=framework,
        execution=execution,
        source=source,
        confidence=confidence,
        label=label or "?",
        signature=signature,
    )


def _split_command(command: str) -> List[str]:
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.strip().split()
    if not argv:
        return []
    if _basename(argv[0]).lower() == "env":
        index = 1
        while index < len(argv) and "=" in argv[index] and not argv[index].startswith("-"):
            index += 1
        argv = argv[index:]
    return argv[:256]


def _launcher(executable: str, lowered: Sequence[str]) -> tuple[str, str]:
    name = executable.lower()
    joined = " ".join(lowered[:24])
    if name == "torchrun" or "torch.distributed.run" in joined:
        return "torchrun", "distributed"
    if name == "deepspeed" or "deepspeed.launcher" in joined:
        return "deepspeed", "distributed"
    if name == "accelerate" or "accelerate.commands.launch" in joined:
        return "accelerate", "distributed"
    if name in {"mpirun", "mpiexec"}:
        return "mpi", "distributed"
    if name in {"srun", "sbatch"}:
        return "slurm", "distributed"
    if name.startswith("ray::") or name in {"ray", "raylet"}:
        return "ray", "distributed"
    if name in {"jupyter", "jupyter-lab", "jupyter-notebook"} or "ipykernel_launcher" in joined:
        return "jupyter", "interactive"
    if _PYTHON_RE.fullmatch(name):
        return "python", "single"
    if name in {"bash", "sh", "zsh", "fish"}:
        return "shell", "single"
    if name in {"docker", "podman", "singularity", "apptainer"}:
        return "container", "container"
    if name in {"tritonserver", "text-generation-launcher", "ollama"}:
        return "service", "service"
    return "native", "single"


def _entrypoint(executable: str, argv: Sequence[str], launcher: str) -> tuple[str, str]:
    name = executable.lower()
    if launcher == "jupyter":
        return "notebook", "Jupyter kernel"
    if _PYTHON_RE.fullmatch(name):
        if len(argv) > 2 and argv[1] == "-m":
            return "module", argv[2]
        if len(argv) > 1 and argv[1] == "-c":
            return "inline", "python -c"
        script = _first_script(argv[1:])
        return ("script", script) if script else ("runtime", executable)
    if launcher in {"torchrun", "deepspeed", "accelerate", "mpi", "slurm"}:
        script = _first_script(argv[1:])
        if script:
            return "script", script
        module = _module_argument(argv)
        if module:
            return "module", module
        return "runtime", executable
    if launcher == "shell":
        script = _first_script(argv[1:], suffixes=(".sh", ".bash", ".zsh"))
        return ("script", script) if script else ("shell", executable)
    if launcher == "container":
        return "container", executable
    if launcher == "service":
        return "service", executable
    return "binary", executable


def _first_script(argv: Sequence[str], suffixes: tuple[str, ...] = (".py", ".pyw")) -> str:
    for value in argv:
        lowered = value.lower()
        if not value.startswith("-") and lowered.endswith(suffixes):
            return value
    return ""


def _module_argument(argv: Sequence[str]) -> str:
    for index, value in enumerate(argv[:-1]):
        if value in {"-m", "--module"}:
            return argv[index + 1]
    return ""


def _framework(executable: str, lowered: Sequence[str], launcher: str) -> str:
    joined = " ".join(lowered[:32])
    if "vllm" in joined:
        return "vllm"
    if executable.lower() == "tritonserver" or "tritonserver" in joined:
        return "triton"
    if launcher in {"torchrun", "deepspeed"} or "torch.distributed" in joined:
        return "pytorch"
    if "tensorflow" in joined:
        return "tensorflow"
    if re.search(r"(?:^|[. /_-])jax(?:$|[. /_-])", joined):
        return "jax"
    if "tensorrt" in joined or "trtexec" in joined:
        return "tensorrt"
    if "ollama" in joined:
        return "ollama"
    return "unknown"


def _kind(executable: str, entrypoint: str, lowered: Sequence[str], framework: str) -> str:
    searchable = " ".join((executable.lower(), entrypoint.lower(), *lowered[:16]))
    words = set(re.findall(r"[a-z0-9]+", searchable))
    if any(marker in searchable for marker in _SERVICE_MARKERS) or framework in {"vllm", "triton", "ollama"}:
        return "inference-service"
    if "ipykernel" in searchable or "jupyter" in searchable:
        return "interactive"
    if words & _PROFILE_WORDS:
        return "profiling"
    if words & _TRAIN_WORDS:
        return "training"
    if words & _EVAL_WORDS:
        return "evaluation"
    if words & _DATA_WORDS:
        return "data-processing"
    if "infer" in words or "inference" in words or "generate" in words:
        return "inference-batch"
    return "unknown"


def _confidence(kind: str, framework: str, launcher: str, entrypoint_kind: str) -> int:
    score = 20
    if launcher not in {"unknown", "native"}:
        score += 15
    if entrypoint_kind not in {"unknown", "runtime", "binary"}:
        score += 15
    if framework != "unknown":
        score += 20
    if kind != "unknown":
        score += 20
    return min(85, score)


def _basename(value: str) -> str:
    return Path(value).name or value


def _display_entrypoint(value: str) -> str:
    if not value:
        return ""
    if "/" in value or "\\" in value:
        return _basename(value)
    return value


def _safe_label(value: str) -> str:
    normalized = " ".join(str(value).replace("\x00", " ").split())
    normalized = "".join(character for character in normalized if ord(character) >= 32 and ord(character) != 127)
    return normalized[:96]
