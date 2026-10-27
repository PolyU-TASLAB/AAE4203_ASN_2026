"""Fair-play flags.

Slide 32 says the teaching team may re-run any group's code, and the CSV must
have been computed from the provided observations.  These checks never change a
score - they only put a marker next to the row so a human can look.
"""

from __future__ import annotations

from .evaluator import EvalResult


def inspect(cfg, db, group: str, result: EvalResult,
            sha256: str | None) -> tuple[bool, str]:
    """Return ``(suspicious, reason)``."""
    if not cfg.fairplay.enabled:
        return False, ""

    reasons: list[str] = []

    if result.rmse2d == result.rmse2d and result.rmse2d < cfg.fairplay.implausible_rmse2d_m:
        reasons.append(
            f"2D RMSE of {result.rmse2d:.2f} m is better than pseudorange SPP can "
            "physically deliver - check that the position was not taken from a "
            "carrier-phase or DGNSS solution")

    if result.max3d == result.max3d and result.max3d < cfg.fairplay.gt_leak_max_3d_m:
        reasons.append(
            f"every epoch is within {result.max3d:.2f} m of the reference - this "
            "looks like the true trajectory rather than computed positions")

    if cfg.fairplay.flag_identical_copies and sha256:
        same = db.groups_sharing_sha(sha256, group)
        if same:
            reasons.append(
                "the uploaded file is byte-identical to a file already submitted "
                f"by {', '.join(sorted(same))}")

    return bool(reasons), "; ".join(reasons)
