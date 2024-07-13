from langchain.agents import AgentExecutor, create_react_agent
from langchain.agents import AgentExecutor, Tool
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain.llms.base import LLM
import requests
from typing import Dict, Any, List, Optional

def GetDispatchAgent():
    refine_tool = Tool(
    name="Refine Prompt Agent",
    func=refine_agent.run,
    description="An agent that refines the initial prompt"
)

# Initialize your custom LLM
api_url = "https://your-internal-api.com/generate"
api_key = "your_api_key"
custom_llm = CustomLLM(api_url, api_key)

# Define the prompt template for refining
refine_prompt = PromptTemplate(input_variables=["prompt"], template="Refine this prompt: {prompt}")

# Define the chain for refining the prompt
refine_chain = LLMChain(llm=custom_llm, prompt=refine_prompt)

# Define the agent executor for the refining agent
refine_agent = AgentExecutor(
    tools=[Tool(
        name="Refine Prompt",
        func=refine_chain.run,
        description="Refines the initial prompt"
    )]
)
# Define the prompt templates for generating a story and summarizing it
story_prompt = PromptTemplate(input_variables=["prompt"], template="Generate a story based on this prompt: {prompt}")
summary_prompt = PromptTemplate(input_variables=["story"], template="Summarize this story: {story}")

# Define the chains for generating a story and summarizing it
story_chain = LLMChain(llm=custom_llm, prompt=story_prompt)
summary_chain = LLMChain(llm=custom_llm, prompt=summary_prompt)

# Define tools for generating a story and summarizing it
story_tool = Tool(
    name="Generate Story",
    func=story_chain.run,
    description="Generates a story based on the refined prompt"
)

summary_tool = Tool(
    name="Summarize Story",
    func=summary_chain.run,
    description="Summarizes the generated story"
)

# Define the second agent executor with the tools, including the first agent as a tool
second_agent = AgentExecutor(
    tools=[refine_tool, story_tool, summary_tool]
)

# Example usage with the second agent
initial_prompt = "A knight's quest"
final_output = second_agent.run(initial_prompt)
print(final_output)
