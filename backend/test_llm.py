import asyncio

from app.services.llm import get_llm_gateway


async def test_llm():
    try:
        gateway = get_llm_gateway()
        resp = await gateway.complete("Hi, are you there?", system_instruction="Reply yes or no")
        print("LLM Response:", resp)
    except Exception as e:
        print("LLM Error:", str(e))


asyncio.run(test_llm())
