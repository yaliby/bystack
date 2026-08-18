"""ByStack -- an infrastructure control plane."""

#: The version of everything published together. It is not decoration: the
#: Controller composes the "add a host" URL out of it (`routes/enrollment.py`),
#: so this string decides which release tag an operator's pasted command
#: fetches `install-agent.sh` and the agent binary from. Bumping it without
#: pushing the matching tag hands out a command that 404s; pushing a tag
#: without bumping it hands out the *previous* release to every new host.
#: `scripts/install-agent.sh` carries the same number as its default and has
#: to move with this one.
__version__ = "0.4.0"
