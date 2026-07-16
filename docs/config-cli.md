# Config CLI documentation

This page is retained only to redirect links from the former monorepo
documentation.

- Start with the [project README](../README.md).
- Install the client using [installation.md](installation.md).
- Follow the [command reference](commands.md).
- Automation agents must follow [agent-workflow.md](agent-workflow.md).
- Review [security.md](security.md) and
  [compatibility.md](compatibility.md) before production use.

The standalone client has no direct workspace-save command. Every change must
be created as a revision-bound proposal, reviewed, explicitly approved, applied
with `proposals apply PROPOSAL_ID --confirm`, and visually verified.
