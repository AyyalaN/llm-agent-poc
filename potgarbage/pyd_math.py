from pydantic_ai import Agent, RunContext

agent = Agent(
    model="openai:gpt-4",
    system_prompt="You are a helpful calculator. Use tools when computing.",
)

@agent.tool
def add(ctx: RunContext[None], a: float, b: float) -> float:
    return a + b

@agent.tool
def subtract(ctx: RunContext[None], a: float, b: float) -> float:
    return a - b

async def main():
    res = await agent.run("Add 10 and 4, then subtract 3 from the result.")
    print("Agent answer:", res.data)

asyncio.run(main())

# import asyncio
# from pydantic_ai import Agent, RunContext
# from pydantic import BaseModel, Field

# # Optional: structured output model
# class MathResult(BaseModel):
#     operation: str = Field(..., description="The operation performed, e.g., 'add' or 'subtract'")
#     a: float = Field(..., description="First operand")
#     b: float = Field(..., description="Second operand")
#     result: float = Field(..., description="The arithmetic result")

# agent = Agent(
#     model="openai:gpt-4",      # or your custom model provider
#     system_prompt="You are a calculator bot. Use the provided tools to compute results precisely.",
#     output_type=MathResult,    # structured output – optional, but enforces format
# )

# @agent.tool
# def add(ctx: RunContext[None], a: float, b: float) -> float:
#     """Add two numbers: return a + b."""
#     return a + b

# @agent.tool
# def subtract(ctx: RunContext[None], a: float, b: float) -> float:
#     """Subtract b from a: return a - b."""
#     return a - b

# async def main():
#     prompt = "What is 7 minus 2? Then also what is the sum of 5 and 3?"
#     result = await agent.run(prompt)
#     print("Structured output:", result.output)
#     print("Raw AI response:", result.data)

# if __name__ == "__main__":
#     asyncio.run(main())