"""MCP agent integration — stub for Stage 12.5."""

from mcp.server import Server
from mcp.server.sse import SseServerTransport

# Server instance — tools registered in Stage 12.5
mcp_server = Server("semanticdog")
sse_transport = SseServerTransport("/mcp/messages")


async def handle_sse(request):
    """SSE connection handler — mounted at /mcp/sse."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0], streams[1],
            mcp_server.create_initialization_options(),
        )


async def handle_messages(scope, receive, send):
    """POST message handler — mounted at /mcp/messages."""
    await sse_transport.handle_post_message(scope, receive, send)
