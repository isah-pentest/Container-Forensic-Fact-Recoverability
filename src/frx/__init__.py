"""FRX: forensic fact recoverability after pod termination (Blueprint v3.0).

This package holds the statistics, metrics, randomization, integrity and contract code that
the protocol locks before freeze. Cluster-facing subpackages (harness, acquisition,
orchestrator, reconstruct) expose interfaces that are implemented and qualified at G1-G3.
No machine-learning models are used anywhere in this package.
"""
__version__ = "0.1.0"
PROTOCOL_VERSION = "3.0"
