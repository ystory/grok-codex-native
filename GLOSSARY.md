# Grok Codex native

The language this project uses for delegating coding work from a Grok Bot to Codex on the Bot's own cloud computer.

## Language

**Bot**:
A Grok Bot agent that receives a user's request and decides what to delegate.
_Avoid_: Agent, parent

**Cloud computer**:
The hosted Linux computer that all of an account's Bots share.
_Avoid_: VM, host, sandbox

**Cloud Agent**:
The delegation tool built into Grok Bot, which this project's workflow is modeled on.
_Avoid_: Cursor cloud session, cloud session

**Control**:
The container that holds the ChatGPT login and the native Codex store.
_Avoid_: Server, manager

**Worker**:
A Docker container that provides one task's execution environment.
_Avoid_: Sandbox, runner, agent

**Thread**:
A native Codex session, which owns its history and queued input.
_Avoid_: Session, task

**Turn**:
One native execution within a thread, ending in a terminal state.
_Avoid_: Run, step

**Handoff**:
A finite wait for a turn's terminal state that returns its result and file paths to the Bot.
_Avoid_: Callback, watcher

**Capability matrix**:
The table comparing each Cloud Agent capability with what this project provides.
_Avoid_: Verification record, parity list
