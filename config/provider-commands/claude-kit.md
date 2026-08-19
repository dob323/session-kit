---
description: Leave this conversation running and go back to the session picker
disable-model-invocation: true
allowed-tools: Bash(shpool detach:*)
---

!`shpool detach`

The session you were in keeps running exactly where it was. Nothing was
stopped, summarized, or closed: the terminal simply went back to the session
picker, and typing this session's number there re-attaches to this same
conversation. Ctrl-Q is the key that does this, in any session and any
provider.

Use /exit when you are finished with it instead. That closes the session and
frees its number, and Ctrl-D is its key.
