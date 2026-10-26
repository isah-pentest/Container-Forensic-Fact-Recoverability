"""Retention headroom H and predicted loss under size-based rotation (Section 4).

Closed-form arithmetic only.
"""
from __future__ import annotations


def remaining_budget(m_bytes: float, k_backups: int, offset_bytes: float) -> float:
    """B = (M - o) + k*M: bytes that can still be written before the record's file is deleted."""
    if not 0 <= offset_bytes <= m_bytes:
        raise ValueError("offset must lie in [0, M]")
    return (m_bytes - offset_bytes) + k_backups * m_bytes


def headroom(m_bytes: float, k_backups: int, offset_bytes: float, written_bytes: float) -> float:
    """H = B / W. Returns inf when nothing was written after the record."""
    b = remaining_budget(m_bytes, k_backups, offset_bytes)
    return float("inf") if written_bytes <= 0 else b / written_bytes


def predicted_loss_probability(rate_bytes_per_s: float, elapsed_s: float, m_bytes: float,
                               k_backups: int) -> float:
    """P(loss) = clip((r*dt - k*M)/M, 0, 1), assuming the offset is uniform on [0, M)."""
    w = rate_bytes_per_s * elapsed_s
    return min(1.0, max(0.0, (w - k_backups * m_bytes) / m_bytes))


def rate_for_loss_probability(target_p: float, elapsed_s: float, m_bytes: float,
                              k_backups: int) -> float:
    """Inverse of the above: write rate giving a target loss probability (0 < p < 1)."""
    if not 0 < target_p < 1:
        raise ValueError("target_p must be in (0, 1)")
    return (k_backups + target_p) * m_bytes / elapsed_s


def rate_for_headroom(target_h: float, elapsed_s: float, m_bytes: float, k_backups: int,
                      worst_case: bool = True) -> float:
    """Write rate at which headroom equals target_h.

    worst_case=True uses the least favourable offset (record at the end of the active file,
    B = k*M), as required by the low-load rule in Section 4.3; this needs k >= 1.
    worst_case=False uses offset 0 (B = (k+1)*M).
    """
    if target_h <= 0:
        raise ValueError("target_h must be > 0")
    budget = k_backups * m_bytes if worst_case else (k_backups + 1) * m_bytes
    if budget <= 0:
        raise ValueError("worst-case headroom is zero when k = 0; use k >= 1")
    return budget / (target_h * elapsed_s)
