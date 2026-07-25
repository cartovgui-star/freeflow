# Security policy

## Supported versions

Security fixes are applied to the latest version on the `main` branch.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose
microphone audio, transcripts, clipboard contents, or arbitrary code execution.

Use GitHub's **Report a vulnerability** option in the repository Security tab.
Include:

- The affected FreeFlow version or commit
- Windows and Python versions
- Reproduction steps
- Expected and observed impact
- Any suggested mitigation

Do not attach real private transcripts. A maintainer should acknowledge a
report within seven days and coordinate disclosure after a fix is available.

## Local trust model

FreeFlow has global keyboard and clipboard access because those capabilities
are required for system-wide dictation. Only install releases or source code
from a repository you trust.
