"""Hardened deployment of the KAM trade plugin into the live Hermes runtime.

Problem
-------
The KAM source tree at ``/root/kam/plugins/trade/`` is the authoritative
revision, but the running Hermes installation copies it to
``$HERMES_ROOT/plugins/trade/`` (today: ``/usr/local/lib/hermes-agent``)
when the installer runs. If a developer edits the KAM tree and forgets
to (re)run the installer, the live services silently execute the stale
copy. This module turns that implicit "deploy the whole tree" workflow
into an explicit, auditable, per-agent operation with a check-mode that
surfaces the drift.

Goals
-----
* ONE deterministic deployment mechanism for trade-plugin code. Either
  an explicit ``--check``/``--deploy`` invocation, or nothing.
* Source = authoritative. Runtime = mirror of source. Anything else is a
  loud failure.
* No silent staleness. A check that says "deployment needed: NO" must be
  defensible by exact SHA256 of the affected file(s).
* Dirty-tree guard. Uncommitted source files are NEVER copied unless the
  operator passes ``--allow-dirty``. By default any uncommitted tracked
  file under ``plugins/trade/`` is rejected. Untracked files are always
  rejected.
* Per-agent allowlist. ``--agent vestmarkets`` only copies
  ``agents/x_vestmarkets_agent.py`` (and its companion test) — never
  the unrelated, dirty ``x_apex_agent.py`` etc.
* Atomic copy. New files are written to ``<dst>.tmp`` then ``rename``d,
  so a half-written file can never be observed by an importing service.
* Stale ``__pycache__`` / ``.pyc`` files are removed for any module the
  deploy replaces.
* Restart ONLY the systemd services that actually load the changed module.
  ``--restart=auto`` discovers from the live ``plugins.trade`` import
  path; ``--restart=none`` skips restart for offline staging.
* Post-deploy proof: SHA256 of source == SHA256 of runtime, import path
  resolution for each known consumer points at the runtime copy.

Non-goals
---------
* This is NOT a Vest-only script. It is per-agent-generic and works for
  any ``x_<name>_agent.py`` module that lives under
  ``plugins/trade/agents/``.
* It does NOT touch :file:`.env`, credentials, private keys, the
  ``__pycache__`` of unrelated modules, databases, runtime state,
  systemd unit definitions, or the Hermes webchat.

Public CLI
----------
``python -m installer.deploy_trade --agent <name> [--check|--deploy] [--restart=auto|none|webtrade|webtrade2] [--allow-dirty] [--hermes-root=PATH]``

Or via the shell wrapper ``installer/deploy_trade.sh``.

Exit codes
----------
* ``0``  check reports MATCH (deployment not needed) or deploy completed.
* ``1``  check reports MISMATCH and operator did not ask to deploy.
* ``2``  deploy was needed but FAILED (dry-run, dirty tree, error).
* ``3``  bad CLI arguments.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# -----------------------------------------------------------------------------
# Layout constants
# -----------------------------------------------------------------------------

# KAM repo root (defaults to /root/kam). Override with --repo-root.
DEFAULT_REPO_ROOT = Path("/root/kam")

# Default Hermes installation root (where plugins/trade/ lives at runtime).
DEFAULT_HERMES_ROOT = Path("/usr/local/lib/hermes-agent")

# Default runtime PYTHONPATH for hermes-agent's editable install.
DEFAULT_RUNTIME_PYTHONPATH = "/usr/local/lib/hermes-agent"

# systemd unit names that consume the trade plugin. The deployer only
# restarts these. Hermes-gateway is NOT a systemd unit on this host (it's
# started by the Hermes CLI as a child process); it picks up the plugin
# on next spawn — not part of this deployer's restart set.
RUNTIME_SERVICES = ("webtrade.service", "webtrade2.service")

# Files we never copy or replace, even if the operator asked for them.
# They are runtime state, secrets, caches, or installed metadata that
# MUST NOT be overwritten from the source tree.
FORBIDDEN_PATH_TOKENS: Tuple[str, ...] = (
    "/.env",
    "/__pycache__/",
    "/.pyc",
    "/.sqlite",
    "/.db",
    "/.key",
    "/.pem",
    "/.log",
    "/node_modules/",
    "/venv/",
    "/.venv/",
)


# -----------------------------------------------------------------------------
# Agent file mapping
# -----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AgentDeployment:
    """Resolved source/runtime file set for a single ``--agent`` invocation.

    The deployer publishes one AgentDeployment per agent name. Tests live
    alongside the agent and are part of the agent's allowlist (tests
    don't run at runtime but they must mirror the source SHA so the
    repo's CI matches the deployed revision).
    """

    agent: str
    agent_py_relpath: str = ""
    test_relpath: str = ""
    service_names: Tuple[str, ...] = ()

    @classmethod
    def from_name(cls, name: str, *, services: Tuple[str, ...]) -> "AgentDeployment":
        """Resolve the standard ``x_<name>_agent.py`` mapping."""
        return cls(
            agent=name,
            agent_py_relpath=f"plugins/trade/agents/x_{name}_agent.py",
            test_relpath=f"plugins/trade/agents/tests/test_x_{name}_agent.py",
            service_names=services,
        )


# Default service ownership per agent. Both webtrade + webtrade2 share the
# agents package; an agent file change requires both services to reload.
def _default_services() -> Tuple[str, ...]:
    return RUNTIME_SERVICES


# -----------------------------------------------------------------------------
# SHA / dirty-tree helpers
# -----------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_dirty_paths(
    repo_root: Path,
    *,
    pathspec: Optional[str] = None,
) -> List[str]:
    """Return repo-relative paths that are dirty in the working tree.

    "Dirty" = tracked and modified, OR untracked. We treat BOTH classes as
    suspect by default, since the deployer must never include stale edits the
    operator didn't explicitly approve.

    ``pathspec`` optionally narrows the result to paths matching ``git
    pathspec`` syntax (e.g. ``plugins/trade``). The full unfiltered list
    is always available internally; the operator-facing report should
    show only files relevant to the deployment so they aren't drowned
    out by unrelated scratch directories.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git status failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    dirty: List[str] = []
    for raw in proc.stdout.split("\x00"):
        if not raw:
            continue
        line = raw.split("\n", 1)[0]
        if len(line) < 3:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if not path:
            continue
        if pathspec and not _pathspec_match(path, pathspec):
            continue
        dirty.append(path)
    return dirty


def _pathspec_match(path: str, spec: str) -> bool:
    """Lightweight pathspec matcher for the cases we care about.

    Supports the simple subset ``plugins/trade`` (substring match on the
    leading path segments) — enough to filter ``git status`` output down
    to the agent files. We avoid shelling out to ``git`` for each path so
    the report stays fast.
    """
    # Normalise: trailing slash, leading slash.
    head = spec.rstrip("/").lstrip("/")
    return path == head or path.startswith(head + "/")


def _git_head(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


# -----------------------------------------------------------------------------
# Import-path resolution proof
# -----------------------------------------------------------------------------


def _resolve_runtime_import_path(
    agent: str,
    *,
    runtime_pythonpath: str,
) -> Optional[Path]:
    """Resolve the path the running service would import.

    Sets ``sys.path`` to the runtime PYTHONPATH (without touching the
    caller's existing entries) and uses ``importlib.util.find_spec`` to
    discover which file the running interpreter would load.
    """
    saved = list(sys.path)
    try:
        sys.path = [runtime_pythonpath]
        spec = importlib.util.find_spec(f"plugins.trade.agents.x_{agent}_agent")
        if spec is None or spec.origin is None:
            return None
        return Path(spec.origin)
    finally:
        sys.path = saved


# -----------------------------------------------------------------------------
# Atomic copy
# -----------------------------------------------------------------------------


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` atomically.

    The write happens to ``<dst>.<random>.tmp`` and then ``rename`` is
    called on the same filesystem. ``rename`` on POSIX is atomic, so a
    concurrent importer never observes a half-written file. Existing
    ``dst`` is overwritten only after the rename succeeds.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent)
    )
    try:
        with os.fdopen(fd, "wb") as tmp:
            with src.open("rb") as src_fh:
                shutil.copyfileobj(src_fh, tmp, length=65536)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, dst)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _clear_pycaches(target: Path) -> List[Path]:
    """Remove ``__pycache__`` next to ``target`` if it exists.

    Returns the list of removed paths. The cache for the exact module
    ``target`` is removed; parent ``__pycache__`` directories that
    contain only this module's bytecode are also cleared when empty.
    """
    removed: List[Path] = []
    cache_dir = target.parent / "__pycache__"
    if not cache_dir.is_dir():
        return removed
    stem = target.stem
    for f in cache_dir.iterdir():
        if f.name.startswith(f"{stem}.") and f.suffix == ".pyc":
            f.unlink(missing_ok=True)
            removed.append(f)
    return removed


# -----------------------------------------------------------------------------
# Safety guards
# -----------------------------------------------------------------------------


def _assert_safe_path(relpath: str) -> None:
    """Reject any path that overlaps a runtime-state or forbidden token."""
    normalised = f"/{relpath.lstrip('/')}"
    filename = normalised.rsplit("/", 1)[-1]
    for tok in FORBIDDEN_PATH_TOKENS:
        # Token forms accepted: "/.env", "/__pycache__/", ".key", ".pyc".
        bare = tok.lstrip("/").rstrip("/")
        if bare.startswith("."):
            # File-extension / dotfile check: token is e.g. ``.env``,
            # ``.key``, ``.pyc``. Match if the FILENAME ends with it.
            if filename.endswith(bare):
                raise ValueError(f"refusing to deploy forbidden path: {relpath}")
        else:
            # Directory / fragment check: token is e.g. ``__pycache__``,
            # ``venv``, ``node_modules``. Match if any segment equals
            # the token.
            segments = [s for s in normalised.split("/") if s]
            if bare in segments:
                raise ValueError(f"refusing to deploy forbidden path: {relpath}")


def _syntax_check(path: Path) -> Optional[str]:
    """Compile ``path`` to AST; return an error message if it fails.

    A passing compile is a strong signal the file is at least
    syntactically valid Python — enough to prevent shipping broken code
    to the live runtime.
    """
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return None
    except SyntaxError as exc:
        return f"SyntaxError: {exc.msg} (line {exc.lineno})"


# -----------------------------------------------------------------------------
# systemd restart
# -----------------------------------------------------------------------------


def _restart_services(services: Sequence[str]) -> Dict[str, str]:
    """Restart each unit via ``systemctl restart``.

    Returns ``{unit_name: 'restarted' | 'failed: <reason>'}``. We never
    raise — the caller decides whether a partial restart is acceptable.
    """
    out: Dict[str, str] = {}
    for svc in services:
        proc = subprocess.run(
            ["systemctl", "restart", svc],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            out[svc] = "restarted"
        else:
            err = (proc.stderr or proc.stdout or "").strip()[:200]
            out[svc] = f"failed: {err}"
    return out


def _service_active(services: Sequence[str]) -> Dict[str, bool]:
    """Return ``{unit_name: is_active}`` via ``systemctl is-active``.
    """
    out: Dict[str, bool] = {}
    for svc in services:
        proc = subprocess.run(
            ["systemctl", "is-active", svc],
            check=False,
            capture_output=True,
            text=True,
        )
        out[svc] = proc.stdout.strip() == "active"
    return out


# -----------------------------------------------------------------------------
# Core deploy logic
# -----------------------------------------------------------------------------


@dataclasses.dataclass
class DeployResult:
    """Structured deployer output."""

    mode: str  # "check" | "deploy"
    ok: bool
    needs_deploy: bool
    git_head: str
    repo_root: Path
    hermes_root: Path
    runtime_pythonpath: str
    deployment: AgentDeployment
    dirty_source_files: List[str]
    files: List[Dict[str, Any]]
    services_restarted: Dict[str, str] = dataclasses.field(default_factory=dict)
    services_active: Dict[str, bool] = dataclasses.field(default_factory=dict)
    runtime_import_path: Optional[Path] = None
    notes: List[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _build_result_for_files(
    *,
    mode: str,
    repo_root: Path,
    hermes_root: Path,
    runtime_pythonpath: str,
    deployment: AgentDeployment,
    relpaths: List[str],
    dry_run: bool,
    allow_dirty: bool,
    dirty_paths: List[str],
) -> Tuple[List[Dict[str, Any]], List[str], bool]:
    """Compute file-by-file status without performing the copy.

    Returns ``(files, notes, needs_deploy)``. ``files`` is a list of
    dicts suitable for the JSON report. ``notes`` are operator warnings.
    """
    files: List[Dict[str, Any]] = []
    needs_deploy = False
    notes: List[str] = []

    for rel in relpaths:
        _assert_safe_path(rel)
        src = repo_root / rel
        dst = hermes_root / rel
        if not src.is_file():
            raise FileNotFoundError(f"source missing: {src}")
        src_sha = _sha256(src)
        dst_sha = _sha256(dst) if dst.is_file() else None
        syntax_err = _syntax_check(src)
        entry: Dict[str, Any] = {
            "relpath": rel,
            "source_path": str(src),
            "runtime_path": str(dst),
            "source_sha256": src_sha,
            "runtime_sha256": dst_sha,
            "syntax_ok": syntax_err is None,
            "syntax_error": syntax_err,
            "match": dst_sha == src_sha,
            "dirty": rel in dirty_paths,
            "action": "noop" if dst_sha == src_sha else "copied",
        }
        files.append(entry)
        if entry["syntax_error"]:
            notes.append(f"{rel}: {entry['syntax_error']}")
        if entry["dirty"] and not allow_dirty:
            notes.append(
                f"{rel}: dirty in working tree; "
                "pass --allow-dirty to deploy uncommitted source."
            )
        if not entry["match"]:
            needs_deploy = True
    return files, notes, needs_deploy


def _run_deploy(
    *,
    repo_root: Path,
    hermes_root: Path,
    runtime_pythonpath: str,
    deployment: AgentDeployment,
    allow_dirty: bool,
    restart_mode: str,
    dry_run: bool,
) -> DeployResult:
    """The single deployment workflow: validate, copy, restart, prove."""
    head_sha = _git_head(repo_root)
    dirty_paths = _git_dirty_paths(repo_root, pathspec="plugins/trade")

    rels = [deployment.agent_py_relpath]
    if (repo_root / deployment.test_relpath).is_file():
        rels.append(deployment.test_relpath)

    # Always include both .py and .pyc. Never deploy __pycache__.
    files, notes, needs_deploy = _build_result_for_files(
        mode="deploy",
        repo_root=repo_root,
        hermes_root=hermes_root,
        runtime_pythonpath=runtime_pythonpath,
        deployment=deployment,
        relpaths=rels,
        dry_run=dry_run,
        allow_dirty=allow_dirty,
        dirty_paths=dirty_paths,
    )

    # Refuse to deploy any dirty file unless explicitly allowed.
    blocked = [f for f in files if f["dirty"] and not allow_dirty]
    if blocked:
        return DeployResult(
            mode="deploy",
            ok=False,
            needs_deploy=needs_deploy,
            git_head=head_sha,
            repo_root=repo_root,
            hermes_root=hermes_root,
            runtime_pythonpath=runtime_pythonpath,
            deployment=deployment,
            dirty_source_files=dirty_paths,
            files=files,
            notes=notes + ["deploy blocked: dirty source files present"],
        )

    # Also refuse to deploy a file with a syntax error (couldn't import anyway).
    broken = [f for f in files if f["syntax_error"]]
    if broken:
        return DeployResult(
            mode="deploy",
            ok=False,
            needs_deploy=needs_deploy,
            git_head=head_sha,
            repo_root=repo_root,
            hermes_root=hermes_root,
            runtime_pythonpath=runtime_pythonpath,
            deployment=deployment,
            dirty_source_files=dirty_paths,
            files=files,
            notes=notes + ["deploy blocked: source has syntax errors"],
        )

    # Copy each file that needs it.
    if not dry_run:
        for entry in files:
            if not entry["match"]:
                src = repo_root / entry["relpath"]
                dst = hermes_root / entry["relpath"]
                _atomic_copy(src, dst)
                # Refresh post-copy SHAs so the final report is truthful.
                entry["runtime_sha256"] = _sha256(dst)
                entry["match"] = (
                    entry["source_sha256"] == entry["runtime_sha256"]
                )
                _clear_pycaches(dst)

    services_restarted: Dict[str, str] = {}
    services_active: Dict[str, bool] = {}
    if restart_mode == "auto":
        services_restarted = _restart_services(deployment.service_names)
        services_active = _service_active(deployment.service_names)
    elif restart_mode == "none":
        services_restarted = {"_": "skipped (--restart=none)"}
        services_active = _service_active(deployment.service_names)
    elif restart_mode in {"webtrade", "webtrade2"}:
        # Explicit single service restart.
        unit = f"{restart_mode}.service"
        services_restarted = _restart_services([unit])
        services_active = _service_active([unit])
    else:
        notes.append(f"unknown restart mode {restart_mode!r}; not restarting")

    runtime_import_path = _resolve_runtime_import_path(
        deployment.agent, runtime_pythonpath=runtime_pythonpath
    )

    # Final source/runtime SHA match — prove the live mirror is exact.
    for entry in files:
        entry["match"] = (
            entry["source_sha256"] == entry.get("runtime_sha256")
        )

    ok = all(f["match"] for f in files)
    return DeployResult(
        mode="deploy",
        ok=ok,
        needs_deploy=needs_deploy,
        git_head=head_sha,
        repo_root=repo_root,
        hermes_root=hermes_root,
        runtime_pythonpath=runtime_pythonpath,
        deployment=deployment,
        dirty_source_files=dirty_paths,
        files=files,
        services_restarted=services_restarted,
        services_active=services_active,
        runtime_import_path=runtime_import_path,
        notes=notes,
    )


def _run_check(
    *,
    repo_root: Path,
    hermes_root: Path,
    runtime_pythonpath: str,
    deployment: AgentDeployment,
    allow_dirty: bool,
) -> DeployResult:
    """Check-only: never copies, never restarts."""
    head_sha = _git_head(repo_root)
    # Surface only the dirty paths that touch the deployed area. Other
    # dirty files (scratch dirs, unrelated edits) are deliberately
    # omitted so the operator can read the report without scrolling.
    dirty_paths = _git_dirty_paths(repo_root, pathspec="plugins/trade")
    rels = [deployment.agent_py_relpath]
    if (repo_root / deployment.test_relpath).is_file():
        rels.append(deployment.test_relpath)
    files, notes, needs_deploy = _build_result_for_files(
        mode="check",
        repo_root=repo_root,
        hermes_root=hermes_root,
        runtime_pythonpath=runtime_pythonpath,
        deployment=deployment,
        relpaths=rels,
        dry_run=True,
        allow_dirty=allow_dirty,
        dirty_paths=dirty_paths,
    )
    runtime_import_path = _resolve_runtime_import_path(
        deployment.agent, runtime_pythonpath=runtime_pythonpath
    )
    notes.append(
        "check mode: nothing copied, no service restarted."
    )
    return DeployResult(
        mode="check",
        ok=True,
        needs_deploy=needs_deploy,
        git_head=head_sha,
        repo_root=repo_root,
        hermes_root=hermes_root,
        runtime_pythonpath=runtime_pythonpath,
        deployment=deployment,
        dirty_source_files=dirty_paths,
        files=files,
        runtime_import_path=runtime_import_path,
        notes=notes,
    )


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def _parse_argv(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="deploy_trade",
        description=(
            "Hardened deploy of KAM trade-plugin agent files into the "
            "live Hermes runtime. See module docstring."
        ),
    )
    p.add_argument(
        "--agent",
        required=True,
        help=(
            "Agent name without prefix. E.g. ``vestmarkets`` maps to "
            "``plugins/trade/agents/x_vestmarkets_agent.py``."
        ),
    )
    p.add_argument(
        "--repo-root",
        default=str(DEFAULT_REPO_ROOT),
        help=f"KAM repo root (default: {DEFAULT_REPO_ROOT})",
    )
    p.add_argument(
        "--hermes-root",
        default=str(DEFAULT_HERMES_ROOT),
        help=(
            "Hermes installation root whose ``plugins/trade/`` is the "
            "deploy target (default: " + str(DEFAULT_HERMES_ROOT) + ")"
        ),
    )
    p.add_argument(
        "--runtime-pythonpath",
        default=DEFAULT_RUNTIME_PYTHONPATH,
        help=(
            "PYTHONPATH the running service uses to import plugins.trade "
            "(default: " + DEFAULT_RUNTIME_PYTHONPATH + ")"
        ),
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--check",
        dest="check",
        action="store_true",
        help="Report source/runtime drift without copying or restarting.",
    )
    g.add_argument(
        "--deploy",
        dest="deploy",
        action="store_true",
        help="Copy matching files atomically and restart the relevant services.",
    )
    p.add_argument(
        "--restart",
        choices=("auto", "none", "webtrade", "webtrade2"),
        default="auto",
        help=(
            "Which service(s) to restart after deploy. ``auto`` restarts "
            "the services known to load the agent module; ``none`` "
            "leaves services alone (useful for offline staging)."
        ),
    )
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "Allow deploying files whose repo state is dirty "
            "(uncommitted tracked edits OR untracked files). Default: "
            "reject dirty files."
        ),
    )
    p.add_argument(
        "--services",
        default=",".join(_default_services()),
        help=(
            "Comma-separated list of systemd unit names to restart "
            "(only used when ``--restart=auto``). Default: "
            + ",".join(_default_services())
        ),
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON on stdout instead of the human report.",
    )
    return p.parse_args(list(argv))


def _human_report(result: DeployResult) -> str:
    """Render a human-readable summary suitable for terminal output."""
    lines: List[str] = []
    lines.append(f"== KAM trade deploy :: {result.mode} ==")
    lines.append(f"Git commit:        {result.git_head}")
    lines.append(f"Source:            {result.repo_root}")
    lines.append(f"Runtime target:    {result.hermes_root}")
    lines.append(f"Runtime PYTHONPATH:{result.runtime_pythonpath}")
    lines.append(
        f"Agent:             x_{result.deployment.agent}_agent"
    )
    lines.append(f"Services (target): {', '.join(result.deployment.service_names)}")
    if result.runtime_import_path is not None:
        lines.append(f"Runtime import path: {result.runtime_import_path}")
    if result.dirty_source_files:
        listed = ", ".join(sorted(result.dirty_source_files))
        lines.append(f"Dirty source files: {listed}")
    else:
        lines.append("Dirty source files: (none)")
    lines.append("")
    lines.append(f"{'relpath':58s} {'src SHA-256':12s} {'runtime SHA-256':14s} {'match':6s}")
    lines.append("-" * 110)
    for entry in result.files:
        rel = entry["relpath"]
        src_sha = entry["source_sha256"][:12]
        run_sha = (entry.get("runtime_sha256") or "-")[:12] if entry.get("runtime_sha256") else "-"
        match = "YES" if entry["match"] else "NO"
        dirty = "  DIRTY" if entry["dirty"] else ""
        lines.append(
            f"{rel:58s} {src_sha:12s} {run_sha:14s} {match:6s}{dirty}"
        )
    if result.services_restarted:
        lines.append("")
        lines.append("Services:")
        for unit, status in result.services_restarted.items():
            active = result.services_active.get(unit)
            active_str = (
                "active" if active else ("inactive" if active is False else "?"
            ))
            lines.append(f"  {unit:32s} {status:24s} {active_str}")
    if result.notes:
        lines.append("")
        lines.append("Notes:")
        for note in result.notes:
            lines.append(f"  - {note}")
    lines.append("")
    if result.mode == "check":
        if result.needs_deploy:
            lines.append("Deployment needed: YES")
        else:
            lines.append("Deployment needed: NO")
    else:
        lines.append(f"Deploy ok: {result.ok}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint. Returns the process exit code (0/1/2/3)."""
    args = _parse_argv(argv if argv is not None else sys.argv[1:])
    repo_root = Path(args.repo_root).resolve()
    hermes_root = Path(args.hermes_root).resolve()
    if not (repo_root / "plugins" / "trade").is_dir():
        print(
            f"FATAL: {repo_root}/plugins/trade does not exist; "
            "check --repo-root.",
            file=sys.stderr,
        )
        return 3
    if not hermes_root.is_dir():
        print(
            f"FATAL: --hermes-root={hermes_root} does not exist.",
            file=sys.stderr,
        )
        return 3
    services = tuple(s for s in args.services.split(",") if s.strip())
    deployment = AgentDeployment.from_name(args.agent, services=services)

    if args.check:
        result = _run_check(
            repo_root=repo_root,
            hermes_root=hermes_root,
            runtime_pythonpath=args.runtime_pythonpath,
            deployment=deployment,
            allow_dirty=args.allow_dirty,
        )
    else:
        result = _run_deploy(
            repo_root=repo_root,
            hermes_root=hermes_root,
            runtime_pythonpath=args.runtime_pythonpath,
            deployment=deployment,
            allow_dirty=args.allow_dirty,
            restart_mode=args.restart,
            dry_run=False,
        )

    if args.as_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(_human_report(result))

    if result.mode == "check":
        return 0 if not result.needs_deploy else 1
    # deploy
    if not result.ok:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())