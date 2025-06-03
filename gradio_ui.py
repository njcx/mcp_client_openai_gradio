import uuid
import os
import gradio as gr
import asyncio
from pathlib import Path
from typing import List, Dict, Any, TypedDict, Annotated, Generator
from datetime import datetime

from langchain.chat_models import init_chat_model
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.graph import add_messages
from langgraph.managed import IsLastStep
from langgraph.prebuilt import create_react_agent
from langchain_core.prompts import ChatPromptTemplate

from cli.cli import ConversationManager
from mcp_client import MCPClientManager
from mcp_client.mcp_server_util import verify_uvx_installation, verify_npx_installation, verify_python_installation
from dotenv import load_dotenv

SQLITE_DB = Path("conversation_db/conversations.db")


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    is_last_step: IsLastStep
    today_datetime: str
    remaining_steps: int


class GradioMCPInterface:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.mcp_manager = MCPClientManager(config_path)
        self.llm = None
        self.agent_executor: bool = None
        self.debug_enabled: bool = True
        self.initialized: bool = False
        self._debug_logs: List[str] = []
        self.current_model: str = ""
        self.current_temperature: float = None
        load_dotenv()
        print(f"Initialized with config path: {config_path}")

    async def initialize(self):
        """Initialize MCP clients and load configuration"""
        if not self.initialized:
            try:
                print("Starting initialization...")
                if not await verify_python_installation():
                    raise RuntimeError("Failed to verify Python installation")

                if not await verify_uvx_installation():
                    raise RuntimeError("Failed to verify UVX installation")

                if not await verify_npx_installation():
                    raise RuntimeError("Failed to verify NPX installation")

                print("Standard MCP STDIO Commands Verified, initializing MCP manager...")
                try:
                    await self.mcp_manager.initialize()
                except Exception as e:
                    print(f"Error in MCP manager initialization: {str(e)}")  # Debug print
                    self._log(f"MCP manager initialization failed: {str(e)}", "ERROR")
                    raise

                print("MCP manager initialized, setting up LLM...")
                self._init_llm()
                self.initialized = True
                print("LLM Agent initialization complete")
            except Exception as e:
                print(f"Initialization error: {str(e)}")
                self._log(f"Initialization failed: {str(e)}", "ERROR")
                raise

    def _log(self, message: str, level: str = "INFO") -> None:
        """Add a log message if debug mode is enabled"""
        if self.debug_enabled:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            log_entry = f"[{timestamp}] {level}: {message}"
            self._debug_logs.append(log_entry)
            # Keep only last 1000 logs
            self._debug_logs = self._debug_logs[-1000:]

    def _format_debug_logs(self) -> str:
        """Format debug logs for display"""
        if not self.debug_enabled:
            return "Debug mode disabled"
        return "\n".join(self._debug_logs[-50:])  # Show last 50 logs

    def _format_history(self, history: list) -> list:
        """Format chat history into consistent structure"""
        formatted_history = []
        for entry in history:
            if isinstance(entry, list) and len(entry) == 2:
                formatted_history.extend([{"role": "user", "content": entry[0]}, {"role": "assistant", "content": entry[1]}])
        return formatted_history

    def _init_llm(self, model: str = "deepseek-v3", temperature: float = 0.7) -> None:
        """Initialize or update LLM configuration"""
        try:
            api_key = os.getenv("OPEN_API_KEY")
            api_url = os.getenv("OPEN_API_URL")
            api_model = os.getenv("OPEN_API_MODEL")
            self.llm = init_chat_model(model_provider="openai", model=api_model, temperature=temperature, api_key=api_key, base_url=api_url)
            self.current_model = model
            self.current_temperature = temperature
            if self.llm:
                self._init_agent()
        except Exception as e:
            self._log(f"Failed to initialize LLM: {str(e)}", "ERROR")
            raise

    def _init_agent(self) -> None:
        """Initialize the agent with current tools and LLM"""
        if not self.llm:
            return

        tools = self.mcp_manager.get_all_langchain_tools()
        print(f"Initializing agent with {len(tools)} tools")
        if not tools:
            print("WARNING: No tools available for agent!")  # Debug print
            self._log("No tools available for agent", "WARNING")
            return

        prompt = ChatPromptTemplate.from_messages(
            [("system", "You are a helpful AI assistant with access to various tools."), ("placeholder", "{messages}")]
        )

        self.agent_executor = create_react_agent(
            self.llm,
            tools,
            state_schema=AgentState,
            prompt=prompt
        )

    async def chat(self, message: str, history: list, model: str, temperature: float, debug_mode: bool) -> Generator:
        """Handle chat messages and yield responses"""
        if not self.initialized:
            try:
                await self.initialize()
            except Exception as e:
                error_msg = f"Initialization failed: {str(e)}"
                yield (
                    "",
                    [{"role": "user", "content": message}, {"role": "assistant", "content": error_msg}],
                    self._format_debug_logs(),
                )
                return

        # Update configurations if changed
        if debug_mode != self.debug_enabled:
            self.debug_enabled = debug_mode
            self.mcp_manager.toggle_debug()

        # Update LLM if configuration changed or if LLM hasn't been initialized yet
        if self.llm is None or model != self.llm.model_name or temperature != self.llm.temperature:
            try:
                self._log(f"Initializing LLM with model: {model} and temperature: {temperature}")
                self._init_llm(model, temperature)
            except Exception as e:
                error_msg = f"Failed to initialize LLM: {str(e)}"
                yield (
                    "",
                    [{"role": "user", "content": message}, {"role": "assistant", "content": error_msg}],
                    self._format_debug_logs(),
                )
                return

        # Initialize conversation manager
        if not hasattr(self, "conversation_manager"):
            self.conversation_manager = ConversationManager(SQLITE_DB)

        # Handle conversation continuation
        is_continuation = message.startswith("c ")
        if is_continuation:
            message = message[2:]
            thread_id = await self.conversation_manager.get_last_id()
        else:
            thread_id = uuid.uuid4().hex

        input_messages = {
            "messages": [HumanMessage(content=message)],
            "today_datetime": datetime.now().isoformat(),
        }

        current_response = ""
        formatted_history = self._format_history(history)

        try:
            async for chunk in self.agent_executor.astream(
                input_messages, stream_mode=["messages", "values"], config={"configurable": {"thread_id": thread_id}}
            ):
                if isinstance(chunk, tuple) and chunk[0] == "messages":
                    message_chunk = chunk[1][0]
                    if hasattr(message_chunk, "content"):
                        if isinstance(message_chunk, ToolMessage):
                            self._log(f"Tool Call ID: {message_chunk.tool_call_id}", "TOOL")
                            self._log(f"Tool Response: {message_chunk.content}", "TOOL")
                            self._log(f"Tool Name: {message_chunk.name}", "TOOL")
                            self._log(f"Tool ID: {message_chunk.id}", "TOOL")
                        else:
                            # Filter out raw tool output and any trailing characters
                            content = message_chunk.content
                            if content.startswith("["):
                                # Find the end of the raw output by looking for ")]'"
                                end_marker = "')]"
                                if end_marker in content:
                                    content = content[content.find(end_marker) + len(end_marker) :]
                            current_response += content
                        new_history = formatted_history + [
                            {"role": "user", "content": message},
                            {"role": "assistant", "content": current_response},
                        ]
                        # Use self._format_debug_logs() instead of getting from manager
                        yield "", new_history, self._format_debug_logs()

                elif isinstance(chunk, tuple) and chunk[0] == "values":
                    tool_message = chunk[1]["messages"][-1]
                    if isinstance(tool_message, AIMessage) and tool_message.tool_calls:
                        if self.debug_enabled:
                            for tool_call in tool_message.tool_calls:
                                self._log(f"Tool Call: {tool_call['name']} - ID: {tool_call['id']}", "TOOL")
                                self._log(f"Arguments: {tool_call['args']}", "TOOL")

            # Save conversation ID
            try:
                await self.conversation_manager.save_id(thread_id)
            except Exception as e:
                self._log(f"Failed to save conversation ID: {str(e)}", "WARNING")

        except Exception as e:
            error_msg = f"Error during chat: {str(e)}"
            self._log(error_msg, "ERROR")
            new_history = formatted_history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": error_msg},
            ]
            yield "", new_history, self._format_debug_logs()
            return

        # Final state
        final_history = formatted_history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": current_response},
        ]
        yield "", final_history, self._format_debug_logs()


async def create_ui(config_path: Path = Path("config.json")) -> gr.Interface:
    """Create and launch the Gradio interface"""
    interface = GradioMCPInterface(config_path)

    # Initialize before creating UI
    print("Initializing interface...")
    await interface.initialize()
    print("Interface initialized successfully")

    with gr.Blocks(title="MCP Client Chat Interface") as demo:
        gr.Markdown("# MCP Client Chat Interface")

        with gr.Row():
            with gr.Column(scale=2):
                chatbot = gr.Chatbot(height=600, show_copy_button=True, container=True, type="messages")
                msg = gr.Textbox(
                    placeholder="Ask about whatever your heart desires...",
                    container=False,
                    scale=4,
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        model = gr.Dropdown(choices=["gpt-3.5-turbo", "gpt-4"], value="gpt-4", label="Model")
                    with gr.Column(scale=1):
                        temperature = gr.Slider(minimum=0, maximum=1, value=0, step=0.1, label="Temperature")
                    with gr.Column(scale=1):
                        debug_mode = gr.Checkbox(value=True, label="Debug Mode")

            with gr.Column(scale=1):
                debug_output = gr.Textbox(
                    label="Debug Logs",
                    placeholder="Debug logs will appear here...",
                    lines=25,
                    max_lines=25,
                    container=True,
                    interactive=False,
                )

        # Set up chat handler
        msg.submit(
            interface.chat,
            inputs=[msg, chatbot, model, temperature, debug_mode],
            outputs=[msg, chatbot, debug_output],
            show_progress=True,
        ).then(lambda: "", None, msg)

    return demo


async def main():
    """Enhanced main function with proper initialization"""
    try:
        # Enable asyncio debug mode for better error reporting
        asyncio.get_event_loop().set_debug(True)

        # Create and launch the demo
        config_path = Path("config.json")
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found at {config_path}")

        demo = await create_ui(config_path)
        demo.queue()

        # Launch with specific host and port
        demo.launch(
            server_name="127.0.0.1",  # Local only
            server_port=7860,
            show_error=True,
        )
    except Exception as e:
        print(f"Failed to launch Gradio interface: {str(e)}")
        raise


if __name__ == "__main__":
    # Configure asyncio for Windows
    if os.name == "nt":
        # Use SelectEventLoop on Windows
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)

    # Run with proper error handling
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    except Exception as e:
        print(f"Fatal error: {str(e)}")
        raise
