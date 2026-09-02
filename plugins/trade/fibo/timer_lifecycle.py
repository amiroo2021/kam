"""Active-registration-driven fibo-converge.timer lifecycle helper.

Owns the narrow mapping:

    effective active_registration_count > 0  → timer enabled+active
    effective active_registration_count == 0 → timer disabled+inactive

ONLY ``fibo-converge.timer`` is controlled. Never touches:
  - fibo-converge.service (oneshot may finish naturally)
  - fibo-mt4-reader.service
  - hermes-gateway.service

Idempotent: already-correct states are NOOP (no restart).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Hard-coded unit name — never taken from user input.
CONVERGE_TIMER_UNIT = "fibo-converge.timer"

# Forbidden units — defensive guard if a caller ever mis-routes.
_FORBIDDEN_UNITS = frozenset(
    {
        "fibo-converge.service",
        "fibo-mt4-reader.service",
        "hermes-gateway.service",
    }
)

SystemctlRunner = Callable[[Sequence[str]], "CompletedProc"]


@dataclass(frozen=True)
class CompletedProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class TimerReconcileResult:
    """Outcome of reconciling the convergence timer to a desired state."""

    desired_active: bool
    already_ok: bool
    ok: bool
    changed: bool
    enabled: Optional[bool]
    active: Optional[bool]
    message: str
    unit: str = CONVERGE_TIMER_UNIT

    @property
    def status_label(self) -> str:
        if self.ok and self.desired_active and self.active:
            return "ACTIVE"
        if self.ok and (not self.desired_active) and (not self.active):
            return "INACTIVE"
        if self.desired_active:
            return "INACTIVE"
        return "STILL ACTIVE"


def count_active_registrations(registrations: Sequence) -> int:
    """Count EFFECTIVE active registrations.

    ``registrations`` must already be the latest-per-registration_key
    set (e.g. ``FiboRegistrationStore.load_all()``). Historical
    stopped lines that lost to a later row are not present. A
    latest row with ``status=\"stopped\"`` is inactive via
    ``is_active``.
    """
    n = 0
    for reg in registrations:
        is_active = getattr(reg, "is_active", None)
        if callable(is_active):
            if is_active:
                n += 1
        elif getattr(reg, "status", "") != "stopped":
            n += 1
    return n


def _mutations_allowed() -> bool:
    """Block real enable/disable/start/stop unless explicitly permitted.

    Production Start/Stop sets nothing special and allows control.
    Offline tests set ``FIBO_TIMER_LIFECYCLE_DRY_RUN=1`` so they can
    never arm/disarm the host ``fibo-converge.timer``.
    """
    flag = (os.environ.get("FIBO_TIMER_LIFECYCLE_DRY_RUN") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return False
    return True


def _dry_run_mutation_result(
    *,
    desired_active: bool,
    enabled: Optional[bool],
    active: Optional[bool],
) -> TimerReconcileResult:
    return TimerReconcileResult(
        desired_active=desired_active,
        already_ok=False,
        ok=False,
        changed=False,
        enabled=enabled,
        active=active,
        message=(
            "FIBO_TIMER_LIFECYCLE_DRY_RUN=1 — refusing to mutate "
            f"{CONVERGE_TIMER_UNIT} on this host"
        ),
    )


def _default_systemctl(args: Sequence[str]) -> CompletedProc:
    argv = [str(a) for a in args]
    # Hard deny mutating commands under dry-run / test isolation.
    if argv and argv[0] in {"enable", "disable", "start", "stop", "restart", "mask", "unmask"}:
        if not _mutations_allowed():
            return CompletedProc(
                returncode=1,
                stdout="",
                stderr="FIBO_TIMER_LIFECYCLE_DRY_RUN blocks systemctl mutations",
            )
        # Only the converge timer unit may be mutated.
        unit = argv[-1] if len(argv) > 1 else ""
        if unit != CONVERGE_TIMER_UNIT:
            return CompletedProc(
                returncode=1,
                stdout="",
                stderr=f"refusing systemctl {argv[0]} for unit {unit!r}",
            )
    if shutil.which("systemctl") is None:
        return CompletedProc(
            returncode=127,
            stdout="",
            stderr="systemctl not on PATH",
        )
    try:
        proc = subprocess.run(
            ["systemctl", *argv],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return CompletedProc(
            returncode=int(proc.returncode),
            stdout=(proc.stdout or ""),
            stderr=(proc.stderr or ""),
        )
    except Exception as exc:  # noqa: BLE001
        return CompletedProc(returncode=1, stdout="", stderr=str(exc))


def _query_bool(runner: SystemctlRunner, subcmd: str, unit: str) -> Optional[bool]:
    """Interpret ``systemctl is-enabled`` / ``is-active`` output."""
    proc = runner([subcmd, unit])
    token = (proc.stdout or "").strip().splitlines()
    head = (token[0] if token else "").strip().lower()
    if subcmd == "is-enabled":
        if head in {"enabled", "enabled-runtime", "static", "indirect", "generated"}:
            return True
        if head in {"disabled", "masked", "masked-runtime", "not-found", ""}:
            return False
        # Some systemd builds still print enabled on stderr with rc=0.
        if proc.returncode == 0 and "enabled" in (proc.stdout + proc.stderr).lower():
            return True
        return False if proc.returncode != 0 else None
    # is-active
    if head in {"active", "activating", "reloading"}:
        return True
    if head in {"inactive", "failed", "dead", "not-found", ""}:
        return False
    return True if proc.returncode == 0 else False


def _ensure_unit_is_timer(unit: str) -> None:
    if unit != CONVERGE_TIMER_UNIT:
        raise ValueError(
            f"timer_lifecycle refuses unit {unit!r}; "
            f"only {CONVERGE_TIMER_UNIT!r} is allowed"
        )
    if unit in _FORBIDDEN_UNITS:
        raise ValueError(f"timer_lifecycle forbids controlling {unit!r}")


def ensure_convergence_timer_active(
    *,
    runner: Optional[SystemctlRunner] = None,
    unit: str = CONVERGE_TIMER_UNIT,
) -> TimerReconcileResult:
    """Enable + start the converge timer if not already armed. Idempotent."""
    _ensure_unit_is_timer(unit)
    run = runner or _default_systemctl
    enabled = _query_bool(run, "is-enabled", unit)
    active = _query_bool(run, "is-active", unit)
    if enabled is True and active is True:
        return TimerReconcileResult(
            desired_active=True,
            already_ok=True,
            ok=True,
            changed=False,
            enabled=True,
            active=True,
            message="fibo-converge.timer already enabled and active",
        )
    # enable --now is idempotent for an already-enabled unit; we only
    # reach here when at least one of enabled/active is false.
    proc = run(["enable", "--now", unit])
    enabled_after = _query_bool(run, "is-enabled", unit)
    active_after = _query_bool(run, "is-active", unit)
    ok = bool(enabled_after) and bool(active_after)
    if not ok:
        detail = (proc.stderr or proc.stdout or "").strip() or f"rc={proc.returncode}"
        logger.warning(
            "timer_lifecycle: failed to activate %s: %s", unit, detail
        )
        return TimerReconcileResult(
            desired_active=True,
            already_ok=False,
            ok=False,
            changed=False,
            enabled=enabled_after,
            active=active_after,
            message=f"failed to enable/start {unit}: {detail}",
        )
    return TimerReconcileResult(
        desired_active=True,
        already_ok=False,
        ok=True,
        changed=True,
        enabled=True,
        active=True,
        message=f"{unit} enabled and started",
    )


def ensure_convergence_timer_inactive(
    *,
    runner: Optional[SystemctlRunner] = None,
    unit: str = CONVERGE_TIMER_UNIT,
) -> TimerReconcileResult:
    """Disable + stop the converge timer if not already down. Idempotent.

    Does NOT stop ``fibo-converge.service`` — an in-flight oneshot
    finishes naturally.
    """
    _ensure_unit_is_timer(unit)
    run = runner or _default_systemctl
    enabled = _query_bool(run, "is-enabled", unit)
    active = _query_bool(run, "is-active", unit)
    if enabled is False and active is False:
        return TimerReconcileResult(
            desired_active=False,
            already_ok=True,
            ok=True,
            changed=False,
            enabled=False,
            active=False,
            message="fibo-converge.timer already disabled and inactive",
        )
    # disable --now stops the timer unit only (not the oneshot service
    # mid-run beyond cancelling further timer triggers).
    proc = run(["disable", "--now", unit])
    enabled_after = _query_bool(run, "is-enabled", unit)
    active_after = _query_bool(run, "is-active", unit)
    ok = (enabled_after is False) and (active_after is False)
    if not ok:
        detail = (proc.stderr or proc.stdout or "").strip() or f"rc={proc.returncode}"
        logger.warning(
            "timer_lifecycle: failed to deactivate %s: %s", unit, detail
        )
        return TimerReconcileResult(
            desired_active=False,
            already_ok=False,
            ok=False,
            changed=False,
            enabled=enabled_after,
            active=active_after,
            message=f"failed to disable/stop {unit}: {detail}",
        )
    return TimerReconcileResult(
        desired_active=False,
        already_ok=False,
        ok=True,
        changed=True,
        enabled=False,
        active=False,
        message=f"{unit} disabled and stopped",
    )


def reconcile_convergence_timer(
    active_registration_count: int,
    *,
    runner: Optional[SystemctlRunner] = None,
    unit: str = CONVERGE_TIMER_UNIT,
) -> TimerReconcileResult:
    """Reconcile timer state from the effective active registration count."""
    count = int(active_registration_count or 0)
    if count > 0:
        return ensure_convergence_timer_active(runner=runner, unit=unit)
    return ensure_convergence_timer_inactive(runner=runner, unit=unit)


def convergence_status_lines(
    *,
    active_registration_count: int,
    timer_result: Optional[TimerReconcileResult] = None,
    runner: Optional[SystemctlRunner] = None,
) -> List[str]:
    """Human-readable Convergence status lines for wizard screens."""
    count = int(active_registration_count or 0)
    if timer_result is not None:
        active = bool(timer_result.active) if timer_result.active is not None else False
        ok = timer_result.ok
    else:
        run = runner or _default_systemctl
        try:
            active = bool(_query_bool(run, "is-active", CONVERGE_TIMER_UNIT))
            ok = True
        except Exception:  # noqa: BLE001
            active = False
            ok = False
    if count <= 0:
        if active:
            return [
                "⚠️ Convergence: STILL ACTIVE — no active registrations",
            ]
        return ["⚙️ Convergence: INACTIVE"]
    if active and ok:
        return ["⚙️ Convergence: ACTIVE"]
    return [
        "⚠️ Convergence: INACTIVE — Fibo will not auto-trade",
    ]


def format_start_timer_warning(
    *,
    registration_key: str,
    timer_result: TimerReconcileResult,
) -> str:
    return (
        "⚠️ Fibo registration saved, but autonomous convergence could not be enabled.\n\n"
        f"Registration: {registration_key}\n"
        "Convergence: INACTIVE\n\n"
        "Fibo will not trade automatically until fibo-converge.timer is enabled.\n"
        f"Detail: {timer_result.message}"
    )


def format_stop_timer_warning(
    *,
    active_remaining: int,
    timer_result: TimerReconcileResult,
) -> str:
    return (
        "⚠️ Fibo registration stopped, but the convergence timer could not be disabled.\n\n"
        f"Active registrations: {active_remaining}\n"
        "Convergence timer: STILL ACTIVE\n\n"
        "No active Fibo registrations remain, so normal convergence should have\n"
        "nothing to trade, but the scheduler requires administrative attention.\n"
        f"Detail: {timer_result.message}"
    )


__all__ = [
    "CONVERGE_TIMER_UNIT",
    "CompletedProc",
    "TimerReconcileResult",
    "count_active_registrations",
    "ensure_convergence_timer_active",
    "ensure_convergence_timer_inactive",
    "reconcile_convergence_timer",
    "convergence_status_lines",
    "format_start_timer_warning",
    "format_stop_timer_warning",
]
