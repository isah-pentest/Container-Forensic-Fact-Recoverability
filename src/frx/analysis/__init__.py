"""G7 analysis layer (Blueprint v3.0.1, Sections 11, 12, 15.5, 17, 18 G7).

Added after execution, as Section 16 anticipates for table and figure generators. It only reads
locked evidence and reuses the frozen statistics (frx.stats), metrics (frx.metrics), headroom
(frx.headroom), matcher (frx.reconstruct) and scoring (frx.reconstruct.score); it never edits a
frozen module, never rewrites a locked score, and writes only under
<evidence>/analysis_outputs/<analysis commit>/. Entry point: `python -m frx.analysis <command>`.
"""
