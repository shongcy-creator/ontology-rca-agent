#!/usr/bin/env python3
"""Implementation for the bird.agent.prompt_manager module."""


class PromptManager:
    """Implementation of PromptManager."""

    def __init__(self, max_turns: int = 15, semantic_manifest: str = ""):
        self.max_turns = max_turns
        self.semantic_manifest = semantic_manifest

    def build_system_prompt(self, question: str, db_path: str) -> str:
        """Build system prompt."""
        prompt = f"""You are an expert SQL generation agent for text-to-SQL tasks.
Explore the database and produce a correct SQLite query for a given
natural-language question.

## Workflow

Follow the ReAct pattern:

  Thought: [analyze what you know and what you still need to discover]
  Action: [call ONE tool]
  Observation: [tool result from the environment]
  ... repeat as needed ...
  FINISH: SELECT ...

## Rules

- ONE tool call per turn. Choose the most informative action.
- Maximum {self.max_turns} turns (plan accordingly).
- Prefer targeted exploration over guessing. Use tool results to refine
  your understanding.
- Use explicit JOIN ... ON syntax, not NATURAL JOIN or USING.
- Always qualify column names with table aliases in multi-table queries.
- Include necessary GROUP BY when using aggregation functions.
- The SQL must be valid SQLite syntax.
- If a query returns an error, analyze the error and fix your approach.
- If you cannot determine the correct SQL, output your best attempt
  rather than giving up.
"""


        if self.semantic_manifest:
            prompt += f"""
<semantic_manifest>
{self.semantic_manifest}
</semantic_manifest>
"""

        prompt += f"""
## Output Format

When you are ready to submit your final SQL query, output it as:

FINISH: SELECT ...

The SQL must be on the same line or immediately after the FINISH marker.
Do NOT use markdown formatting (bold, code blocks, etc.) around the FINISH line — just
output it as plain text.

## Question

{question}

## Database

{db_path}
"""
        return prompt

    def get_guidance_no_tool_call(self) -> str:
        """Return guidance no tool call."""
        return (
            "Please either call a tool (using the function calling interface) "
            "or, if you are ready to submit your final SQL, output "
            "FINISH: SELECT ..."
        )

    def get_guidance_no_response(self) -> str:
        """Return guidance no response."""
        return (
            "You did not provide any response. Please analyze what information "
            "you still need, call a tool to get it, or output your final SQL "
            "with the FINISH marker."
        )
