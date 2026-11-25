"""Sarthi: an open-source, self-hosted AI agent that finds, matches and applies to jobs for you."""

import warnings

# trafilatura (reading job pages) imports urllib3's SOCKS support, which prints a PySocks warning on every
# start. Sarthi never uses a SOCKS proxy, so the warning only alarms new users.
warnings.filterwarnings("ignore", message="SOCKS support in urllib3", module="urllib3.contrib.socks")

__version__ = "0.1.0"
