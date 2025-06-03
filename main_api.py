import uuid
import os
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional, AsyncGenerator
from datetime import datetime
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from langchain.chat_models import init_chat_model
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.graph import add_messages
from langgraph.managed import IsLastStep
from langgraph.prebuilt import create_react_agent
from langchain_core.prompts import ChatPromptTemplate
from typing_extensions import TypedDict, Annotated

from cli.cli import ConversationManager
from mcp_client import MCPClientManager
from mcp_client.mcp_server_util import verify_uvx_installation, verify_npx_installation, verify_python_installation
from dotenv import load_dotenv


# Pydantic models for OpenAI API compatibility
class ChatMessage(BaseModel):
    role: str = Field(..., description="The role of the message author")
    content: str = Field(..., description="The content of the message")
    name: Optional[str] = Field(None, description="The name of the message author")


class ChatCompletionRequest(BaseModel):
    model: str = Field(default="gpt-4", description="Model to use")
    messages: List[ChatMessage] = Field(..., description="List of messages")
    temperature: Optional[float] = Field(0.7, ge=0, le=2)
    max_tokens: Optional[int] = Field(None, gt=0)
    stream: Optional[bool] = Field(False)
    stop: Optional[List[str]] = Field(None)
    presence_penalty: Optional[float] = Field(0, ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(0, ge=-2, le=2)
    top_p: Optional[float] = Field(1, gt=0, le=1)
    n: Optional[int] = Field(1, ge=1, le=128)


class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionResponseChoice]
    usage: Dict[str, int] = Field(default_factory=dict)


class ChatCompletionStreamChoice(BaseModel):
    index: int
    delta: Dict[str, Any]
    finish_reason: Optional[str] = None


class ChatCompletionStreamResponse(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionStreamChoice]


# Agent State
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    is_last_step: IsLastStep
    today_datetime: str
    remaining_steps: int


class OpenAIMCPServer:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.mcp_manager = MCPClientManager(config_path)
        self.llm = None
        self.agent_executor = None
        self.initialized = False
        self.conversation_manager = None
        self.sqlite_db = Path("conversation_db/conversations.db")
        load_dotenv()

    async def initialize(self):
        """Initialize MCP clients and load configuration"""
        if not self.initialized:
            try:
                print("Starting MCP server initialization...")

                # Verify installations
                if not await verify_python_installation():
                    raise RuntimeError("Failed to verify Python installation")
                if not await verify_uvx_installation():
                    raise RuntimeError("Failed to verify UVX installation")
                if not await verify_npx_installation():
                    raise RuntimeError("Failed to verify NPX installation")

                # Initialize MCP manager
                await self.mcp_manager.initialize()

                # Initialize LLM and agent
                self._init_llm()

                # Initialize conversation manager
                self.conversation_manager = ConversationManager(self.sqlite_db)

                self.initialized = True
                print("MCP server initialization complete")
            except Exception as e:
                print(f"Initialization error: {str(e)}")
                raise

    def _init_llm(self, model: str = "deepseek-v3", temperature: float = 0.7):
        """Initialize LLM and agent"""
        try:
            api_key = os.getenv("OPEN_API_KEY")
            api_url = os.getenv("OPEN_API_URL")
            api_model = os.getenv("OPEN_API_MODEL")

            self.llm = init_chat_model(
                model_provider="openai",
                model=api_model,
                temperature=temperature,
                api_key=api_key,
                base_url=api_url
            )

            if self.llm:
                self._init_agent()

        except Exception as e:
            print(f"Failed to initialize LLM: {str(e)}")
            raise

    def _init_agent(self):
        """Initialize the agent with tools"""
        if not self.llm:
            return

        tools = self.mcp_manager.get_all_langchain_tools()
        print(f"Initializing agent with {len(tools)} tools")

        prompt = ChatPromptTemplate.from_messages([
            ("system", "You are a helpful AI assistant with access to various tools."),
            ("placeholder", "{messages}")
        ])

        self.agent_executor = create_react_agent(
            self.llm,
            tools,
            state_schema=AgentState,
            prompt=prompt
        )

    def _convert_openai_messages_to_langchain(self, messages: List[ChatMessage]) -> List[BaseMessage]:
        """Convert OpenAI format messages to LangChain format"""
        langchain_messages = []
        for msg in messages:
            if msg.role == "user":
                langchain_messages.append(HumanMessage(content=msg.content))
            elif msg.role == "assistant":
                langchain_messages.append(AIMessage(content=msg.content))
        return langchain_messages

    async def chat_completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """Handle non-streaming chat completion"""
        if not self.initialized:
            await self.initialize()

        # Generate conversation ID
        thread_id = uuid.uuid4().hex

        # Convert messages
        langchain_messages = self._convert_openai_messages_to_langchain(request.messages)

        input_messages = {
            "messages": langchain_messages,
            "today_datetime": datetime.now().isoformat(),
        }

        # Collect complete response
        full_response = ""
        try:
            async for chunk in self.agent_executor.astream(
                    input_messages,
                    stream_mode=["messages", "values"],
                    config={"configurable": {"thread_id": thread_id}}
            ):
                if isinstance(chunk, tuple) and chunk[0] == "messages":
                    message_chunk = chunk[1][0]
                    if hasattr(message_chunk, "content") and not isinstance(message_chunk, ToolMessage):
                        content = message_chunk.content
                        # Filter out raw tool output
                        if content.startswith("["):
                            end_marker = "')]"
                            if end_marker in content:
                                content = content[content.find(end_marker) + len(end_marker):]
                        full_response += content

            # Save conversation
            if self.conversation_manager:
                await self.conversation_manager.save_id(thread_id)

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error during chat completion: {str(e)}")

        # Format response
        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        return ChatCompletionResponse(
            id=response_id,
            created=int(datetime.now().timestamp()),
            model=request.model,
            choices=[
                ChatCompletionResponseChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=full_response.strip()),
                    finish_reason="stop"
                )
            ],
            usage={
                "prompt_tokens": 0,  # Not implemented
                "completion_tokens": 0,  # Not implemented
                "total_tokens": 0
            }
        )

    async def chat_completion_stream(self, request: ChatCompletionRequest) -> AsyncGenerator[str, None]:
        """Handle streaming chat completion"""
        if not self.initialized:
            await self.initialize()

        # Generate conversation ID
        thread_id = uuid.uuid4().hex
        response_id = f"chatcmpl-{uuid.uuid4().hex}"

        # Convert messages
        langchain_messages = self._convert_openai_messages_to_langchain(request.messages)

        input_messages = {
            "messages": langchain_messages,
            "today_datetime": datetime.now().isoformat(),
        }

        try:
            async for chunk in self.agent_executor.astream(
                    input_messages,
                    stream_mode=["messages", "values"],
                    config={"configurable": {"thread_id": thread_id}}
            ):
                if isinstance(chunk, tuple) and chunk[0] == "messages":
                    message_chunk = chunk[1][0]
                    if hasattr(message_chunk, "content") and not isinstance(message_chunk, ToolMessage):
                        content = message_chunk.content
                        # Filter out raw tool output
                        if content.startswith("["):
                            end_marker = "')]"
                            if end_marker in content:
                                content = content[content.find(end_marker) + len(end_marker):]

                        if content:
                            stream_response = ChatCompletionStreamResponse(
                                id=response_id,
                                created=int(datetime.now().timestamp()),
                                model=request.model,
                                choices=[
                                    ChatCompletionStreamChoice(
                                        index=0,
                                        delta={"content": content},
                                        finish_reason=None
                                    )
                                ]
                            )
                            yield f"data: {stream_response.model_dump_json()}\n\n"

            # Final message
            final_response = ChatCompletionStreamResponse(
                id=response_id,
                created=int(datetime.now().timestamp()),
                model=request.model,
                choices=[
                    ChatCompletionStreamChoice(
                        index=0,
                        delta={},
                        finish_reason="stop"
                    )
                ]
            )
            yield f"data: {final_response.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"

            # Save conversation
            if self.conversation_manager:
                await self.conversation_manager.save_id(thread_id)

        except Exception as e:
            error_response = ChatCompletionStreamResponse(
                id=response_id,
                created=int(datetime.now().timestamp()),
                model=request.model,
                choices=[
                    ChatCompletionStreamChoice(
                        index=0,
                        delta={"content": f"Error: {str(e)}"},
                        finish_reason="stop"
                    )
                ]
            )
            yield f"data: {error_response.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"


# FastAPI app
app = FastAPI(title="OpenAI-Compatible MCP API", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global server instance
server = None


@app.on_event("startup")
async def startup_event():
    global server
    config_path = Path("config.json")
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found at {config_path}")

    server = OpenAIMCPServer(config_path)
    await server.initialize()


@app.get("/v1/models")
async def list_models():
    """List available models (OpenAI API compatibility)"""
    return {
        "object": "list",
        "data": [
            {
                "id": "gpt-4",
                "object": "model",
                "created": int(datetime.now().timestamp()),
                "owned_by": "mcp-client"
            },
            {
                "id": "gpt-3.5-turbo",
                "object": "model",
                "created": int(datetime.now().timestamp()),
                "owned_by": "mcp-client"
            }
        ]
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """Handle chat completions (OpenAI API compatibility)"""
    global server
    if not server:
        raise HTTPException(status_code=500, detail="Server not initialized")

    if request.stream:
        return StreamingResponse(
            server.chat_completion_stream(request),
            media_type="text/plain",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
        )
    else:
        response = await server.chat_completion(request)
        return response


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    global server
    return {
        "status": "healthy" if server and server.initialized else "initializing",
        "timestamp": datetime.now().isoformat()
    }


def main():
    """Run the OpenAI-compatible API server"""
    if os.name == "nt":
        # Use ProactorEventLoop on Windows
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)

    print("Starting OpenAI-compatible MCP API server...")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )


if __name__ == "__main__":
    main()