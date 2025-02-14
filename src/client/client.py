import json
import logging
import os
from collections.abc import AsyncGenerator, Generator
from typing import Any, Optional, Dict, Union

import httpx

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
from httpx import AsyncHTTPTransport, HTTPTransport

from schema import (  # noqa: E402
    ChatHistory,
    ChatHistoryInput,
    ChatMessage,
    Feedback,
    ServiceMetadata,
    StreamInput,
    UserInput,
)


class AgentClientError(Exception):
    pass


class AgentClient:
    """Client for interacting with the agent service."""

    def __init__(
        self,
        base_url: str = "http://localhost",
        agent: str = None,
        timeout: float | None = None,
        get_info: bool = True,
    ) -> None:
        """
        Initialize the client.

        Args:
            base_url (str): The base URL of the agent service.
            agent (str): The name of the default agent to use.
            timeout (float, optional): The timeout for requests.
            get_info (bool, optional): Whether to fetch agent information on init.
                Default: True
        """
        self.base_url = base_url
        self.auth_secret = os.getenv("AUTH_SECRET")
        self.timeout = timeout
        self.info: ServiceMetadata | None = None
        self.agent: str | None = None
        if get_info:
            self.retrieve_info()
        if agent:
            self.update_agent(agent)

    @property
    def _headers(self) -> dict[str, str]:
        headers = {}
        if self.auth_secret:
            headers["Authorization"] = f"Bearer {self.auth_secret}"
        return headers

    def retrieve_info(self) -> None:
        url = f"{self.base_url}/info"
        logger.debug(f"Attempting to connect to agent service at: {url}")
        try:
            response = httpx.get(
                url,
                headers=self._headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            logger.debug("Successfully connected to agent service")
        except httpx.ConnectError as e:
            logger.error(f"Failed to connect to {url}: Connection refused or service not available")
            raise AgentClientError(f"Error connecting to agent service: Connection refused")
        except httpx.HTTPError as e:
            error_msg = f"HTTP Error connecting to {url}: {str(e)}"
            if hasattr(e, 'response'):
                error_msg += f"\nStatus code: {e.response.status_code}"
                error_msg += f"\nResponse text: {e.response.text}"
            logger.error(error_msg)
            raise AgentClientError(f"Error getting service info: {e}")
        except Exception as e:
            logger.error(f"Unexpected error connecting to {url}: {str(e)}")
            raise AgentClientError(f"Error getting service info: {e}")

        self.info: ServiceMetadata = ServiceMetadata.model_validate(response.json())
        if not self.agent or self.agent not in [a.key for a in self.info.agents]:
            self.agent = self.info.default_agent

    def update_agent(self, agent: str, verify: bool = True) -> None:
        if verify:
            if not self.info:
                self.retrieve_info()
            agent_keys = [a.key for a in self.info.agents]
            if agent not in agent_keys:
                raise AgentClientError(
                    f"Agent {agent} not found in available agents: {', '.join(agent_keys)}"
                )
        self.agent = agent

    async def ainvoke(
        self, message: str, model: str | None = None, thread_id: str | None = None
    ) -> ChatMessage:
        """
        Invoke the agent asynchronously. Only the final message is returned.

        Args:
            message (str): The message to send to the agent
            model (str, optional): LLM model to use for the agent
            thread_id (str, optional): Thread ID for continuing a conversation

        Returns:
            AnyMessage: The response from the agent
        """
        if not self.agent:
            raise AgentClientError("No agent selected. Use update_agent() to select an agent.")
        request = UserInput(message=message, thread_id=thread_id, model=model)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/{self.agent}/invoke",
                    json=request.model_dump(),
                    headers=self._headers,
                    timeout=self.timeout,
                )
                response.raise_for_status()
            except httpx.HTTPError as e:
                raise AgentClientError(f"Error: {e}")

        return ChatMessage.model_validate(response.json())

    def invoke(
        self, message: str, model: str | None = None, thread_id: str | None = None
    ) -> ChatMessage:
        """
        Invoke the agent synchronously. Only the final message is returned.

        Args:
            message (str): The message to send to the agent
            model (str, optional): LLM model to use for the agent
            thread_id (str, optional): Thread ID for continuing a conversation

        Returns:
            ChatMessage: The response from the agent
        """
        if not self.agent:
            raise AgentClientError("No agent selected. Use update_agent() to select an agent.")
        request = UserInput(message=message)
        if thread_id:
            request.thread_id = thread_id
        if model:
            request.model = model
        try:
            response = httpx.post(
                f"{self.base_url}/{self.agent}/invoke",
                json=request.model_dump(),
                headers=self._headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise AgentClientError(f"Error: {e}")

        return ChatMessage.model_validate(response.json())

    def _parse_stream_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Parse a line from the SSE stream or raw JSON."""
        try:
            if not line or line == "[DONE]":
                return None
            
            if line.startswith("data: "):
                data = line[6:]
            else:
                data = line
            
            parsed = json.loads(data)
            
            # Handle error messages properly
            if parsed.get("type") == "error":
                logger.error(f"Error from server: {parsed.get('content')}")
                return {
                    "type": "error",
                    "content": parsed.get("content", "Unknown error")
                }
            
            return parsed
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse stream line: {line}, error: {e}")
            return {
                "type": "error",
                "content": f"Failed to parse response: {str(e)}"
            }
        except Exception as e:
            logger.error(f"Unexpected error parsing stream line: {e}")
            return {
                "type": "error",
                "content": f"Unexpected error: {str(e)}"
            }

    def stream(
        self,
        message: str,
        model: str | None = None,
        thread_id: str | None = None,
        stream_tokens: bool = True,
    ) -> Generator[ChatMessage | str, None, None]:
        """
        Stream the agent's response synchronously.

        Each intermediate message of the agent process is yielded as a ChatMessage.
        If stream_tokens is True (the default value), the response will also yield
        content tokens from streaming models as they are generated.

        Args:
            message (str): The message to send to the agent
            model (str, optional): LLM model to use for the agent
            thread_id (str, optional): Thread ID for continuing a conversation
            stream_tokens (bool, optional): Stream tokens as they are generated
                Default: True

        Returns:
            Generator[ChatMessage | str, None, None]: The response from the agent
        """
        if not self.agent:
            raise AgentClientError("No agent selected. Use update_agent() to select an agent.")
        request = StreamInput(message=message, stream_tokens=stream_tokens)
        logger.debug(f"Starting stream request for agent {self.agent}")
        if thread_id:
            request.thread_id = thread_id
        if model:
            request.model = model
        try:
            with httpx.stream(
                "POST",
                f"{self.base_url}/{self.agent}/stream",
                json=request.model_dump(),
                headers=self._headers,
                timeout=self.timeout,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.strip():
                        parsed = self._parse_stream_line(line)
                        if parsed is None:
                            break
                        yield parsed
        except httpx.HTTPError as e:
            raise AgentClientError(f"Error: {e}")

    async def astream(
        self,
        message: Union[str, Dict[str, Any]],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream responses from the agent service."""
        try:
            async with httpx.AsyncClient() as client:
                url = f"{self.base_url}/orchestrator/stream"
                
                # Format message properly
                if isinstance(message, str):
                    data = {"message": message}
                else:
                    data = message
                    
                async with client.stream("POST", url, json=data) as response:
                    response.raise_for_status()
                    
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                            
                        parsed = self._parse_stream_line(line)
                        if parsed:
                            if parsed["type"] == "error":
                                logger.error(f"Stream error: {parsed['content']}")
                                yield parsed
                            else:
                                yield parsed
                            
        except httpx.HTTPError as e:
            logger.error(f"HTTP error during streaming: {e}")
            yield {"type": "error", "content": f"Connection error: {str(e)}"}
        except Exception as e:
            logger.error(f"Unexpected error during streaming: {e}")
            yield {"type": "error", "content": f"Stream error: {str(e)}"}

    async def acreate_feedback(
        self, run_id: str, key: str, score: float, kwargs: dict[str, Any] = {}
    ) -> None:
        """
        Create a feedback record for a run.

        This is a simple wrapper for the LangSmith create_feedback API, so the
        credentials can be stored and managed in the service rather than the client.
        See: https://api.smith.langchain.com/redoc#tag/feedback/operation/create_feedback_api_v1_feedback_post
        """
        request = Feedback(run_id=run_id, key=key, score=score, kwargs=kwargs)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/feedback",
                    json=request.model_dump(),
                    headers=self._headers,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                response.json()
            except httpx.HTTPError as e:
                raise AgentClientError(f"Error: {e}")

    def get_history(
        self,
        thread_id: str,
    ) -> ChatHistory:
        """
        Get chat history.

        Args:
            thread_id (str, optional): Thread ID for identifying a conversation
        """
        request = ChatHistoryInput(thread_id=thread_id)
        try:
            response = httpx.post(
                f"{self.base_url}/history",
                json=request.model_dump(),
                headers=self._headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise AgentClientError(f"Error: {e}")

        return ChatHistory.model_validate(response.json())
