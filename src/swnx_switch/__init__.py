"""swnx-switch — read and convert SolidWorks (.sldprt) <-> Siemens NX (.prt).

The package has four layers:

* :mod:`swnx_switch.ir`      neutral, AI-readable CAD document model (JSON)
* :mod:`swnx_switch.backends` one driver per CAD app (SolidWorks COM, NX batch)
* :mod:`swnx_switch.pipeline` format -> STEP/Parasolid -> format conversion
* :mod:`swnx_switch.ai`      agent-facing tool layer (MCP stdio server + briefs)
"""

__version__ = "0.2.0"
SCHEMA = "swnx.ir/1"

__all__ = ["__version__", "SCHEMA"]
