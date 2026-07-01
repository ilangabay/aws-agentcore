# my_agent.py
from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands import Agent
from strands.models import BedrockModel

app = BedrockAgentCoreApp()
memory = MemoryClient(region_name="us-east-1")

MEMORY_ID = "agent007remembers_mem-NexdES4zDx"

model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-6", region_name="us-east-1")

@app.entrypoint
def production_agent(request):
    user_message = request.get("prompt", "Tell me a short joke.")
    actor_id   = request.get("actorId", "ilan")
    session_id = request.get("sessionId", "default")

    # --- Retrieve LTM preferences (user-scoped, cross-session) ---
    prefs = memory.retrieve_memories(
        memory_id=MEMORY_ID,
        namespace=f"/users/{actor_id}/preferences/",
        query=user_message,
        top_k=5,
    )
    pref_text = "\n".join(p.get("content", {}).get("text", "") for p in prefs)

    system = "You are a helpful assistant."
    if pref_text:
        system += f"\n\nKnown user preferences (honor these):\n{pref_text}"

    agent = Agent(model=model, system_prompt=system)
    result = agent(user_message)

    # --- Write this turn back so future sessions learn from it ---
    memory.create_event(
        memory_id=MEMORY_ID,
        actor_id=actor_id,
        session_id=session_id,
        messages=[(user_message, "USER"),
                (str(result.message), "ASSISTANT")],
    )

    return {"result": result.message}

if __name__ == "__main__":
    print ("Running agent")
    production_agent.run()