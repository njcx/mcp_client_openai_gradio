import os
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, Type
from dataclasses import dataclass
from pathlib import Path
import json
import asyncio
from datetime import datetime

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel
from jsonschema_pydantic import jsonschema_to_pydantic

ASYNC_INITIALIZE_TIMEOUT = 30  # 30 second timeout for async operations
ASYNC_TOOL_CALL_TIMEOUT = 60  # 60 second timeout for tool calls

class MCPError(Exception):
    """Base exception class for MCP-related errors"""
    pass


class MCPConfigError(MCPError):
    """Configuration-related errors"""
    pass


class MCPConnectionError(MCPError):
    """Connection-related errors"""
    pass


class MCPToolError(MCPError):
    """Tool-related errors"""
    pass


@dataclass
class MCPConfig:
    """Configuration for MCP server connection"""
    type: str  # 'stdio' or 'sse'
    name: str
    # For STDIO
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    # For SSE
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None

    def validate(self) -> None:
        """Validate configuration"""
        if self.type not in ["stdio", "sse"]:
            raise MCPConfigError(f"Invalid client type: {self.type}")

        if self.type == "stdio" and not self.command:
            raise MCPConfigError("Command is required for STDIO client")

        if self.type == "sse" and not self.url:
            raise MCPConfigError("URL is required for SSE client")


class MCPClientBase(ABC):
    """Abstract base class for MCP clients"""

    def __init__(self, config: MCPConfig):
        self.config = config
        self.tools: Optional[List[types.Tool]] = None
        self._debug_mode = True  # Default enabled
        self._debug_logs: List[str] = []

        try:
            self.config.validate()
        except MCPConfigError as e:
            self._log(f"Configuration error: {str(e)}", "ERROR")
            raise

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize connection and fetch tools"""
        pass

    @abstractmethod
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> types.CallToolResult:
        """Call a specific tool"""
        pass

    def create_langchain_tool(self, tool_schema: types.Tool) -> BaseTool:
        """Create a LangChain tool from MCP tool schema"""
        self._log(f"Creating LangChain tool for {tool_schema.name}")

        try:
            input_model = jsonschema_to_pydantic(tool_schema.inputSchema)

            class McpTool(BaseTool):
                name: str = tool_schema.name
                description: str = tool_schema.description
                args_schema: Type[BaseModel] = input_model
                mcp_client: MCPClientBase = self

                def _run(self, **kwargs):
                    raise NotImplementedError("Only async operations are supported")

                async def _arun(self, **kwargs):
                    try:
                        result = await self.mcp_client.call_tool(self.name, kwargs)
                        if result.isError:
                            raise ToolException(result.content)
                        return result.content
                    except Exception as e:
                        self.mcp_client._log(f"Tool execution failed: {str(e)}", "ERROR")
                        raise ToolException(str(e))

            return McpTool()
        except Exception as e:
            self._log(f"Failed to create LangChain tool: {str(e)}", "ERROR")
            raise MCPToolError(f"Failed to create tool {tool_schema.name}: {str(e)}")

    def get_langchain_tools(self) -> List[BaseTool]:
        """Convert all MCP tools to LangChain tools"""
        if not self.tools:
            raise MCPToolError("Client not initialized")

        return [self.create_langchain_tool(tool) for tool in self.tools]

    def toggle_debug(self) -> None:
        """Toggle debug mode"""
        self._debug_mode = not self._debug_mode
        self._log(f"Debug mode {'enabled' if self._debug_mode else 'disabled'}")

    def get_debug_logs(self) -> List[str]:
        """Get current debug logs"""
        return self._debug_logs

    def clear_debug_logs(self) -> None:
        """Clear debug logs"""
        self._debug_logs = []

    def _log(self, message: str, level: str = "INFO") -> None:
        """Add a log message if debug mode is enabled"""
        if self._debug_mode:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            log_entry = f"[{timestamp}] {level}: {message}"
            self._debug_logs.append(log_entry)

    def get_tool_names(self) -> List[str]:
        """Get list of available tool names"""
        if not self.tools:
            return []
        return [tool.name for tool in self.tools]

    def get_tool_descriptions(self) -> Dict[str, str]:
        """Get mapping of tool names to descriptions"""
        if not self.tools:
            return {}
        return {tool.name: tool.description for tool in self.tools}


class MCPStdioClient(MCPClientBase):
    """STDIO-based MCP client"""

    async def initialize(self) -> None:
        if not self.config.command:
            raise MCPConfigError("Command is required for STDIO client")

        # Start initializing STDIO client
        print(f"\nInitializing STDIO client for {self.config.command}")
        self._log(f"Initializing STDIO client for {self.config.command}")

        # Create server parameters with explicit env settings -
        # These force Python to run in unbuffered mode
        # Makes Python flush output immediately without buffering
        # This is crucial for real-time communication between processes, ensuring that output is sent immediately rather than being held in a buffer
        # Particularly important for stdio communication where we need immediate response/feedback
        # Otherwise, STDIO servers may not send output until the buffer is full or the process ends - which results in what looks like a hang
        env = {
            'PYTHONUNBUFFERED': '1',
            'PATH': os.environ.get('PATH', '')
        }
        if self.config.env:
            env.update(self.config.env)

        server_params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args or [],
            env=env
        )

        print(f"Creating stdio client with command: {server_params.command}")
        print(f"Working directory: {os.getcwd()}")
        # print(f"Environment PATH: {env.get('PATH')}") # Uncomment to debug PATH issues

        try:
            # Add timeout for operations
            async with asyncio.timeout(ASYNC_INITIALIZE_TIMEOUT):
                async with stdio_client(server_params) as (read, write):
                    print("Stdio streams established")
                    async with ClientSession(read, write) as session:
                        self._log("Session created, initializing...")
                        await session.initialize()
                        self._log("Session initialized, listing tools...")
                        tools_result: types.ListToolsResult = await session.list_tools()
                        self.tools = tools_result.tools
                        self._log(f"Tools loaded: {[tool.name for tool in self.tools]}")
                        print(f"Tools loaded: {[tool.name for tool in self.tools]}")

        except asyncio.TimeoutError:
            error_msg = f"Timeout, {ASYNC_INITIALIZE_TIMEOUT} seconds, while initializing STDIO client"
            self._log(error_msg, "ERROR")
            raise MCPConnectionError(error_msg)
        except Exception as e:
            error_msg = f"Failed to initialize STDIO client: {str(e)}"
            self._log(error_msg, "ERROR")
            raise MCPConnectionError(error_msg)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> types.CallToolResult:
        """Call a specific tool with timeout handling"""
        if not self.tools:
            raise MCPToolError("Client not initialized")

        env = {'PYTHONUNBUFFERED': '1'}
        if self.config.env:
            env.update(self.config.env)

        server_params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args or [],
            env=env)

        try:
            async with asyncio.timeout(ASYNC_TOOL_CALL_TIMEOUT):  # 60 second timeout for tool execution
                async with stdio_client(server_params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments=arguments)
                        return result
        except asyncio.TimeoutError:
            self._log(f"Tool execution timed out at {ASYNC_TOOL_CALL_TIMEOUT} seconds", "ERROR")
            raise MCPConnectionError(f"Tool execution timed out at {ASYNC_TOOL_CALL_TIMEOUT} seconds")
        except Exception as e:
            self._log(f"Tool execution failed: {str(e)}", "ERROR")
            raise MCPToolError(f"Tool execution failed: {str(e)}")

class MCPClientManager:
    """Manager class for handling multiple MCP clients"""
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.clients: Dict[str, MCPClientBase] = {}
        self._debug_mode = True
        print(f"Initialized with config path: {config_path}")

    @staticmethod
    def _get_python_env(server_config: Dict[str, Any]) -> Dict[str, str]:
        """Get Python environment variables from config"""
        env = os.environ.copy()
        if server_config.get("env"):
            env.update(server_config["env"])
        return env

    @staticmethod
    def _verify_command(command: str) -> bool:
        """Verify command exists either as full path or in PATH"""
        # If it's a full path, check directly
        if os.path.isabs(command):
            return Path(command).exists()

        # Otherwise check if it's in PATH
        import shutil
        return shutil.which(command) is not None

    async def initialize(self) -> None:
        """Initialize all clients from config file"""
        try:
            with open(self.config_path) as f:
                config = json.load(f)
        except Exception as e:
            raise MCPConfigError(f"Failed to load config from {self.config_path}: {str(e)}")

        for server_name, server_config in config.get("mcpServers", {}).items():
            print(f"\nInitializing server: {server_name}")

            client_type = server_config.get("type")

            client_config = MCPConfig(
                type=client_type,
                name=server_name,
                command=server_config.get("command"),
                args=server_config.get("args", []),
                env=self._get_python_env(server_config) if client_type == "stdio" else None,
                url=server_config.get("url"),
                headers=server_config.get("headers", {})
            )

            try:
                client: MCPClientBase
                if client_type == "stdio":
                    if not client_config.command or not self._verify_command(client_config.command):
                        print(f"Invalid command configuration {client_config.command} for {server_name}")
                        continue
                    client = MCPStdioClient(client_config)
                elif client_type == "sse":
                    if not client_config.url:
                        print(f"No URL specified for {server_name}")
                        continue
                    client = MCPSSEClient(client_config)
                else:
                    self._log(f"Unknown client type: {client_type} - Will ignore", "ERROR")
                    raise MCPConfigError(f"Unknown client type: {client_type}")

                await client.initialize()
                self.clients[server_name] = client
                print(f"Successfully initialized {server_name}")

            except Exception as e:
                print(f"Failed to initialize client {server_name}: {str(e)}")
                self._log(f"Failed to initialize client {server_name}: {str(e)}", "ERROR")
                import traceback
                traceback.print_exc()

    def get_all_langchain_tools(self) -> List[BaseTool]:
        """Get all tools as LangChain tools"""
        tools = []
        for name, client in self.clients.items():
            try:
                client_tools = client.get_langchain_tools()
                print(f"Got {len(client_tools)} tools from {name}: {[t.name for t in client_tools]}")
                self._log(f"Got {len(client_tools)} tools from {name}: {[t.name for t in client_tools]}")
                tools.extend(client_tools)
            except Exception as e:
                print(f"Error getting tools from {name}: {str(e)}")
                self._log(f"Failed to get tools from {name}: {str(e)}", "ERROR")
                continue

        if not tools:
            print("Warning: No tools were loaded from any clients")
        return tools

    def _log(self, message: str, level: str = "INFO") -> None:
        """Log message to all clients"""
        for client in self.clients.values():
            client._log(message, level)

    def get_all_debug_logs(self) -> List[str]:
        """Get debug logs from all clients"""
        all_logs = []
        for client in self.clients.values():
            all_logs.extend(client.get_debug_logs())
        return all_logs

    def toggle_debug(self) -> None:
        """Toggle debug mode for all clients"""
        self._debug_mode = not self._debug_mode
        for client in self.clients.values():
            client.toggle_debug()

    def clear_all_debug_logs(self) -> None:
        """Clear debug logs from all clients"""
        for client in self.clients.values():
            client.clear_debug_logs()


class MCPSSEClient(MCPClientBase):
    """SSE-based MCP client implementation using official MCP package"""

    def __init__(self, config: MCPConfig):
        super().__init__(config)
        self.session: Optional[ClientSession] = None

    async def initialize(self) -> None:
        """Initialize SSE connection and fetch tools"""
        if not self.config.url:
            self._log("URL is required for SSE client", "ERROR")
            raise MCPConfigError("URL is required for SSE client")

        self._log(f"Initializing SSE client for {self.config.url}")

        try:
            # Set up headers
            headers = self.config.headers or {}

            # Initialize connection with timeout
            async with asyncio.timeout(ASYNC_INITIALIZE_TIMEOUT):  # 30 second timeout
                async with sse_client(self.config.url, headers=headers) as (read, write):
                    async with ClientSession(read, write) as session:
                        self._log("Session created, initializing...")
                        await session.initialize()
                        self._log("Session initialized, listing tools...")
                        tools_result: types.ListToolsResult = await session.list_tools()
                        self.tools = tools_result.tools
                        self._log(f"Tools loaded: {[tool.name for tool in self.tools]}")
                        print(f"Tools loaded: {[tool.name for tool in self.tools]}")

        except asyncio.TimeoutError:
            self._log(f"Timeout,{ASYNC_INITIALIZE_TIMEOUT} seconds, while initializing SSE client", "ERROR")
            raise MCPConnectionError("Timeout while initializing SSE client")
        except Exception as e:
            self._log(f"Failed to initialize SSE client: {str(e)}", "ERROR")
            raise MCPConnectionError(f"Failed to initialize SSE client: {str(e)}")

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> types.CallToolResult:
        """Call a specific tool via SSE"""
        if not self.tools:
            self._log(f"SSE Client {tool_name} not initialized", "ERROR")
            raise MCPToolError(f"SSE Client {tool_name} not initialized")

        try:
            # Create new session for each tool call since we need fresh streams
            async with sse_client(self.config.url, headers=self.config.headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)
                    return result

        except Exception as e:
            self._log(f"Tool execution failed: {str(e)}", "ERROR")
            raise MCPToolError(f"Tool execution failed: {str(e)}")