# pyproject: pydantic-ai>=0.0.x, pydantic>=2.7
import asyncio
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent, RunContext

# ---------- Domain models (typed contract) ----------
class LineItem(BaseModel):
    sku: str = Field(..., min_length=1)
    qty: int = Field(..., ge=1)
    unit_price: float = Field(..., ge=0)

class PurchaseOrder(BaseModel):
    po_number: str = Field(..., min_length=3)
    customer_id: str = Field(..., min_length=3)
    items: list[LineItem]
    total: float = Field(..., ge=0)

    @field_validator("total")
    @classmethod
    def total_matches_items(cls, v, info):
        # Enforce arithmetic consistency
        data = info.data
        if "items" in data and data["items"]:
            calc = sum(i.qty * i.unit_price for i in data["items"])
            # allow tiny float tolerance
            if abs(v - calc) > 1e-6:
                raise ValueError(f"total {v} != sum(items) {calc}")
        return v

# ---------- Dependencies (DI) ----------
@dataclass
class Deps:
    customers: dict[str, dict]

# ---------- Agent ----------
agent = Agent[Deps, PurchaseOrder](
    model="openai:gpt-4o-mini",  # or your OpenAI-compatible vLLM client
    deps_type=Deps,
    system_prompt=(
        "You create structured purchase orders. "
        "Use tools for customer lookups. "
        "Return ONLY a valid PurchaseOrder matching the schema."
    ),
    output_type=PurchaseOrder,  # <— structured output contract
)

# ---------- Typed tool using DI ----------
@agent.tool
def lookup_customer(ctx: RunContext[Deps], customer_id: str) -> Optional[dict]:
    """
    Return the customer's profile dict if known, else None.
    """
    return ctx.deps.customers.get(customer_id)

# ---------- Run it ----------
async def main():
    deps = Deps(
        customers={
            "CUST-1001": {"name": "Acme Health", "segment": "Healthcare"},
            "CUST-2002": {"name": "OrbitRX", "segment": "Pharmacy"},
        }
    )

    user_request = """
    Create a PO for customer CUST-1001 with two items:
    - SKU 'GLUCOMETER' qty 2 at $39.5 each
    - SKU 'TEST-STRIPS' qty 5 at $12.0 each
    Make sure total matches the sum of items.
    """

    result = await agent.run(user_request, deps=deps)
    po: PurchaseOrder = result.output

    print("Validated PO:", po.model_dump())
    # You can also inspect tool calls / usage:
    # print(result.usage)
    # print(result.messages)

if __name__ == "__main__":
    asyncio.run(main())