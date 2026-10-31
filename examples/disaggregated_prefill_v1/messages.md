# Anthropic Messages through the load balance proxy

`load_balance_proxy_server_example.py` accepts `POST /v1/messages` using
the same prefill and decode pools configured for OpenAI requests. The proxy
startup command does not change.

Both backends must expose `/v1/messages`. The prefill backend must support
the vLLM `kv_transfer_params` extension on both the request and response.
The proxy sends a non-streaming, one-token request to P, then forwards the
original request with P's returned KV metadata to D. If P returns no KV
metadata, the proxy returns HTTP 502 without sending a request to D.
Older backends without this extension need an upgrade; adding this proxy
route does not add Messages support to the backend.

For a proxy listening on port 8081:

```bash
curl -N http://localhost:8081/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "your-served-model",
    "max_tokens": 128,
    "stream": true,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

Set `stream` to `false` for a JSON response. The proxy forwards Messages
response bytes unchanged, including tool blocks, thinking blocks, usage,
and named SSE events. It does not apply OpenAI `choices` parsing, cached-token
rewriting, or OpenAI-specific recompute continuation to Messages responses.
Mid-stream transport failures interrupt the response rather than silently
completing it. Initial backend HTTP errors retain their status and body.

The headers `x-api-key`, `anthropic-version`, and `anthropic-beta` are forwarded
to both backends. `X-Request-Id` is preserved through the existing request-ID
path. As with the OpenAI routes, the backend `Authorization: Bearer ...`
header comes from the proxy's `OPENAI_API_KEY` environment variable.
