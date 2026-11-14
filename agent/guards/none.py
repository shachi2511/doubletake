"""Guard "none": the unguarded baseline. Every call executes."""

from agent.guards.base import Decision, Execute, Guard, GuardContext, ToolCall


class NoneGuard(Guard):
    name = "none"

    async def before_tool_call(self, ctx: GuardContext, call: ToolCall) -> Decision:
        return Execute()
