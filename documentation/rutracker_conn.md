Prowlarr is the easiest and most modern microservice to set up for this task. It runs silently in the background, handles RuTracker's strict Cloudflare checks, cookies, and login flow, and gives your custom Python tool a clean, static JSON API to read from.📦 Step 1: Install ProwlarrChoose the method that matches your environment:Option A: Using Docker (Recommended)If you have Docker installed, you can spin up Prowlarr with a single command:bashdocker run -d \
  --name=prowlarr \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=Etc/UTC \
  -p 9696:9696 \
  -v /config:/config \
  --restart unless-stopped \
  lscr.io/linuxserver/prowlarr:latest
Use code with caution.Option B: Native Desktop AppGo to the Official Prowlarr Downloads Page.Download and run the installer for Windows, macOS, or Linux.Once installed, it will automatically launch a web service running on your local machine.🌐 Step 2: Configure RuTracker inside ProwlarrOpen your web browser and navigate to http://localhost:9696.Proceed through the initial setup wizard (you can skip setting an administrative password if this is just a local script runner).Click on Indexers in the left-hand sidebar menu, then click Add New (the + icon).Type RuTracker in the search bar and select it.In the configuration popup window, fill out your credentials:Username: Your RuTracker usernamePassword: Your RuTracker passwordClick Test. If the test passes, click Save.(Note: If the connection fails, it means RuTracker is blocking your home IP address. You can scroll down to the "Proxy" section within the same window and paste a standard SOCKS5 or HTTP residential proxy).🔑 Step 3: Get Your API KeyInside the Prowlarr dashboard, navigate to Settings ➡️ General.Under the Security section, copy the string inside the API Key field.Paste this key directly into your Python script as the value for API_KEY.🚀 Step 4: Run Your Claude Agent ToolWith Prowlarr running in the background, your Python script can now hit your local microservice. Prowlarr handles all the messy session validation behind the scenes.Here is the clean, production-ready script you can run immediately to verify your installation:pythonimport json
import requests
import anthropic

# 1. DEFINE CLAUDE'S TOOL CONFIGURATION
RUTRACKER_TOOL_SCHEMA = {
    "name": "search_rutracker",
    "description": "Queries RuTracker for software, media, books, or datasets. Returns magnet links and seed stats.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The exact keywords to look up."}
        },
        "required": ["query"]
    }
}

# 2. PROWLARR INTERFACE CONNECTOR
def execute_rutracker_search(query: str) -> str:
    # Setup your local environment credentials here:
    PROWLARR_URL = "http://localhost:9696/api/v1/v1.0/api"
    API_KEY = "PASTE_YOUR_COPIED_PROWLARR_API_KEY_HERE"
    
    params = {
        "apikey": API_KEY,
        "Query": query
    }
    
    try:
        # Request search results from your local Prowlarr agent microservice
        response = requests.get(PROWLARR_URL, params=params, timeout=15)
        response.raise_for_status()
        raw_results = response.json()
        
        if not raw_results:
            return json.dumps({"status": "success", "message": "No results found."})
        
        # Format a condensed dictionary list to stay well within Claude's token limit
        cleaned = []
        for item in raw_results[:5]:
            cleaned.append({
                "title": item.get("title"),
                "size_mb": round(item.get("size", 0) / (1024 * 1024), 2) if item.get("size") else "Unknown",
                "seeders": item.get("seeders", 0),
                "magnet": item.get("magnetUrl") or item.get("link")
            })
        return json.dumps({"status": "success", "data": cleaned}, indent=2)
        
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Microservice interaction failed: {str(e)}"})

# 3. INTERACTIVE AGENT CHAIN EXECUTION 
def run_agent(user_query: str):
    # Initialize the Anthropic client (reads ANTHROPIC_API_KEY from environment)
    client = anthropic.Anthropic()
    model = "claude-3-5-sonnet-20241022"
    
    messages = [{"role": "user", "content": user_query}]
    
    # First response cycle: Claude reads the request and decides if it needs the tool
    response = client.messages.create(model=model, max_tokens=1000, tools=[RUTRACKER_TOOL_SCHEMA], messages=messages)
    messages.append({"role": "assistant", "content": response.content})
    
    if response.stop_reason == "tool_use":
        tool_block = next(b for b in response.content if b.type == "tool_use")
        
        # Trigger the local Prowlarr search pipeline
        search_payload = execute_rutracker_search(query=tool_block.input["query"])
        
        # Second response cycle: Feed the scraped results back into the context window
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_block.id, "content": search_payload}]
        })
        
        final_response = client.messages.create(model=model, max_tokens=1000, tools=[RUTRACKER_TOOL_SCHEMA], messages=messages)
        print("\n🤖 Claude's Answer:\n", final_response.content[0].text)
    else:
        print("\n🤖 Claude's Answer:\n", response.content[0].text)

if __name__ == "__main__":
    run_agent("Find a copy of the open-source Godot Engine installer on RuTracker.")
