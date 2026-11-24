"""The Windows side of the platform layer (a preview from 1.22): Windows services hosted by the
panel's runner, Task Scheduler, Defender Firewall and Java from Adoptium.

The panel describes servers and schedules in the same unit files as on Linux; this side reads
them (units.py) and makes Windows do what they say (runner.py, services.py). Where the panel
reads a Linux tool's output (`systemctl show`, `ufw status`, `apt-cache policy`), this side
answers in the same words, so the features above it are the same code on both systems.
"""
